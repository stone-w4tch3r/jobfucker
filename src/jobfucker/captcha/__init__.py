"""Generic, board-agnostic captcha handling (owning client track-independence).

This package implements the shared contract's :data:`CaptchaHandler` seam:
receiving captcha PNG bytes, displaying (sixel/kitty terminal image) or solving
(AI vision), and returning the recognised text. It is reused by any client that
needs captcha; each board's *trigger detection* lives in that board's client.

Public surface (re-exported so callers import everything from
``jobfucker.captcha``):

- :func:`encode_sixel`, :func:`encode_kitty`, :func:`detect_terminal_protocol`
- :class:`TerminalCaptchaHandler`
- :class:`AiCaptchaHandler`
- :func:`select_captcha_handler`
"""

from __future__ import annotations

from jobfucker.captcha.ai import AiCaptchaHandler, CaptchaVisionFn, CaptchaVisionRequest
from jobfucker.captcha.selector import select_captcha_handler
from jobfucker.captcha.terminal import (
    Protocol,
    detect_terminal_protocol,
    encode_kitty,
    encode_sixel,
    wrap_sixel,
)
from jobfucker.captcha.terminal_handlers import TerminalCaptchaHandler

__all__ = [
    "AiCaptchaHandler",
    "CaptchaVisionFn",
    "CaptchaVisionRequest",
    "Protocol",
    "TerminalCaptchaHandler",
    "detect_terminal_protocol",
    "encode_kitty",
    "encode_sixel",
    "select_captcha_handler",
    "wrap_sixel",
]
