"""Captcha handler selection (core logic, architecture §5.4).

Chooses which :data:`CaptchaHandler` to inject for a pipeline based on the
`openai_captcha` config section and the CLI flags. This is **generic core
logic** — board-agnostic; the client that needs a captcha just consumes whatever
handler it is given.

Selection order (documented in jobfucker.architecture.md §5.4, with explicit
CLI flags applied first so a user's deliberate ``--use-sixel``/``--use-kitty``
always wins over a configured AI section):

1. explicit ``--use-sixel`` / ``--use-kitty`` → :class:`TerminalCaptchaHandler`
   with that protocol (mutually exclusive);
2. ``openai_captcha`` configured (and ``--no-captcha-ai`` not set) →
   :class:`AiCaptchaHandler`;
3. terminal auto-detect → :class:`TerminalCaptchaHandler` with the detected
   protocol;
4. else fail fast with the documented Russian message.

``--no-captcha-ai`` forces the terminal path even when ``openai_captcha`` is
configured.
"""

from __future__ import annotations

from rusty_results.prelude import Err, Ok, Result

from jobfucker.captcha.ai import AiCaptchaHandler
from jobfucker.captcha.terminal import detect_terminal_protocol
from jobfucker.captcha.terminal_handlers import TerminalCaptchaHandler
from jobfucker.clients.base import CaptchaHandler
from jobfucker.config import PipelineConfig

__all__ = ["select_captcha_handler"]

# The documented fail-fast message (architecture §5.4, step 4).
_NO_PROTOCOL_MESSAGE = (
    "Не удалось определить протокол вывода капчи. Используйте --use-sixel или "
    "--use-kitty, либо настройте openai_captcha в pipeline.yaml."
)


def select_captcha_handler(
    pipeline: PipelineConfig,
    *,
    use_sixel: bool = False,
    use_kitty: bool = False,
    no_captcha_ai: bool = False,
) -> Result[CaptchaHandler, str]:
    """Select the captcha handler for ``pipeline``.

    Args:
        pipeline: the validated pipeline config (its ``openai_captcha`` section
            drives the AI branch).
        use_sixel: force the terminal sixel handler.
        use_kitty: force the terminal kitty handler.
        no_captcha_ai: disable the AI branch even when ``openai_captcha`` is set.

    Returns:
        ``Ok`` with the selected :data:`CaptchaHandler`, or ``Err`` with the
        fail-fast message when no handler can be chosen.
    """
    if use_sixel and use_kitty:
        return Err("Cannot use both --use-sixel and --use-kitty")

    # Explicit CLI flags win: the user deliberately asked for terminal output,
    # overriding a configured AI section (step order above).
    if use_sixel:
        return Ok(TerminalCaptchaHandler("sixel"))
    if use_kitty:
        return Ok(TerminalCaptchaHandler("kitty"))

    # AI branch: only when openai_captcha is configured and not disabled.
    if not no_captcha_ai and pipeline.openai_captcha is not None:
        return Ok(AiCaptchaHandler(pipeline.openai_captcha))

    # Terminal auto-detect.
    detected = detect_terminal_protocol()
    if detected is not None:
        return Ok(TerminalCaptchaHandler(detected))

    return Err(_NO_PROTOCOL_MESSAGE)
