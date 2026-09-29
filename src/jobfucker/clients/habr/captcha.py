"""Board-owned SmartCaptcha protocol for the Habr Account login step.

Habr's login form is gated by Yandex SmartCaptcha on ``account.habr.com``. Two
solvers are available:

- :class:`jobfucker.clients.habr.browser.BrowserClickSolver` — the primary path:
  drive the shared stealth browser to a real checkbox click (no AI cost).
- :class:`HttpVisionSolver` — the browserless fallback: walk the observed
  ``/check`` ladder (minimal → checkbox → image), OCR the image through the
  injected :data:`CaptchaHandler`, solve the proof-of-work, and submit the answer.

:class:`LoginCaptcha` wires them browser-first, vision-fallback. The ladder is
verified against live Habr (docs/habr/captcha.md); the protocol constants are
board-owned and never leak into the board-neutral layers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from time import monotonic
from typing import Final

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaHandler, CaptchaSolvingError, ClientError, ProtocolError
from jobfucker.clients.habr.browser import BrowserClickSolver
from jobfucker.clients.habr.models import HabrCaptchaResponse, HabrPow
from jobfucker.clients.habr.transport import SMARTCAPTCHA_ORIGIN, HabrTransport, decode_json
from jobfucker.clients.shared.transport import header_value
from jobfucker.reporting import NullReporter, Reporter, RunEvent

__all__ = [
    "BrowserClickSolver",
    "HttpVisionSolver",
    "LoginCaptcha",
    "PowSolution",
    "decode_image_loader",
    "encode_pdata",
    "solve_pow",
]

CAPTCHA_HOST: Final = "account.habr.com"
_CHECK_URL: Final = f"{SMARTCAPTCHA_ORIGIN}/check"

_POW_NONCE_BYTES: Final = 16
# A 16-byte nonce makes the proof-of-work search cost ~2**complexity hashes; the
# board sends ~10, so a cap of 20 (≈1 s worst case at ~1.5M hashes/s) leaves ample
# headroom while keeping a hostile/garbled challenge from spinning the solver for
# minutes (2**32 would be ~47 min). A wall-clock deadline backstops the cap so a
# pathological input can never spin unbounded (unsolvable instead of hanging).
_MAX_POW_COMPLEXITY: Final = 20
_POW_DEADLINE_S: Final = 10.0
# Check the deadline every N trials: a per-trial monotonic() call would dominate
# the hash loop at 1.5M trials/s.
_POW_DEADLINE_CHECK_INTERVAL: Final = 4096
_HTTP_OK: Final = 200
_MAX_ANSWER_CHARACTERS: Final = 64
_IMAGE_LOADER_ENCODING_ERRORS: Final = (ValueError, UnicodeDecodeError)

# Base form body every /check step repeats (verified against live Habr).
_BASE_FIELDS: Final = (("lang", "ru"), ("test", "false"), ("webview", "false"))
_CHECK_HEADERS: Final = (
    ("origin", SMARTCAPTCHA_ORIGIN),
    ("referer", f"{SMARTCAPTCHA_ORIGIN}/"),
)


@dataclass(frozen=True, slots=True)
class PowSolution:
    """A solved SmartCaptcha proof-of-work: a 16-byte nonce (hex) and its cost."""

    nonce: str  # 32 lowercase hex chars
    calc_time_ms: int


def _leading_zero_bits(digest: bytes) -> int:
    """Count the leading zero bits of a SHA-256 digest."""
    count = 0
    for byte in digest:
        if byte == 0:
            count += 8
            continue
        count += 8 - byte.bit_length()
        break
    return count


def solve_pow(prefix: str, complexity: int) -> PowSolution | None:
    """Find a 16-byte nonce so ``sha256(prefix ++ nonce)`` has ``complexity`` leading zero bits.

    Returns ``None`` when the prefix is not valid hex (an upstream protocol
    violation the caller maps to a ``ProtocolError``), when ``complexity``
    exceeds :data:`_MAX_POW_COMPLEXITY` (an unsolvable, potentially hanging
    challenge the caller maps to a ``CaptchaSolvingError``), or when the
    :data:`_POW_DEADLINE_S` wall-clock budget is exhausted (same mapping);
    never raises.
    """
    if complexity > _MAX_POW_COMPLEXITY:
        return None
    try:
        prefix_bytes = bytes.fromhex(prefix)
    except ValueError:
        return None
    started = monotonic()
    max_nonce = 1 << (_POW_NONCE_BYTES * 8)
    for nonce_int in range(max_nonce):
        if nonce_int % _POW_DEADLINE_CHECK_INTERVAL == 0 and monotonic() - started >= _POW_DEADLINE_S:
            return None
        nonce = nonce_int.to_bytes(_POW_NONCE_BYTES, "big")
        digest = hashlib.sha256(prefix_bytes + nonce).digest()
        if _leading_zero_bits(digest) >= complexity:
            elapsed_ms = max(1, int((monotonic() - started) * 1000))
            return PowSolution(nonce=nonce.hex(), calc_time_ms=elapsed_ms)
    return None


def encode_pdata(solution: PowSolution, prefix: str) -> str:
    """Encode the browser's ``pdata`` field: base64url(compact JSON), no padding."""
    payload = {
        "powNonce": solution.nonce,
        "powCalcTime": solution.calc_time_ms,
        "powPrefix": prefix,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_image_loader(loader: str) -> str | None:
    """Decode the ``captcha.image`` loader into the real image URL, or ``None``.

    The widget wraps the image URL as ``…/load-captchaimg?<base64url>,<more>``;
    the real URL lives in the decoded query segment (docs/habr/captcha.md). A
    plain ``http(s)`` URL is passed through unchanged.
    """
    if "?" not in loader:
        return loader if loader.startswith("http") else None
    encoded = loader.split("?", 1)[1].split(",", 1)[0]
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    except _IMAGE_LOADER_ENCODING_ERRORS:
        return None
    return decoded if decoded.startswith("http") else None


class HttpVisionSolver:
    """Walk the browserless SmartCaptcha ladder, OCR the image, submit the answer."""

    def __init__(self, transport: HabrTransport, handler: CaptchaHandler, *, max_attempts: int) -> None:
        self._transport = transport
        self._handler = handler
        self._max_attempts = max_attempts

    async def solve(self, login_url: str, sitekey: str) -> Result[str, ClientError]:
        """Return the ``spravka`` token to write into the login form's ``smart-token``."""
        first_result = await self._check(login_url, sitekey, ())
        if first_result.is_err:
            return Err(first_result.unwrap_err())
        challenge = first_result.unwrap()
        token = _spravka(challenge)
        if token is not None:
            return Ok(token)
        escalated = await self._reach_image_challenge(login_url, sitekey, challenge)
        if escalated.is_err:
            return Err(escalated.unwrap_err())
        return await self._solve_images(login_url, sitekey, escalated.unwrap())

    async def _reach_image_challenge(  # noqa: PLR0911 - a Result cascade: each guard returns early
        self, login_url: str, sitekey: str, challenge: HabrCaptchaResponse
    ) -> Result[HabrCaptchaResponse, ClientError]:
        """Escalate a checkbox challenge to an image challenge (or reject it)."""
        if _is_image_challenge(challenge):
            return Ok(challenge)
        if not _is_checkbox_challenge(challenge):
            return _unsolved(login_url, "SmartCaptcha returned an unknown challenge type")
        pow_challenge = challenge.pow
        captcha = challenge.captcha
        if pow_challenge is None or captcha is None:
            return _unsolved(login_url, "SmartCaptcha checkbox challenge carried no proof-of-work")
        solution_result = _solve_pow_field(pow_challenge, login_url)
        if solution_result.is_err:
            return Err(solution_result.unwrap_err())
        solution = solution_result.unwrap()
        escalation_result = await self._check(
            login_url, sitekey, (("key", captcha.key), ("pdata", encode_pdata(solution, pow_challenge.prefix)))
        )
        if escalation_result.is_err:
            return Err(escalation_result.unwrap_err())
        escalation = escalation_result.unwrap()
        token = _spravka(escalation)
        if token is not None:
            # The checkbox was accepted server-side without an image; the image
            # loop's spravka guard returns the token.
            return Ok(escalation)
        if not _is_image_challenge(escalation):
            return _unsolved(login_url, "SmartCaptcha escalation did not produce an image")
        return Ok(escalation)

    async def _solve_images(  # noqa: PLR0911 - a Result cascade: each guard returns early
        self, login_url: str, sitekey: str, challenge: HabrCaptchaResponse
    ) -> Result[str, ClientError]:
        """OCR and answer fresh image challenges, bounded by ``max_attempts``."""
        token = _spravka(challenge)
        if token is not None:
            return Ok(token)
        for _ in range(self._max_attempts):
            captcha = challenge.captcha
            pow_challenge = challenge.pow
            if captcha is None or captcha.image is None or pow_challenge is None:
                return _unsolved(login_url, "SmartCaptcha image challenge is incomplete")
            image_url = decode_image_loader(captcha.image)
            if image_url is None:
                return Err(ProtocolError(message="SmartCaptcha image loader is malformed"))
            image_result = await self._fetch_image(image_url)
            if image_result.is_err:
                return Err(image_result.unwrap_err())
            answer_result = await self._handler(image_result.unwrap())
            if answer_result.is_err:
                return Err(CaptchaSolvingError(message=answer_result.unwrap_err(), recovery_url=login_url))
            answer = _validated_answer(answer_result.unwrap())
            if answer is None:
                return _unsolved(login_url, "SmartCaptcha answer is empty or too long")
            solution_result = _solve_pow_field(pow_challenge, login_url)
            if solution_result.is_err:
                return Err(solution_result.unwrap_err())
            solution = solution_result.unwrap()
            submit_result = await self._check(
                login_url,
                sitekey,
                (("key", captcha.key), ("rep", answer), ("pdata", encode_pdata(solution, pow_challenge.prefix))),
            )
            if submit_result.is_err:
                return Err(submit_result.unwrap_err())
            submitted = submit_result.unwrap()
            token = _spravka(submitted)
            if token is not None:
                return Ok(token)
            if not _is_image_challenge(submitted):
                return _unsolved(login_url, "SmartCaptcha rejected the answer without a fresh challenge")
            challenge = submitted
        return _unsolved(login_url, "SmartCaptcha image attempts exhausted")

    async def _check(
        self, login_url: str, sitekey: str, extra_fields: tuple[tuple[str, str], ...]
    ) -> Result[HabrCaptchaResponse, ClientError]:
        """POST one ``/check`` ladder step and decode its JSON."""
        params = (("host", CAPTCHA_HOST), ("sitekey", sitekey), ("href", login_url))
        fields = (("sitekey", sitekey), *_BASE_FIELDS, *extra_fields)
        response_result = await self._transport.post_form(
            _CHECK_URL, fields=fields, params=params, headers=_CHECK_HEADERS
        )
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(ProtocolError(message="SmartCaptcha check failed", status=response.status_code))
        return decode_json(response, HabrCaptchaResponse, operation="SmartCaptcha check")

    async def _fetch_image(self, image_url: str) -> Result[bytes, ClientError]:
        """Download one challenge image and validate it is an image."""
        response_result = await self._transport.get_absolute(image_url)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(ProtocolError(message="SmartCaptcha image is unavailable", status=response.status_code))
        content_type = header_value(response, "content-type") or ""
        if not content_type.startswith("image/"):
            return Err(ProtocolError(message="SmartCaptcha image is not an image", status=response.status_code))
        return Ok(response.content)


class LoginCaptcha:
    """Browser-first, vision-fallback login-captcha coordinator."""

    def __init__(
        self,
        *,
        browser_solver: BrowserClickSolver,
        vision_solver: HttpVisionSolver,
        reporter: Reporter | None = None,
    ) -> None:
        self._browser_solver = browser_solver
        self._vision_solver = vision_solver
        self._reporter = reporter if reporter is not None else NullReporter()

    async def solve(self, login_url: str, sitekey: str) -> Result[str, ClientError]:
        """Solve the login challenge, preferring the browser click over vision."""
        engine = await self._browser_solver.ensure_engine()
        if engine.is_ok:
            browser_result = await self._browser_solver.solve(login_url)
            if browser_result.is_ok:
                return browser_result
            await self._reporter.publish(
                RunEvent(
                    stage="captcha",
                    message="Habr browser captcha unavailable; falling back to the browserless vision solver",
                    level="warning",
                    url=login_url,
                )
            )
        else:
            # The primary (browser click) strategy cannot run without the shared
            # engine; skip it outright instead of burning failed launches, and go
            # straight to the vision fallback (unlike HH, Habr has one).
            await self._reporter.publish(
                RunEvent(
                    stage="captcha",
                    message="Habr browser engine is unavailable; using the browserless vision solver",
                    level="warning",
                    url=login_url,
                )
            )
        return await self._vision_solver.solve(login_url, sitekey)


def _spravka(response: HabrCaptchaResponse) -> str | None:
    """Return the pass token when a ``/check`` step succeeded."""
    if response.status == "ok" and response.spravka:
        return response.spravka
    return None


def _solve_pow_field(pow_challenge: HabrPow, login_url: str) -> Result[PowSolution, ClientError]:
    """Solve one challenge's proof-of-work, mapping the failure shapes.

    An over-bound complexity or a search that exhausts its wall-clock budget is
    an unsolvable challenge (hand the URL to a human); a malformed prefix is a
    protocol violation. All never hang.
    """
    if pow_challenge.complexity > _MAX_POW_COMPLEXITY:
        return _unsolved(login_url, "SmartCaptcha proof-of-work complexity is implausibly high")
    try:
        bytes.fromhex(pow_challenge.prefix)
    except ValueError:
        return Err(ProtocolError(message="SmartCaptcha proof-of-work prefix is malformed"))
    solution = solve_pow(pow_challenge.prefix, pow_challenge.complexity)
    if solution is None:
        return _unsolved(login_url, "SmartCaptcha proof-of-work was not solved within its budget")
    return Ok(solution)


def _is_image_challenge(response: HabrCaptchaResponse) -> bool:
    return response.captcha is not None and response.captcha.type == "image"


def _is_checkbox_challenge(response: HabrCaptchaResponse) -> bool:
    return response.captcha is not None and response.captcha.type == "checkbox"


def _validated_answer(answer: str) -> str | None:
    """Normalize an OCR answer; reject empty/oversized/control-character input."""
    normalized = answer.strip()
    if not normalized or len(normalized) > _MAX_ANSWER_CHARACTERS:
        return None
    if not all(character.isprintable() for character in normalized):
        return None
    return normalized


def _unsolved[ValueT](login_url: str, message: str) -> Result[ValueT, ClientError]:
    """A failed challenge, carrying the URL a human would reopen."""
    return Err(CaptchaSolvingError(message=message, recovery_url=login_url))
