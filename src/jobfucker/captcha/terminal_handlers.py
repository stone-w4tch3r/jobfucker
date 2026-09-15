"""Terminal captcha handler: prints the image and reads a manual answer.

Implements the shared contract :data:`CaptchaHandler`
(``Callable[[bytes], Awaitable[Result[str, str]]]``): it takes the PNG bytes of
a captcha, prints the image to the terminal (sixel or kitty, per the chosen
protocol), prompts the human for the recognised text and returns it.

The handler is **async** (per the async-migration spec — the captcha seam is
awaitable like every other blocking interaction). The blocking ``typer.echo`` /
``typer.prompt`` calls run off the loop via ``asyncio.to_thread`` so a live
qasync / asyncio loop never freezes while a human reads the image and types an
answer.

Decoding/validation of the PNG goes through Pillow shared with the encoders;
the sixel sequence is additionally wrapped in ``TMUX``/``ZELLIJ`` passthrough
at render time. The ``typer.echo`` / ``typer.prompt`` seams are injectable so
tests can drive the handler without a real TTY.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import typer
from rusty_results.prelude import Err, Ok, Result
from typer import Abort

from jobfucker.captcha.terminal import Protocol, encode_kitty, encode_sixel, wrap_sixel

__all__ = ["TerminalCaptchaHandler"]


# Shown after the image so a human who does not see it knows why.
_HELPER_TEXT = "Don't see image? Check your terminal supports sixel/kitty graphics."
# No trailing suffix: typer.prompt appends ": " itself, so the seam (and the
# on-screen prompt) show the same text either way.
_PROMPT = "Введите текст с картинки"


def _prompt_text(text: str) -> str:
    """A ``str``-typed :func:`typer.prompt` (typer declares its return as ``Any``).

    One-line answer read from the TTY (or ``Abort`` on EOF/Ctrl+C).
    """
    return typer.prompt(text)  # type: ignore[reportAny]  # rationale: typer.prompt declares Any; one-line TTY read


class TerminalCaptchaHandler:
    """A :data:`CaptchaHandler` that displays the captcha in the terminal.

    Args:
        protocol: which escape protocol to render (``"sixel"`` or ``"kitty"``).
        output: an echo-like callable (defaults to ``typer.echo``); injectable
            for tests.
        input_callback: a prompt-like callable (defaults to
            :func:`_prompt_text`, i.e. ``typer.prompt``); injectable for tests.
    """

    def __init__(
        self,
        protocol: Protocol,
        *,
        output: Callable[[str], None] | None = None,
        input_callback: Callable[[str], str] | None = None,
    ) -> None:
        self._protocol: Protocol = protocol
        self._output: Callable[[str], None] = output if output is not None else typer.echo
        self._input_callback: Callable[[str], str] = input_callback if input_callback is not None else _prompt_text

    async def __call__(self, image: bytes) -> Result[str, str]:
        """Render the captcha image and return the human-entered text.

        Args:
            image: the PNG bytes of the captcha.

        Returns:
            ``Ok`` with the entered text, or ``Err`` when the image cannot be
            encoded, terminal I/O fails (closed pipe), input is cancelled
            (EOF), or the entered text is empty.
        """
        if self._protocol == "sixel":
            encoded = encode_sixel(image)
            if encoded.is_err:
                return Err(encoded.unwrap_err())
            rendered = wrap_sixel(encoded.unwrap())
        else:
            encoded = encode_kitty(image)
            if encoded.is_err:
                return Err(encoded.unwrap_err())
            rendered = encoded.unwrap()

        # Blocking human-interaction I/O must not freeze the event loop: run the
        # print + input on a worker thread (async-migration D4 — handlers remain
        # awaitable; the blocking seam stays inside the implementation).
        try:
            await asyncio.to_thread(lambda: self._output(rendered))
            await asyncio.to_thread(lambda: self._output(_HELPER_TEXT))
            text = await asyncio.to_thread(lambda: self._input_callback(_PROMPT))
        except EOFError, Abort:
            # EOF (Ctrl-D on an empty line / a closed stdin) or Ctrl+C cancel
            # input: the human gave no answer. The builtin ``input()`` seam
            # raises EOFError; the default ``typer.prompt`` seam raises
            # typer's ``Abort`` for both EOF and Ctrl+C — the handler converts
            # either to Err.
            return Err("captcha input ended (EOF); no answer provided")
        except OSError as exc:
            # Terminal write/read failures (closed pipe, vanished stdin) are
            # expected boundary failures → Result, not an exception
            # (client-contract §7.1). CancelledError stays uncaught so a
            # cancelled task keeps propagating; Ctrl+C is swallowed by
            # typer.prompt into Abort and handled above as a cancelled input.
            return Err(f"captcha terminal I/O failed: {exc}")
        answer = text.strip()
        if not answer:
            # An empty answer is "no answer provided"; Ok("") upstream would be
            # treated as a solved captcha (consumers only check is_err).
            return Err("captcha: no answer provided (empty input)")
        return Ok(answer)
