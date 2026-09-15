"""HH challenge classification and bounded text-CAPTCHA recovery.

Standalone challenges are cleared by the browser engine
(:mod:`jobfucker.clients.hh.browser`); the embedded login challenge keeps the
browserless multipart form-replay protocol (docs/hh/captcha-login.md).
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaHandler, CaptchaSolvingError, ClientError, ProtocolError
from jobfucker.clients.hh.models import (
    CaptchaImageKeyResponse,
    HHErrorEnvelope,
    LoginResponse,
    PersistedCookie,
)
from jobfucker.clients.hh.transport import HHTransport, fingerprint
from jobfucker.reporting import EventLevel, Reporter, RunEvent

_HTTP_OK: Final = 200
_HTTP_FORBIDDEN: Final = 403
_MAX_IMAGE_BYTES: Final = 1_048_576
_MAX_IMAGE_WIDTH: Final = 1_000
_MAX_IMAGE_HEIGHT: Final = 500
_MAX_ANSWER_CHARACTERS: Final = 64

# Debug dump switch: while DEBUG logging is enabled, every CAPTCHA PNG handed to
# a solver is written to this directory for offline review; unset means the
# system-temp fallback (see ``log_captcha_image``).
_CAPTCHA_IMAGES_ENV: Final = "LOGGING_CAPTCHA_IMAGES_PATH"
_DEFAULT_CAPTCHA_IMAGES_DIRNAME: Final = "jobfucker-captcha-images"

ChallengeContext = Literal["api", "login", "website"]

logger = logging.getLogger(__name__)


class ChallengeKind(StrEnum):
    """Challenge families with distinct recovery policies."""

    STANDALONE_TEXT = "standalone_text"
    EMBEDDED_LOGIN = "embedded_login"
    RECAPTCHA = "recaptcha"
    UNKNOWN = "unknown"


class _SubmissionOutcome(StrEnum):
    """Validated result of one standalone answer submission."""

    CLEARED = "cleared"
    WRONG_ANSWER = "wrong_answer"


@dataclass(frozen=True, slots=True)
class Challenge:
    """Sanitized challenge evidence extracted from an untrusted HH response."""

    kind: ChallengeKind
    context: ChallengeContext
    recovery_url: str | None
    status: int
    request_id: str | None
    marker: str
    captcha_state: str | None = None


@dataclass(frozen=True, slots=True)
class CaptchaAnswer:
    """One fresh server-issued image key paired with its validated solver text."""

    key: str
    text: str


LoginCaptchaSubmitter = Callable[[CaptchaAnswer, str], Awaitable[Result[httpx.Response, ClientError]]]


@dataclass(frozen=True, slots=True)
class _StandaloneLocation:
    """Validated same-origin standalone challenge state."""

    state: str


@dataclass(frozen=True, slots=True)
class _LoginAttempt:
    """One decoded embedded-login CAPTCHA submission."""

    outcome: _SubmissionOutcome
    response: httpx.Response


def classify_challenge(response: httpx.Response, *, context: ChallengeContext) -> Challenge | None:
    """Classify supported and unsupported HH challenge signals before status mapping."""
    envelope = _error_envelope(response)
    request_id = _request_id(response, envelope)
    envelope_challenge = _envelope_challenge(
        envelope,
        context=context,
        status=response.status_code,
        request_id=request_id,
    )
    if envelope_challenge is not None:
        return envelope_challenge

    lowered = response.content.lower()
    embedded_challenge = _embedded_login_challenge(
        response,
        context=context,
        request_id=request_id,
        lowered=lowered,
    )
    if embedded_challenge is not None:
        return embedded_challenge

    standalone_markers = (
        b"account-captcha-submit",
        b"account-captcha-error",
    )
    if any(marker in lowered for marker in standalone_markers):
        try:
            recovery_url = str(response.request.url)
        except RuntimeError:
            recovery_url = None
        return Challenge(
            kind=ChallengeKind.STANDALONE_TEXT if recovery_url is not None else ChallengeKind.UNKNOWN,
            context=context,
            recovery_url=recovery_url,
            status=response.status_code,
            request_id=request_id,
            marker="standalone_page",
        )

    recaptcha_markers = (
        b"g-recaptcha-response",
        b"recaptcha/api.js",
        b"recaptcha/enterprise.js",
        b"www.google.com/recaptcha/",
        b"data-sitekey=",
        b"recaptcha_required",
    )
    if any(marker in lowered for marker in recaptcha_markers):
        return Challenge(
            kind=ChallengeKind.RECAPTCHA,
            context=context,
            recovery_url=None,
            status=response.status_code,
            request_id=request_id,
            marker="recaptcha",
        )
    if response.status_code == _HTTP_FORBIDDEN and b"captcha" in lowered:
        return Challenge(
            kind=ChallengeKind.UNKNOWN,
            context=context,
            recovery_url=None,
            status=response.status_code,
            request_id=request_id,
            marker="unmapped_captcha",
        )
    return None


def _embedded_login_challenge(
    response: httpx.Response,
    *,
    context: ChallengeContext,
    request_id: str | None,
    lowered: bytes,
) -> Challenge | None:
    if context != "login":
        return None
    login_response = _login_response(response)
    if login_response is not None and login_response.hhcaptcha is not None:
        if login_response.hhcaptcha.is_bot:
            return Challenge(
                kind=ChallengeKind.EMBEDDED_LOGIN,
                context=context,
                recovery_url=None,
                status=response.status_code,
                request_id=request_id,
                marker="embedded_login_model",
                captcha_state=login_response.hhcaptcha.captcha_state,
            )
        return None
    embedded_markers = (
        b"captchastate",
        b"captchaerror",
        b'"isbot":true',
        b'"isbot": true',
        b"account-captcha-picture",
    )
    if not any(marker in lowered for marker in embedded_markers):
        return None
    return Challenge(
        kind=ChallengeKind.EMBEDDED_LOGIN,
        context=context,
        recovery_url=None,
        status=response.status_code,
        request_id=request_id,
        marker="embedded_login",
    )


def _envelope_challenge(
    envelope: HHErrorEnvelope | None,
    *,
    context: ChallengeContext,
    status: int,
    request_id: str | None,
) -> Challenge | None:
    if envelope is None:
        return None
    for error in envelope.errors:
        error_type = (error.type or "").casefold()
        error_value = (error.value or "").casefold()
        if "captcha" not in error_type and "captcha" not in error_value:
            continue
        if error.captcha_url:
            return Challenge(
                kind=ChallengeKind.STANDALONE_TEXT,
                context=context,
                recovery_url=error.captcha_url,
                status=status,
                request_id=request_id,
                marker="captcha_required",
            )
        return Challenge(
            kind=ChallengeKind.UNKNOWN,
            context=context,
            recovery_url=None,
            status=status,
            request_id=request_id,
            marker="captcha_without_url",
        )
    return None


def challenge_error(challenge: Challenge) -> CaptchaSolvingError:
    """Map a classified challenge into the board-neutral terminal error."""
    messages = {
        ChallengeKind.STANDALONE_TEXT: "HH standalone CAPTCHA was not cleared",
        ChallengeKind.EMBEDDED_LOGIN: "HH embedded login CAPTCHA was not cleared",
        ChallengeKind.RECAPTCHA: "HH reCAPTCHA is not supported",
        ChallengeKind.UNKNOWN: "HH returned an unsupported CAPTCHA challenge",
    }
    return CaptchaSolvingError(
        message=messages[challenge.kind],
        recovery_url=challenge.recovery_url,
    )


def log_captcha_image(png: bytes, *, label: str) -> Path | None:
    """Persist one solver-bound CAPTCHA PNG while DEBUG logging is enabled.

    Debug aid for solver regressions: with DEBUG on, every image handed to a
    solver is written so the exact production frames can be replayed against
    candidate models later (docs/captcha-benchmarking/doc.md). The target
    directory is ``LOGGING_CAPTCHA_IMAGES_PATH`` when set, otherwise
    ``<system temp>/jobfucker-captcha-images``. Best-effort: a dump failure is
    logged and swallowed — CAPTCHA recovery must never break over a debug write.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return None
    directory_value = os.environ.get(_CAPTCHA_IMAGES_ENV, "").strip()
    directory = (
        Path(directory_value) if directory_value else Path(tempfile.gettempdir()) / _DEFAULT_CAPTCHA_IMAGES_DIRNAME
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"captcha-{label}-{time.time_ns()}.png"
        path.write_bytes(png)
    except OSError as exc:
        logger.warning("captcha debug image dump failed: %s", exc)
        return None
    logger.debug("captcha debug image dumped: %s", path)
    return path


class StandaloneCaptchaSolver(Protocol):
    """One standalone challenge clearer backed by the stealth browser engine."""

    async def solve(
        self,
        challenge: Challenge,
        *,
        cookies: Sequence[PersistedCookie],
    ) -> Result[None, ClientError]: ...


class CaptchaCoordinator:
    """Coordinate bounded HH CAPTCHA recovery across its two submit protocols.

    Standalone challenges go through :class:`StandaloneCaptchaSolver` (the
    browser engine); embedded login challenges keep the browserless multipart
    form-replay protocol below.
    """

    def __init__(
        self,
        transport: HHTransport,
        handler: CaptchaHandler,
        *,
        max_attempts: int,
        standalone_solver: StandaloneCaptchaSolver,
        reporter: Reporter | None = None,
    ) -> None:
        self._transport = transport
        self._handler = handler
        self._max_attempts = max_attempts
        self._standalone_solver = standalone_solver
        self._reporter = reporter

    _RUN_EVENT_LOG_LEVELS: Final[dict[EventLevel, int]] = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "success": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }

    async def _announce(self, level: EventLevel, message: str) -> None:
        """Mirror one captcha milestone into the file log and the run-event stream."""
        logger.log(self._RUN_EVENT_LOG_LEVELS[level], message)
        if self._reporter is not None:
            await self._reporter.publish(RunEvent(stage="client", message=message, level=level))

    async def solve(self, challenge: Challenge) -> Result[None, ClientError]:
        """Clear one standalone challenge through the browser engine; fail closed otherwise."""
        if challenge.kind is not ChallengeKind.STANDALONE_TEXT:
            return Err(challenge_error(challenge))
        location = _standalone_location(challenge)
        if location.is_err:
            return Err(location.unwrap_err())
        state = location.unwrap().state
        await self._announce(
            "info",
            f"HH text captcha challenge (state={fingerprint(state)}): the stealth browser engine is solving "
            f"{challenge.recovery_url}",
        )
        solved = await self._standalone_solver.solve(challenge, cookies=self._transport.snapshot_cookies())
        if solved.is_ok:
            await self._announce("success", "HH captcha cleared by the browser engine")
            return Ok(None)
        error = solved.unwrap_err()
        await self._announce("warning", f"HH captcha NOT solved: {error.message}")
        return Err(error)

    async def solve_login(
        self,
        challenge: Challenge,
        *,
        xsrf: str,
        submit: LoginCaptchaSubmitter,
    ) -> Result[httpx.Response, ClientError]:
        """Solve an embedded challenge by replaying the credential form with each answer."""
        if challenge.kind is not ChallengeKind.EMBEDDED_LOGIN:
            return Err(challenge_error(challenge))
        state = challenge.captcha_state
        if state is None or not state:
            return Err(ProtocolError(message="HH embedded login CAPTCHA has no state", status=challenge.status))
        await self._announce(
            "info",
            f"HH login captcha challenge (state={fingerprint(state)}): the credential form will be replayed "
            f"with each answer; the captcha page is https://hh.ru/account/captcha?state={state}",
        )

        for number in range(1, self._max_attempts + 1):
            attempt = await self._login_attempt(challenge, xsrf=xsrf, state=state, submit=submit, number=number)
            if attempt.is_err:
                return Err(attempt.unwrap_err())
            completed = attempt.unwrap()
            if completed.outcome is _SubmissionOutcome.CLEARED:
                await self._announce("success", f"HH login captcha cleared on attempt {number}/{self._max_attempts}")
                return Ok(completed.response)

        await self._announce("warning", f"HH login captcha NOT solved after {self._max_attempts} attempts")
        return Err(CaptchaSolvingError(message=f"HH login CAPTCHA was not solved after {self._max_attempts} attempts"))

    async def _login_attempt(
        self,
        challenge: Challenge,
        *,
        xsrf: str,
        state: str,
        submit: LoginCaptchaSubmitter,
        number: int,
    ) -> Result[_LoginAttempt, ClientError]:
        answer_result = await self._answer(challenge, xsrf=xsrf)
        if answer_result.is_err:
            return Err(answer_result.unwrap_err())
        answer = answer_result.unwrap()
        logger.debug(
            "captcha login attempt %d/%d: key=%s answer=%r",
            number,
            self._max_attempts,
            fingerprint(answer.key),
            answer.text,
        )
        submitted = await submit(answer, state)
        if submitted.is_err:
            return Err(submitted.unwrap_err())
        response = submitted.unwrap()
        outcome = _login_submission_outcome(response, expected_state=state)
        if outcome.is_err:
            return Err(outcome.unwrap_err())
        return Ok(_LoginAttempt(outcome=outcome.unwrap(), response=response))

    async def _answer(self, challenge: Challenge, *, xsrf: str) -> Result[CaptchaAnswer, ClientError]:
        """Issue and immediately solve one short-lived text-CAPTCHA image (login protocol)."""
        key_result = await self._issue_key(xsrf)
        if key_result.is_err:
            return Err(key_result.unwrap_err())
        key = key_result.unwrap()
        image_result = await self._fetch_image(key)
        if image_result.is_err:
            return Err(image_result.unwrap_err())
        answer_result = await self._handler(image_result.unwrap())
        if answer_result.is_err:
            return Err(
                CaptchaSolvingError(
                    message=f"HH CAPTCHA solver failed: {answer_result.unwrap_err()}",
                    recovery_url=challenge.recovery_url,
                )
            )
        answer = answer_result.unwrap().strip()
        if (
            not answer
            or len(answer) > _MAX_ANSWER_CHARACTERS
            or not answer.isprintable()
            or any(character.isspace() and character != " " for character in answer)
        ):
            return Err(
                CaptchaSolvingError(
                    message="HH CAPTCHA solver returned an invalid answer",
                    recovery_url=challenge.recovery_url,
                )
            )
        return Ok(CaptchaAnswer(key=key, text=answer))

    async def _issue_key(self, xsrf: str) -> Result[str, ClientError]:
        response_result = await self._transport.issue_captcha_key(xsrf)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(ProtocolError(message="HH CAPTCHA key request failed", status=response.status_code))
        if not _has_media_type(response, "application/json"):
            return Err(ProtocolError(message="HH CAPTCHA image-key response is not JSON", status=response.status_code))
        try:
            decoded = CaptchaImageKeyResponse.model_validate_json(response.content)
        except ValidationError:
            return Err(ProtocolError(message="Malformed HH CAPTCHA image-key response", status=response.status_code))
        return Ok(decoded.key)

    async def _fetch_image(self, key: str) -> Result[bytes, ClientError]:
        response_result = await self._transport.get_captcha_picture(key)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        image_result = _validated_png(response_result.unwrap())
        if image_result.is_ok:
            log_captcha_image(image_result.unwrap(), label="login")
        return image_result


