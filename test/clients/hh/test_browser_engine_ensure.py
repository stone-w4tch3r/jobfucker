"""Preflight checks for the browser-engine startup install (tiny plain-pytest exception, test/AGENTS.md §3a).

The subprocess layer under :func:`ensure_chromium_engine` is covered by
real-process tests in ``test/test_subprocess_utils.py``; these tests pin the
reporting/Result behavior around a scripted runner fake.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.hh import browser as browser_module
from jobfucker.clients.hh.browser import ensure_chromium_engine
from jobfucker.reporting import EventLevel, Reporter, RunEvent
from jobfucker.subprocess_utils import SubprocessRun
from test.conftest import LogCapture

_INSTALL_ARGV: Final = (sys.executable, "-m", "patchright", "install", "chromium")


class RecordingReporter:
    """Collects published run-events for assertion (never raises)."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        self.events.append(event)

    def levels(self) -> list[EventLevel]:
        return [event.level for event in self.events]


def _script_run(
    monkeypatch: pytest.MonkeyPatch,
    capture: SubprocessRun | None = None,
    error: Exception | None = None,
) -> list[Sequence[str]]:
    """Replace the subprocess runner with a scripted fake; returns recorded calls."""
    calls: list[Sequence[str]] = []

    async def fake_run(argv: Sequence[str], *, timeout_s: float) -> SubprocessRun:
        del timeout_s
        calls.append(argv)
        if error is not None:
            raise error
        assert capture is not None
        return capture

    monkeypatch.setattr(browser_module, "run_subprocess_with_capture", fake_run)
    return calls


@pytest.mark.unit
async def test_existing_engine_skips_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A present engine binary means no installer subprocess, ever."""
    binary = tmp_path / "chrome"
    binary.touch()
    calls = _script_run(monkeypatch, capture=SubprocessRun(returncode=0, stdout=b"", stderr=b"", timed_out=False))
    result = await ensure_chromium_engine(str(binary))
    assert result.is_ok
    assert not calls


@pytest.mark.unit
async def test_missing_engine_installs_chromium(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing engine triggers the installer and announces progress to the reporter."""
    calls = _script_run(
        monkeypatch, capture=SubprocessRun(returncode=0, stdout=b"chromium downloaded", stderr=b"", timed_out=False)
    )
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_ok
    assert calls == [_INSTALL_ARGV]
    assert reporter.levels() == ["info", "success"]
    assert reporter.events[-1].message == "Browser engine installed"


@pytest.mark.unit
async def test_failed_install_reports_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A non-zero installer exit becomes an Err carrying the last output line."""
    _script_run(
        monkeypatch,
        capture=SubprocessRun(
            returncode=1, stdout=b"downloading...\r|#####     | 33%", stderr=b"fatal: disk full", timed_out=False
        ),
    )
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    reason = result.unwrap_err()
    assert "exit code 1" in reason
    assert "fatal: disk full" in reason
    assert any(
        "auto-install failed" in message and "fatal: disk full" in message for message in captured_logs.messages()
    )
    assert reporter.levels() == ["info", "warning"]
    # the reporter warning carries the installer's last output line for the user
    assert "fatal: disk full" in reporter.events[-1].message


@pytest.mark.unit
async def test_timed_out_install_reports_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A timed-out install becomes an Err that names the timeout and the last captured line."""
    _script_run(
        monkeypatch,
        capture=SubprocessRun(
            returncode=-9, stdout=b"downloading chromium...\rdownloaded 55%", stderr=b"", timed_out=True
        ),
    )
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    reason = result.unwrap_err()
    assert "install timed out after 300s" in reason
    assert "downloaded 55%" in reason
    assert any("timed out" in message for message in captured_logs.messages())
    assert any("downloaded 55%" in message for message in captured_logs.messages())
    assert reporter.levels() == ["info", "warning"]
    assert "downloaded 55%" in reporter.events[-1].message


@pytest.mark.unit
async def test_crashed_spawn_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A spawn crash (missing interpreter, hostile environment) is an Err, not an exception."""
    _script_run(monkeypatch, error=RuntimeError("cannot spawn"))
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    assert "RuntimeError: cannot spawn" in result.unwrap_err()
    assert any("auto-install crashed" in message for message in captured_logs.messages())
    assert reporter.levels() == ["info", "warning"]


@pytest.mark.unit
async def test_successful_install_output_lands_in_debug_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A clean install stores the captured installer output in the DEBUG log only."""
    _script_run(
        monkeypatch,
        capture=SubprocessRun(returncode=0, stdout=b"chromium downloaded to /tmp/x", stderr=b"", timed_out=False),
    )
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"))
    assert result.is_ok
    assert any(
        record.levelname == "DEBUG" and "chromium downloaded to /tmp/x" in record.getMessage()
        for record in captured_logs.records
    )


@pytest.mark.unit
async def test_driver_ensure_engine_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The driver memoizes the preflight outcome: repeated calls run the check once."""
    ensured: list[str] = []

    async def fake_ensure(executable_path: str, reporter: Reporter | None = None) -> Result[None, str]:
        del reporter
        ensured.append(executable_path)
        return Err("engine broken")

    monkeypatch.setattr(browser_module, "ensure_chromium_engine", fake_ensure)
    driver = browser_module.PatchrightDriver()
    first = await driver.ensure_engine()
    second = await driver.ensure_engine()
    assert len(ensured) == 1
    # the failure is memoized too: every action fails identically, fast
    assert first.is_err and second.is_err
    assert second.unwrap_err() == "engine broken"


@pytest.mark.unit
async def test_driver_ensure_engine_passes_success_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The driver surfaces an Ok preflight outcome unchanged."""
    ensured: list[str] = []

    async def fake_ensure(executable_path: str, reporter: Reporter | None = None) -> Result[None, str]:
        del reporter
        ensured.append(executable_path)
        return Ok(None)

    monkeypatch.setattr(browser_module, "ensure_chromium_engine", fake_ensure)
    driver = browser_module.PatchrightDriver()
    result = await driver.ensure_engine()
    assert result.is_ok
    assert len(ensured) == 1
