"""Captcha terminal encoders + protocol detection + terminal handler seam.

Proves the sixel/kitty encoders produce the documented, deterministic escape
sequences (pure Python, no libsixel), that :func:`detect_terminal_protocol`
matches ``data/terminal_capabilities.json`` (incl. tmux/zellij passthrough
wrapping on the sixel output), and that the async
:class:`TerminalCaptchaHandler` renders + prompts through its injected
``typer.echo``/``typer.prompt``-shaped seams off the event loop (the
async-migration-touched path).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from typer import Abort

from jobfucker.captcha.terminal import (
    Protocol,
    detect_terminal_protocol,
    encode_kitty,
    encode_sixel,
    known_unsupported_terminal,
    wrap_sixel,
)
from jobfucker.captcha.terminal_handlers import TerminalCaptchaHandler

_CAPTCHA_PNG = Path(__file__).resolve().parent.parent / "fixtures" / "captcha.png"


def _png_bytes() -> bytes:
    return _CAPTCHA_PNG.read_bytes()


# --- sixel encoder ----------------------------------------------------------
def test_encode_sixel_produces_documented_escape() -> None:
    result = encode_sixel(_png_bytes())
    assert result.is_ok
    sixel = result.unwrap()
    # Raster header: ESC Pq "1;1;{width};{height}
    assert sixel.startswith('\x1bPq"1;1;')
    assert ";" in sixel[len('\x1bPq"1;1;') : len('\x1bPq"1;1;') + 10]
    # DCS String Terminator closes the sequence.
    assert sixel.endswith("\x1b\\")
    # A 256-entry palette `#i;2;R;G;B` is present (architecture §5.3).
    assert "#0;2;" in sixel
    # 6-row band rendering uses RLE `!N` compression somewhere for this image.
    assert "#" in sixel


def test_encode_sixel_is_deterministic() -> None:
    first = encode_sixel(_png_bytes()).unwrap()
    second = encode_sixel(_png_bytes()).unwrap()
    assert first == second


def test_encode_sixel_rejects_invalid_image() -> None:
    result = encode_sixel(b"not a png")
    assert result.is_err
    assert "decode" in result.unwrap_err().lower()


# --- kitty encoder ----------------------------------------------------------
def test_encode_kitty_uses_raw_png_base64() -> None:
    result = encode_kitty(_png_bytes())
    assert result.is_ok
    kitty = result.unwrap()
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    assert kitty == f"\x1b_Ga=T,f=100;{payload}\x1b\\"


# --- protocol detection -----------------------------------------------------
def test_detect_prefers_kitty_when_both_supported() -> None:
    # wezterm supports both sixel and kitty; kitty must win (§5.3 preference).
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "WezTerm"}) == "kitty"
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "contour"}) == "kitty"


def test_detect_kitty_only_terminals() -> None:
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "kitty"}) == "kitty"
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "ghostty"}) == "kitty"
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "konsole"}) == "kitty"


def test_detect_sixel_only_terminals() -> None:
    assert detect_terminal_protocol(env={"TERM": "foot"}) == "sixel"
    assert detect_terminal_protocol(env={"TERM": "xterm"}) == "sixel"
    assert detect_terminal_protocol(env={"TERM": "xterm-256color"}) == "sixel"


def test_detect_term_program_entries_outrank_generic_term_entries() -> None:
    # WezTerm/Contour default to TERM=xterm-256color; the generic TERM entry
    # must not shadow the more specific TERM_PROGRAM entry (kitty preferred).
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "WezTerm", "TERM": "xterm-256color"}) == "kitty"
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "contour", "TERM": "xterm-256color"}) == "kitty"


def test_detect_unknown_environment() -> None:
    assert detect_terminal_protocol(env={}) is None
    assert detect_terminal_protocol(env={"TERM": "something-else", "TERM_PROGRAM": "unknown"}) is None


def test_detect_known_unsupported_windows_terminal() -> None:
    # Windows Terminal is only identifiable via WT_SESSION presence (it sets no
    # reliable TERM_PROGRAM) and supports neither sixel nor kitty.
    env = {"WT_SESSION": "some-guid"}
    assert detect_terminal_protocol(env=env) is None
    assert known_unsupported_terminal(env=env) == "Windows Terminal"


def test_detect_known_unsupported_apple_terminal() -> None:
    env = {"TERM_PROGRAM": "Apple_Terminal"}
    assert detect_terminal_protocol(env=env) is None
    assert known_unsupported_terminal(env=env) == "Apple Terminal"


def test_known_unsupported_entries_outrank_generic_term_entries() -> None:
    # Both TERM and WT_SESSION leak into child shells (WSL/ssh): without the
    # specific entries first, the generic TERM=xterm-256color entry would
    # wrongly claim sixel support for Windows Terminal / Terminal.app.
    windows = {"WT_SESSION": "some-guid", "TERM": "xterm-256color"}
    assert detect_terminal_protocol(env=windows) is None
    assert known_unsupported_terminal(env=windows) == "Windows Terminal"
    apple = {"TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-256color"}
    assert detect_terminal_protocol(env=apple) is None
    assert known_unsupported_terminal(env=apple) == "Apple Terminal"


def test_known_unsupported_terminal_is_none_without_a_known_terminal() -> None:
    # The name function must stay None both for protocol-capable terminals and
    # for unknown environments — it only names recognised-but-unsupported ones.
    assert known_unsupported_terminal(env={"TERM_PROGRAM": "WezTerm"}) is None
    assert known_unsupported_terminal(env={}) is None


def test_detect_ignores_multiplexer_for_protocol_choice(tmp_path: Path) -> None:
    # TMUX/ZELLIJ presence must NOT change which protocol the underlying
    # terminal supports — only rendering wraps the sequence for passthrough.
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "WezTerm", "TMUX": "0"}) == "kitty"
    assert detect_terminal_protocol(env={"TERM": "foot", "ZELLIJ": "0"}) == "sixel"


def test_detect_loads_custom_capabilities_file(tmp_path: Path) -> None:
    caps = tmp_path / "terminal_capabilities.json"
    caps.write_text(
        json.dumps(
            {
                "myterm": {"detect": {"TERM_PROGRAM": "MyTerm"}, "sixel": True, "kitty": False},
            }
        ),
        encoding="utf-8",
    )
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "MyTerm"}, capabilities_path=caps) == "sixel"
    assert detect_terminal_protocol(env={"TERM_PROGRAM": "Missing"}, capabilities_path=caps) is None


# --- tmux/zellij passthrough ------------------------------------------------
def test_wrap_sixel_tmux() -> None:
    wrapped = wrap_sixel("DCSBODY", env={"TMUX": "0"})
    assert wrapped.startswith("\x1bPtmux;\x1b")
    assert wrapped.endswith("\x1b\\\x1b\\")
    assert "DCSBODY" in wrapped


def test_wrap_sixel_zellij() -> None:
    wrapped = wrap_sixel("DCSBODY", env={"ZELLIJ": "0"})
    assert wrapped.startswith("\x1bPz;\x1b")
    assert wrapped.endswith("\x1b\\")
    assert "DCSBODY" in wrapped


def test_wrap_sixel_no_multiplexer_is_unchanged() -> None:
    assert wrap_sixel("DCSBODY", env={}) == "DCSBODY"


# --- terminal handler (`__call__` seam) -------------------------------------
# The handler is async (async-migration): `await`ing it runs the injected
# echo/prompt seams through `asyncio.to_thread` (a real worker thread), so the
# tests below exercise the actual off-loop blocking seam, not a stubbed await.
_HELPER_TEXT = "Don't see image? Check your terminal supports sixel/kitty graphics."
_PROMPT = "Введите текст с картинки"


def _make_handler(
    protocol: Protocol,
    answer: str | Exception = "abc",
) -> tuple[TerminalCaptchaHandler, list[str], list[str]]:
    """Build a handler with captured output and a canned/raising input callback."""

    outputs: list[str] = []
    prompts: list[str] = []

    def output(text: str) -> None:
        outputs.append(text)

    def input_callback(prompt: str) -> str:
        prompts.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return TerminalCaptchaHandler(protocol, output=output, input_callback=input_callback), outputs, prompts


async def test_handler_sixel_renders_sequence_and_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic render: strip TMUX/ZELLIJ so wrap_sixel leaves the DCS
    # sequence unwrapped (passthrough wrapping is environment-driven).
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    handler, outputs, prompts = _make_handler("sixel")

    result = await handler(_png_bytes())

    assert result.is_ok
    assert result.unwrap() == "abc"
    assert outputs == [
        encode_sixel(_png_bytes()).unwrap(),
        _HELPER_TEXT,
    ]
    assert prompts == [_PROMPT]


async def test_handler_kitty_renders_unwrapped_sequence_and_prompts() -> None:
    handler, outputs, prompts = _make_handler("kitty")

    result = await handler(_png_bytes())

    assert result.is_ok
    assert result.unwrap() == "abc"
    # Kitty is a raw base64 passthrough: no wrap_sixel wrapper is applied.
    assert outputs == [
        encode_kitty(_png_bytes()).unwrap(),
        _HELPER_TEXT,
    ]
    assert prompts == [_PROMPT]


async def test_handler_eof_returns_err() -> None:
    # EOFError from input() propagates out of the worker thread back into the
    # await, where the handler's `except EOFError` converts it to Err.
    handler, outputs, prompts = _make_handler("sixel", answer=EOFError())

    result = await handler(_png_bytes())

    assert result.is_err
    assert "EOF" in result.unwrap_err()
    assert outputs == [encode_sixel(_png_bytes()).unwrap(), _HELPER_TEXT]
    assert prompts == [_PROMPT]


async def test_handler_typer_abort_returns_err() -> None:
    # Production-faithful cancel path: the default seam is ``typer.prompt``,
    # which raises typer's ``Abort`` on EOF/Ctrl+C — the handler must convert
    # it to Err the same way it converts EOFError.
    handler, outputs, prompts = _make_handler("sixel", answer=Abort())

    result = await handler(_png_bytes())

    assert result.is_err
    assert "EOF" in result.unwrap_err()
    assert outputs == [encode_sixel(_png_bytes()).unwrap(), _HELPER_TEXT]
    assert prompts == [_PROMPT]


async def test_handler_sixel_rejects_undecodable_image() -> None:
    handler, outputs, prompts = _make_handler("sixel")

    result = await handler(b"not a png")

    assert result.is_err
    assert "decode" in result.unwrap_err().lower()
    # Failed before any output/prompt: nothing was rendered or asked.
    assert outputs == []
    assert prompts == []


async def test_handler_kitty_passes_undecodable_bytes_through() -> None:
    # Documented divergence: the kitty path never decodes the PNG (raw base64
    # passthrough), so garbage bytes still reach the prompt, unlike sixel.
    handler, outputs, prompts = _make_handler("kitty")

    result = await handler(b"not a png")

    assert result.is_ok
    assert result.unwrap() == "abc"
    assert outputs[0] == encode_kitty(b"not a png").unwrap()
    assert prompts == [_PROMPT]


async def test_handler_strips_surrounding_whitespace() -> None:
    handler, _, _ = _make_handler("sixel", answer="  abc  ")

    result = await handler(_png_bytes())

    assert result.is_ok
    assert result.unwrap() == "abc"


async def test_handler_empty_answer_is_err() -> None:
    # An empty answer is "no answer provided" — a success with no text would be
    # treated as a solved captcha upstream (mock client only checks is_err).
    handler, _, _ = _make_handler("sixel", answer="")

    result = await handler(_png_bytes())

    assert result.is_err
    assert "no answer" in result.unwrap_err()


async def test_handler_output_failure_returns_err() -> None:
    # A terminal write failure (closed pipe) is an expected boundary failure →
    # Result, not an exception (client-contract §7.1).
    def failing_output(text: str) -> None:
        raise BrokenPipeError("Broken pipe")

    handler = TerminalCaptchaHandler("sixel", output=failing_output, input_callback=lambda prompt: "abc")

    result = await handler(_png_bytes())

    assert result.is_err
    assert "terminal I/O" in result.unwrap_err()