def _error_envelope(response: httpx.Response) -> HHErrorEnvelope | None:
    try:
        return HHErrorEnvelope.model_validate_json(response.content)
    except ValidationError:
        return None


def _login_response(response: httpx.Response) -> LoginResponse | None:
    try:
        return LoginResponse.model_validate_json(response.content)
    except ValidationError:
        return None


def _request_id(response: httpx.Response, envelope: HHErrorEnvelope | None) -> str | None:
    if envelope is not None and envelope.request_id:
        return envelope.request_id
    return _header_value(response, "x-request-id")


def _standalone_location(challenge: Challenge) -> Result[_StandaloneLocation, ClientError]:
    recovery_url = challenge.recovery_url
    if recovery_url is None:
        return Err(ProtocolError(message="HH standalone CAPTCHA has no recovery URL", status=challenge.status))
    query: str | None = None
    try:
        parsed = urlparse(recovery_url)
        valid_origin = (
            parsed.scheme == "https"
            and parsed.hostname == "hh.ru"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/account/captcha"
        )
        if valid_origin:
            query = parsed.query
    except ValueError:
        query = None
    if query is None:
        return Err(ProtocolError(message="HH CAPTCHA returned an invalid recovery URL", status=challenge.status))
    states = parse_qs(query).get("state", [])
    if len(states) != 1 or not states[0]:
        return Err(ProtocolError(message="HH CAPTCHA recovery URL has no valid state", status=challenge.status))
    return Ok(_StandaloneLocation(state=states[0]))


