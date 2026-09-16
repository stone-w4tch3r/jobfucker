"""Terminal captcha output: sixel/kitty encoders + protocol auto-detection.

This module is **generic and board-agnostic** (no board names anywhere). It
implements two image-to-escape-sequence encoders used by
:class:`jobfucker.captcha.terminal_handlers.TerminalCaptchaHandler`, plus
  :func:`detect_terminal_protocol` which decides which of sixel/kitty the user's
  terminal supports by reading ``TERM_PROGRAM``/``TERM`` (plus presence-only
  rules such as ``WT_SESSION``) against the packaged
  ``resources/terminal_capabilities.json``. **Entry order in the JSON matters**:
  detection is first-match, so specific ``TERM_PROGRAM`` entries must precede
  generic ``TERM`` entries (e.g. WezTerm also sets ``TERM=xterm-256color``), and
  presence-detected terminals must precede generic ``TERM`` entries too (both
  ``WT_SESSION`` and ``TERM`` leak into child shells — WSL/ssh — where the
  generic ``TERM=xterm-256color`` entry would wrongly claim sixel support).

- :func:`encode_sixel` — pure-Python SIXEL encoder (no ``libsixel``): opens the
  PNG via Pillow, quantizes to 256 colours, builds the raster header, a 256-entry
  palette and 6-row bands with run-length compression.
- :func:`encode_kitty` — base64 of the raw PNG wrapped in the kitty graphics
  escape protocol.
- :func:`detect_terminal_protocol` — returns ``"kitty"`` (preferred) or
  ``"sixel"`` when the terminal is recognised and supports one, else ``None``.
  ``None`` is ambiguous (unknown terminal *or* known terminal without either
  protocol); :func:`known_unsupported_terminal` disambiguates by returning the
  display name of a recognised-but-protocol-less terminal. When running under
  ``TMUX``/``ZELLIJ`` the *protocol* is still decided by the underlying
  terminal's capabilities; :func:`wrap_sixel` adds the passthrough wrapping at
  render time.

'':'': the documented escape forms (architecture §5.3) are preserved exactly:

- sixel header: ``ESC Pq "1;1;{width};{height}``
- kitty: ``ESC _ G a = T , f = 100 ; {base64} ESC \\``

Expected failures (a PNG that Pillow cannot decode) return ``Result``.
"""

from __future__ import annotations

import base64
import io
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal

from PIL import Image
from pydantic import BaseModel
from rusty_results.prelude import Err, Ok, Result

__all__ = [
    "Protocol",
    "detect_terminal_protocol",
    "encode_kitty",
    "encode_sixel",
    "known_unsupported_terminal",
    "wrap_sixel",
]

# The supported output protocols, in kitty-preferred order (kitty has higher
# fidelity for arbitrary images; sixel is capped at 256 colours).
Protocol = Literal["sixel", "kitty"]

_SIXEL_ST = "\x1b\\"  # String Terminator, closes a DCS sequence.

_CAPABILITIES_PATH: Final[Path] = Path(__file__).parents[1] / "resources" / "terminal_capabilities.json"


# --- Capability map (typed at the JSON boundary) ----------------------------
class _DetectRule(BaseModel):
    """The env-var rule that identifies one terminal.

    ``TERM_PROGRAM``/``TERM`` match by exact value; ``env_set`` is a
    presence-only rule (the named variable must exist with any value) for
    terminals without a reliable identity variable — Windows Terminal is only
    identifiable via ``WT_SESSION`` and sets no ``TERM_PROGRAM``.
    """

    TERM_PROGRAM: str | None = None
    TERM: str | None = None
    env_set: str | None = None


class _Capability(BaseModel):
    """A single terminal's recognised capabilities."""

    detect: _DetectRule
    sixel: bool
    kitty: bool

    def matches(self, environment: Mapping[str, str]) -> bool:
        """Whether this terminal matches the current environment."""
        if self.detect.env_set is not None:
            return environment.get(self.detect.env_set) is not None
        if self.detect.TERM_PROGRAM is not None:
            return environment.get("TERM_PROGRAM") == self.detect.TERM_PROGRAM
        if self.detect.TERM is not None:
            return environment.get("TERM") == self.detect.TERM
        return False


