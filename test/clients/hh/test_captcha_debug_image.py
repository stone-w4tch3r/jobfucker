"""Debug-dump helper: dumps happen only with DEBUG logging, gated by env/temp fallback."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from jobfucker.clients.hh.captcha import log_captcha_image

_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-frame"
_CAPTCHA_LOGGER = "jobfucker.clients.hh.captcha"


def _enable_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=_CAPTCHA_LOGGER)


def _disable_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_CAPTCHA_LOGGER)


def test_no_dump_without_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _disable_debug(caplog)
    monkeypatch.setenv("LOGGING_CAPTCHA_IMAGES_PATH", str(tmp_path))

    assert log_captcha_image(_FAKE_PNG, label="standalone") is None
    assert list(tmp_path.iterdir()) == []


def test_dump_uses_env_path(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_debug(caplog)
    target = tmp_path / "captcha-images"
    monkeypatch.setenv("LOGGING_CAPTCHA_IMAGES_PATH", str(target))

    path = log_captcha_image(_FAKE_PNG, label="standalone")

    assert path is not None
    assert path.parent == target
    assert path.name.startswith("captcha-standalone-")
    assert path.suffix == ".png"
    assert path.read_bytes() == _FAKE_PNG


def test_dump_falls_back_to_system_temp(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_debug(caplog)
    monkeypatch.delenv("LOGGING_CAPTCHA_IMAGES_PATH", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    path = log_captcha_image(_FAKE_PNG, label="login")

    assert path is not None
    assert path.parent == tmp_path / "jobfucker-captcha-images"
    assert path.read_bytes() == _FAKE_PNG


def test_dump_failure_is_swallowed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_debug(caplog)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("LOGGING_CAPTCHA_IMAGES_PATH", str(blocker / "nested"))

    assert log_captcha_image(_FAKE_PNG, label="login") is None