def _validated_png(response: httpx.Response) -> Result[bytes, ClientError]:
    content = response.content
    if response.status_code != _HTTP_OK or not _has_media_type(response, "image/png"):
        return Err(ProtocolError(message="HH CAPTCHA picture is not a PNG", status=response.status_code))
    if not content or len(content) > _MAX_IMAGE_BYTES:
        return Err(ProtocolError(message="HH CAPTCHA picture has an invalid size", status=response.status_code))
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG":
                return Err(
                    ProtocolError(message="HH CAPTCHA picture has invalid PNG data", status=response.status_code)
                )
            width, height = image.size
            if width <= 0 or height <= 0 or width > _MAX_IMAGE_WIDTH or height > _MAX_IMAGE_HEIGHT:
                return Err(
                    ProtocolError(message="HH CAPTCHA picture dimensions are unsafe", status=response.status_code)
                )
            image.verify()
    except Image.DecompressionBombError, UnidentifiedImageError, OSError:
        return Err(ProtocolError(message="HH CAPTCHA picture has invalid PNG data", status=response.status_code))
    return Ok(content)


def _login_submission_outcome(
    response: httpx.Response,
    *,
    expected_state: str,
) -> Result[_SubmissionOutcome, ClientError]:
    if response.status_code != _HTTP_OK or not _has_media_type(response, "application/json"):
        return Err(ProtocolError(message="Unexpected HH login CAPTCHA response", status=response.status_code))
    decoded = _login_response(response)
    if decoded is None or decoded.hhcaptcha is None:
        return Err(ProtocolError(message="Malformed HH login CAPTCHA response", status=response.status_code))
    captcha = decoded.hhcaptcha
    if not captcha.is_bot:
        return Ok(_SubmissionOutcome.CLEARED)
    if captcha.captcha_state != expected_state:
        return Err(ProtocolError(message="HH login CAPTCHA state changed unexpectedly", status=response.status_code))
    return Ok(_SubmissionOutcome.WRONG_ANSWER)


def _header_value(response: httpx.Response, name: str) -> str | None:
    """Read one case-insensitive response header without leaking httpx's weak typing."""
    normalized = name.casefold()
    return next(
        (value for key, value in response.headers.multi_items() if key.casefold() == normalized),
        None,
    )


def _has_media_type(response: httpx.Response, expected: str) -> bool:
    raw_content_type = _header_value(response, "content-type") or ""
    return raw_content_type.partition(";")[0].strip().casefold() == expected