def _load_capabilities(
    capabilities_path: Path | None = None,
) -> dict[str, _Capability]:  # lint-ignore[raw-dict]: terminal capability map
    """Load and validate the terminal capability map from JSON.

    Uses ``pydantic`` at the boundary so the dynamic JSON becomes a typed
    ``dict[str, _Capability]`` (no raw ``dict``/``object`` leaks).
    """
    path = capabilities_path if capabilities_path is not None else _CAPABILITIES_PATH
    raw: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))  # type: ignore[reportAny]  # rationale: json.loads returns object; validated by pydantic below  # lint-ignore[restricted-object]: JSON boundary; validated by pydantic  # lint-ignore[raw-dict]: JSON map
    caps: dict[str, _Capability] = {}
    for name, value in raw.items():
        caps[name] = _Capability.model_validate(value)
    return caps


def _detect(
    env: Mapping[str, str] | None,
    capabilities_path: Path | None,
) -> tuple[Protocol | None, str | None]:
    """First-match detection over the capability map (JSON insertion order).

    Returns ``(protocol, matched_terminal_name)``: ``protocol`` is ``None`` for
    both unknown terminals and known-but-protocol-less ones — the matched name
    disambiguates.
    """
    environment = os.environ if env is None else env
    caps = _load_capabilities(capabilities_path)
    for name, capability in caps.items():
        if not capability.matches(environment):
            continue
        # Prefer kitty over sixel when a terminal supports both (wezterm/contour).
        if capability.kitty:
            return "kitty", name
        if capability.sixel:
            return "sixel", name
        return None, name
    return None, None


def detect_terminal_protocol(
    *,
    env: Mapping[str, str] | None = None,
    capabilities_path: Path | None = None,
) -> Protocol | None:
    """Detect the terminal's captcha output protocol.

    Args:
        env: optional environment mapping (defaults to ``os.environ``). Injected
            so tests can control ``TERM_PROGRAM``/``TERM``/``WT_SESSION``/``TMUX``.
        capabilities_path: optional path to the capability JSON (defaults to the
            packaged ``resources/terminal_capabilities.json``).

    Returns:
        ``"kitty"`` (preferred when both are supported), ``"sixel"``, or
        ``None`` when the terminal is not recognised **or** is recognised but
        supports neither protocol. Use :func:`known_unsupported_terminal` to
        tell those two cases apart.

    Ordering contract: capabilities are checked in JSON insertion order and the
    first match wins — keep specific ``TERM_PROGRAM``-detected entries before
    presence-only (``env_set``) entries, and those before generic ``TERM``-detected
    ones (many terminals ship ``TERM=xterm-256color``, and both ``TERM`` and
    ``WT_SESSION`` leak into child shells).
    """
    protocol, _name = _detect(env, capabilities_path)
    return protocol


def known_unsupported_terminal(
    *,
    env: Mapping[str, str] | None = None,
    capabilities_path: Path | None = None,
) -> str | None:
    """Name of the recognised terminal that supports neither sixel nor kitty.

    Companion to :func:`detect_terminal_protocol`: where that returns ``None``,
    this returns the terminal's display name when it *was* recognised (e.g.
    ``"Windows Terminal"``, ``"Apple Terminal"``) so callers can explain why,
    and ``None`` for a fully unknown environment.

    Args mirror :func:`detect_terminal_protocol` (``env``, ``capabilities_path``).
    """
    protocol, name = _detect(env, capabilities_path)
    return None if protocol is not None else name


def wrap_sixel(sixel: str, *, env: Mapping[str, str] | None = None) -> str:
    """Wrap a SIXEL sequence for passthrough under ``TMUX``/``ZELLIJ``.

    The underlying terminal's capabilities still decide *whether* sixel is
    supported; this only adds the multiplexer passthrough wrapper at render time
    so the outer terminal receives the raw DCS sequence.

    Args:
        sixel: a complete SIXEL DCS sequence from :func:`encode_sixel`.
        env: optional environment mapping (defaults to ``os.environ``).

    Returns:
        The sequence, wrapped when a multiplexer is present, unchanged otherwise.
    """
    environment = os.environ if env is None else env
    # TMUX bracketed passthrough: open `ESC P tmux; ESC`, then the inner DCS,
    # then two STs (one closes the inner DCS, one closes the passthrough).
    if environment.get("TMUX") is not None:
        return f"\x1bPtmux;\x1b{sixel}{_SIXEL_ST}{_SIXEL_ST}"
    # Zellij passthrough: `ESC Pz; ESC <data> ESC \` (best-effort wrapper).
    if environment.get("ZELLIJ") is not None:
        return f"\x1bPz;\x1b{sixel}{_SIXEL_ST}"
    return sixel


# --- SIXEL encoding ---------------------------------------------------------
# SIXEL rows are 6 pixels tall; a byte's 6 bits set the 6 rows at a column.
_SIXEL_BAND_HEIGHT: Final = 6
_SIXEL_COLORS: Final = 256
_RGB_CHANNELS: Final = 3  # palette is a flat R,G,B triple stream
_RLE_REPEAT_THRESHOLD: Final = 3  # only runs of >= 3 equal bytes use `!N` compression


