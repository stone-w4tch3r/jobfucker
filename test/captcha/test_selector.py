"""Captcha handler selection order + fail-fast (Phase 6, task 6.1).

Proves :func:`select_captcha_handler` implements architecture §5.4: explicit
CLI flags win, then `openai_captcha` (AI) unless ``--no-captcha-ai``, then
terminal auto-detect, else the documented fail-fast message.
"""

from __future__ import annotations

import pytest

from jobfucker.captcha.ai import AiCaptchaHandler
from jobfucker.captcha.selector import select_captcha_handler
from jobfucker.captcha.terminal_handlers import TerminalCaptchaHandler
from jobfucker.config import OpenAIConfig, PipelineConfig
from test.pipeline_helpers import build_pipeline_config


def _config_with_captcha_ai() -> PipelineConfig:
    """A pipeline config with both main ``openai`` and ``openai_captcha`` set."""
    config = build_pipeline_config()
    config.openai_captcha = OpenAIConfig(
        model="captcha-model",
        base_url="https://captcha.example/v1",
        api_key="captcha-key",
    )
    return config


def test_explicit_flags_win_over_configured_ai() -> None:
    config = _config_with_captcha_ai()
    sixel = select_captcha_handler(config, use_sixel=True).unwrap()
    kitty = select_captcha_handler(config, use_kitty=True).unwrap()
    assert isinstance(sixel, TerminalCaptchaHandler)
    assert isinstance(kitty, TerminalCaptchaHandler)


def test_sixel_and_kitty_are_mutually_exclusive() -> None:
    result = select_captcha_handler(_config_with_captcha_ai(), use_sixel=True, use_kitty=True)
    assert result.is_err
    assert "both" in result.unwrap_err()


def test_configured_ai_is_selected_by_default() -> None:
    config = _config_with_captcha_ai()
    handler = select_captcha_handler(config).unwrap()
    assert isinstance(handler, AiCaptchaHandler)


def test_ai_uses_only_openai_captcha_section() -> None:
    config = _config_with_captcha_ai()
    handler = select_captcha_handler(config).unwrap()
    assert isinstance(handler, AiCaptchaHandler)
    # The handler holds exactly the openai_captcha config (never the main openai).
    assert handler._config.model == "captcha-model"  # type: ignore[private-usage]  # rationale: introspect the injected config in a unit test
    assert config.openai.model != "captcha-model"


def test_no_captcha_ai_forces_terminal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # With AI configured but disabled, and no terminal detected, we fail fast
    # (proving the AI branch is skipped, not silently used). Pin a terminal that
    # matches no capability so detection is deterministic (independent of the
    # ambient TERM/TERM_PROGRAM of whoever runs the suite).
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    result = select_captcha_handler(_config_with_captcha_ai(), no_captcha_ai=True)
    assert result.is_err
    assert "протокол" in result.unwrap_err()


def test_no_captcha_ai_with_detect_uses_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "foot")
    handler = select_captcha_handler(_config_with_captcha_ai(), no_captcha_ai=True).unwrap()
    assert isinstance(handler, TerminalCaptchaHandler)


def test_auto_detect_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "kitty")
    handler = select_captcha_handler(build_pipeline_config()).unwrap()
    assert isinstance(handler, TerminalCaptchaHandler)


def test_fail_fast_when_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin a terminal that matches no capability so the "nothing available"
    # fail-fast path is deterministic regardless of the ambient environment.
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    result = select_captcha_handler(build_pipeline_config())
    assert result.is_err
    assert result.unwrap_err() == (
        "Не удалось определить протокол вывода капчи. Используйте --use-sixel или "
        "--use-kitty, либо настройте openai_captcha в pipeline.yaml."
    )
