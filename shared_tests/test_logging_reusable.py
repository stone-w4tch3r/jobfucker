from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared.logging.logger_setup import setup_file_logging, setup_stdout_logging


@pytest.fixture
def isolated_root_logger() -> Iterator[None]:
    """Temporarily isolate root logger handlers for shared logging tests."""
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers.copy()
    original_level = root_logger.level

    root_logger.handlers.clear()
    root_logger.setLevel(logging.NOTSET)

    try:
        yield
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers.clear()
        root_logger.handlers.extend(original_handlers)
        root_logger.setLevel(original_level)


def test_stdout_logging_hides_debug_when_level_is_info(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    isolated_root_logger: None,
) -> None:
    """Stdout logging should respect its configured level while file logging keeps DEBUG."""
    setup_file_logging(log_dir=tmp_path, app_name="mockapp", level=logging.DEBUG)
    setup_stdout_logging(level=logging.INFO)

    logger = logging.getLogger("mockapp.validation")
    logger.debug("debug should only be in file")
    logger.info("info should be in stdout and file")

    captured = capsys.readouterr()
    assert "debug should only be in file" not in captured.out
    assert "info should be in stdout and file" in captured.out

    log_contents = (tmp_path / "mockapp.log").read_text(encoding="utf-8")
    assert "debug should only be in file" in log_contents
    assert "info should be in stdout and file" in log_contents


def test_file_logging_default_level_is_info(
    tmp_path: Path,
    isolated_root_logger: None,
) -> None:
    """The file log defaults to INFO; DEBUG records are not written unless asked."""
    setup_file_logging(log_dir=tmp_path, app_name="mockapp")

    logger = logging.getLogger("mockapp.file")
    logger.debug("debug should not be in file by default")
    logger.info("info should be in file")

    log_contents = (tmp_path / "mockapp.log").read_text(encoding="utf-8")
    assert "debug should not be in file by default" not in log_contents
    assert "info should be in file" in log_contents


def test_setup_stdout_logging_is_idempotent(
    capsys: pytest.CaptureFixture[str],
    isolated_root_logger: None,
) -> None:
    setup_stdout_logging(level=logging.INFO)
    setup_stdout_logging(level=logging.INFO)

    logger = logging.getLogger("mockapp.stdout")
    logger.info("stdout should appear once")

    captured = capsys.readouterr()

    assert captured.out.count("stdout should appear once") == 1


def test_stderr_stream_keeps_stdout_clean(
    capsys: pytest.CaptureFixture[str],
    isolated_root_logger: None,
) -> None:
    """CLI mode: ``stream=sys.stderr`` routes logs to stderr, never stdout.

    stdout is the CLI user interface (help, prompts, results); log lines must
    not be mixed into it.
    """
    setup_stdout_logging(level=logging.INFO, stream=sys.stderr)

    logger = logging.getLogger("mockapp.cli")
    logger.info("cli log line")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cli log line" in captured.err


def test_stderr_logging_is_idempotent(
    capsys: pytest.CaptureFixture[str],
    isolated_root_logger: None,
) -> None:
    setup_stdout_logging(level=logging.INFO, stream=sys.stderr)
    setup_stdout_logging(level=logging.INFO, stream=sys.stderr)

    logger = logging.getLogger("mockapp.stderr")
    logger.info("stderr should appear once")

    captured = capsys.readouterr()

    assert captured.err.count("stderr should appear once") == 1


def test_setup_file_logging_is_idempotent(
    tmp_path: Path,
    isolated_root_logger: None,
) -> None:
    setup_file_logging(log_dir=tmp_path, app_name="mockapp")
    setup_file_logging(log_dir=tmp_path, app_name="mockapp")

    logger = logging.getLogger("mockapp.file")
    logger.info("file should contain one line")

    log_contents = (tmp_path / "mockapp.log").read_text(encoding="utf-8")

    assert log_contents.count("file should contain one line") == 1