def _sixel_palette(palette: Sequence[int]) -> str:
    """Emit the 256-entry palette ``#i;2;R;G;B`` prefix for the image."""
    parts: list[str] = []
    for i in range(_SIXEL_COLORS):
        r = palette[i * _RGB_CHANNELS]
        g = palette[i * _RGB_CHANNELS + 1]
        b = palette[i * _RGB_CHANNELS + 2]
        parts.append(f"#{i};2;{r};{g};{b}")
    return "".join(parts)


def _rle(masks: Sequence[int]) -> str:
    """Run-length compress a row of 6-bit masks into SIXEL bytes + ``!N`` repeats.

    Each value is a 0..63 6-bit mask for one column; the SIXEL byte is
    ``chr(63 + value)`` (``0x3f..0x7e``). Runs of >= 3 equal bytes use the
    ``!count`` compression form.
    """
    parts: list[str] = []
    i = 0
    n = len(masks)
    while i < n:
        value = masks[i]
        j = i
        while j < n and masks[j] == value:
            j += 1
        run = j - i
        char = chr(63 + value)
        if run >= _RLE_REPEAT_THRESHOLD:
            parts.append(f"!{run}{char}")
        else:
            parts.append(char * run)
        i = j
    return "".join(parts)


def _sixel_body(data: Sequence[int], width: int, height: int) -> str:
    """Encode the image in 6-row bands with per-colour run-length data.

    ``data`` is the flattened palette-index list from ``Image.getdata()``
    (length ``width * height``, row-major). Each band overlays the colour planes
    at the same 6 rows (``$`` returns the cursor to column 0 of the band after
    each plane), then ``-`` moves to the next band.
    """
    bands: list[str] = []
    for y0 in range(0, height, _SIXEL_BAND_HEIGHT):
        band_height = min(_SIXEL_BAND_HEIGHT, height - y0)
        streams: dict[int, list[int]] = {}
        for x in range(width):
            for row in range(band_height):
                color = data[(y0 + row) * width + x]  # palette index (int for 'P' mode)
                if color not in streams:
                    streams[color] = [0] * width
                streams[color][x] |= 1 << row
        band_parts: list[str] = []
        for color in range(_SIXEL_COLORS):
            masks = streams.get(color)
            if masks is None:
                continue
            band_parts.append(f"#{color}")
            band_parts.append(_rle(masks))
            band_parts.append("$")
        band_parts.append("-")
        bands.append("".join(band_parts))
    return "".join(bands)


def encode_sixel(png_bytes: bytes) -> Result[str, str]:
    """Encode a PNG into a pure-Python SIXEL DCS sequence.

    Args:
        png_bytes: the raw PNG bytes of the captcha image.

    Returns:
        ``Ok`` with the SIXEL sequence (``ESC Pq`` ... ``ESC \\``), or ``Err``
        when the bytes are not a decodable image.
    """
    try:
        image = Image.open(io.BytesIO(png_bytes))
        image = image.convert("RGB")
        quantized = image.quantize(colors=_SIXEL_COLORS, method=Image.Quantize.MAXCOVERAGE)
        width, height = quantized.size
        palette = quantized.getpalette()
        if palette is None:
            raise ValueError("quantized image has no palette")
        # For a 'P'-image, `tobytes()` is the raw 1-byte-per-pixel palette index
        # stream — a fully-typed way to read pixel indices (no untyped load()).
        data = list(quantized.tobytes())
        if len(data) != width * height:
            raise ValueError("unexpected pixel buffer size")
    except Exception as exc:
        return Err(f"Could not decode PNG for sixel: {exc}")

    header = f'\x1bPq"1;1;{width};{height}'
    try:
        palette_text = _sixel_palette(palette)
        body = _sixel_body(data, width, height)
    except Exception as exc:
        return Err(f"Could not encode sixel: {exc}")
    return Ok(header + palette_text + body + _SIXEL_ST)


# --- KITTY encoding ---------------------------------------------------------
def encode_kitty(png_bytes: bytes) -> Result[str, str]:
    """Encode a PNG into the kitty graphics escape protocol.

    Args:
        png_bytes: the raw PNG bytes of the captcha image.

    Returns:
        ``Ok`` with the kitty sequence ``ESC _ G a=T,f=100;{base64} ESC \\``.
    """
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return Ok(f"\x1b_Ga=T,f=100;{encoded}{_SIXEL_ST}")
