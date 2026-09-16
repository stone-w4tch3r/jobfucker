"""Preflight checks for the browser-engine startup install (tiny plain-pytest exception, test/AGENTS.md §3a)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Final

import pytest
from rusty_results.prelude import Err, Result

from jobfucker.clients.hh import browser as browser_module
from jobfucker.clients.hh.browser import ensure_chromium_engine
from jobfucker.reporting import EventLevel, Reporter, RunEvent
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


class _FakeReader:
    """Stream-reader double: one payload, then EOF (``b""``) like the real thing."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, size: int = -1) -> bytes:
        del size
        payload, self._payload = self._payload, b""
        return payload


class _FakeProcess:
    def __init__(
        self,
        returncode: int,
        *,
        crash: bool = False,
        hang: bool = False,
        stdout: bytes = b"",
        stderr: bytes = b"boom",
    ) -> None:
        self.returncode = returncode
        self._crash = crash
        self._hang = hang
        self.stdout = _FakeReader(stdout)
        self.stderr = _FakeReader(stderr)
        self.killed = False
        self.waited = False

    async def wait(self) -> int:
        if self._hang and not self.killed:
            await asyncio.sleep(1)
        else:
            # a real process yields the loop while running — let the drain
            # tasks consume the pipe output before the exit
            await asyncio.sleep(0)
        self.waited = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.unit
async def test_existing_engine_skips_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A present engine binary means no installer subprocess, ever."""
    binary = tmp_path / "chrome"
    binary.touch()
    spawned: list[tuple[str, ...]] = []

    async def fake_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        spawned.append(args)
        return _FakeProcess(0)

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", fake_spawn)
    result = await ensure_chromium_engine(str(binary))
    assert result.is_ok
    assert not spawned


@pytest.mark.unit
async def test_missing_engine_installs_chromium(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing engine triggers the installer and announces progress to the reporter."""
    spawned: list[tuple[str, ...]] = []
    reporter = RecordingReporter()

    async def fake_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        spawned.append(args)
        return _FakeProcess(0)

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", fake_spawn)
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_ok
    assert spawned == [_INSTALL_ARGV]
    assert reporter.levels() == ["info", "success"]
    assert reporter.events[-1].message == "Browser engine installed"


@pytest.mark.unit
async def test_failed_install_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """Installer exit codes and spawn crashes only log; preflight stays best-effort."""

    async def failing_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        return _FakeProcess(1, stdout=b"downloading...\r|#####     | 33%", stderr=b"fatal: disk full")

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", failing_spawn)
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    assert "fatal: disk full" in result.unwrap_err()
    assert any(
        "auto-install failed" in message and "fatal: disk full" in message for message in captured_logs.messages()
    )
    assert reporter.levels() == ["info", "warning"]
    # the reporter warning carries the installer's last output line for the user
    assert "fatal: disk full" in reporter.events[-1].message

    async def crashing_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        raise RuntimeError("cannot spawn")

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", crashing_spawn)
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    assert "RuntimeError" in result.unwrap_err()
    assert any("auto-install crashed" in message for message in captured_logs.messages())
    assert reporter.levels() == ["info", "warning", "info", "warning"]


@pytest.mark.unit
async def test_successful_install_output_lands_in_debug_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A clean install stores the captured installer output in the DEBUG log only."""

    async def fake_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        return _FakeProcess(0, stdout=b"chromium downloaded to /tmp/x")

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", fake_spawn)
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"))
    assert result.is_ok
    assert any(
        record.levelname == "DEBUG" and "chromium downloaded to /tmp/x" in record.getMessage()
        for record in captured_logs.records
    )


@pytest.mark.unit
async def test_install_timeout_kills_process_and_recovers_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_logs: LogCapture
) -> None:
    """A hung installer is killed; output downloaded so far lands in log + event."""
    process = _FakeProcess(0, hang=True, stdout=b"downloading chromium...\rdownloaded 55%", stderr=b"")
    monkeypatch.setattr(browser_module, "_INSTALL_TIMEOUT_S", 0.01)

    async def fake_spawn(*args: str, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(browser_module.asyncio, "create_subprocess_exec", fake_spawn)
    reporter = RecordingReporter()
    result = await ensure_chromium_engine(str(tmp_path / "absent-chrome"), reporter=reporter)
    assert result.is_err
    assert "timed out" in result.unwrap_err()
    assert process.killed
    assert process.waited
    assert any("timed out" in message for message in captured_logs.messages())
    # partial installer output survived the cancelled wait
    assert any("downloaded 55%" in message for message in captured_logs.messages())
    assert reporter.levels() == ["info", "warning"]
    assert "downloaded 55%" in reporter.events[-1].message


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
