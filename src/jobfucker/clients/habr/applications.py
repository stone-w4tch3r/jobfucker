"""Habr application preflight, submission, pacing, and reconciliation.

Applying on Habr is one multipart ``POST`` to
``/api/frontend/vacancies/<id>/responses`` (the cover letter is the ``body``
field; letterless means no field at all). Before submitting, the client refetches
the vacancy detail and turns deterministic board states into idempotent skips
(already applied, archived/hidden). CSRF is owned by
:class:`~jobfucker.clients.habr.transport.HabrTransport` (auto-attach plus a
single refresh-and-replay on ``422``).

The board enforces a ~10 s minimum between responses per account, far above the
shared transport's 0.3 s read pacing, so this service owns a monotonic
response-interval delay (with jitter), injectable for tests. An unconfirmed POST
(``UnknownApplyOutcomeError``) is reconciled by refetching the detail and
checking ``response.kind``; it is never replayed.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyError,
    ApplyFailed,
    ApplyResult,
    ApplySkip,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    ClientError,
    ConfigurationError,
    LimitExceededError,
    NotFoundError,
    ProtocolError,
    ServiceVacancyId,
    TransportError,
    UnknownApplyOutcomeError,
)
from jobfucker.clients.habr.models import HabrErrorEnvelope, HabrStructuredError
from jobfucker.clients.habr.search import fetch_vacancy_detail
from jobfucker.clients.habr.transport import FormFields, HabrTransport

__all__ = ["ApplicationService", "ApplyDelay"]

# The board's minimum interval between responses for one account (docs/habr);
# the shared transport's ordinary pacing is only 0.3 s and must not be relied on.
_MIN_RESPONSE_INTERVAL_S: Final = 10.0
_JITTER_MIN_S: Final = 0.5
_JITTER_MAX_S: Final = 1.5

# Message fragments that discriminate the two same-shaped `400` bodies and the
# two differently-shaped `401` bodies (verified against live Habr).
_RESEND_MARKER: Final = "10 секунд"
_MONTHLY_CAP_MARKER: Final = "150 откликов"
_DUPLICATE_MARKER: Final = "уже откликнулись"

_KIND_APPLIED: Final = "applied"

_HTTP_OK: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_NOT_FOUND: Final = 404
_HTTP_UNPROCESSABLE: Final = 422
_HTTP_SERVER_ERROR_MIN: Final = 500

# The response POST is only safe to repeat after an explicit throttle signal:
# the board rejected it before recording an application.
ApplyDelay = Callable[[], Awaitable[None]]


class _HabrApplyResponse(BaseModel):
    """The successful submission's ``response`` object (only ``id`` matters)."""

    model_config = ConfigDict(extra="ignore")

    id: int


class _HabrApplyEnvelope(BaseModel):
    """``200 {"response": {"id": …}}`` success envelope."""

    model_config = ConfigDict(extra="ignore")

    response: _HabrApplyResponse | None = None


class _HabrMessageEnvelope(BaseModel):
    """``400 {"message": …}`` throttle/monthly-cap envelope."""

    model_config = ConfigDict(extra="ignore")

    message: str | None = None


@dataclass(frozen=True, slots=True)
class _Throttled:
    """The board asked for a wait before the submission may be retried once."""

    text: str


def _make_default_apply_delay() -> ApplyDelay:
    """Build the stateful ≥10 s monotonic response pacing plus small jitter.

    The interval is measured between the moments the delay returns (immediately
    before each response POST), so back-to-back submissions keep the board's
    minimum spacing even when the caller never sleeps between applies.
    """
    last_response_at: float | None = None

    async def delay() -> None:
        nonlocal last_response_at
        if last_response_at is not None:
            remaining = _MIN_RESPONSE_INTERVAL_S - (monotonic() - last_response_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))  # noqa: S311 - apply jitter, not cryptographic
        last_response_at = monotonic()

    return delay


class ApplicationService:
    """Apply preflight, submission, outcome classification, and reconciliation."""

    def __init__(self, transport: HabrTransport, *, delay: ApplyDelay | None = None) -> None:
        self._transport = transport
        self._delay = delay if delay is not None else _make_default_apply_delay()

    async def apply(  # noqa: PLR0911 - preflight skip + submit/retry branches, one return each
        self,
        *,
        vacancy_id: ServiceVacancyId,
        message: str | None,
    ) -> Result[ApplyResult, ClientError]:
        """Preflight, submit at most twice (one throttle retry), and classify."""
        preflight = await self._preflight(vacancy_id)
        if preflight.is_err:
            return Err(preflight.unwrap_err())
        skip = preflight.unwrap()
        if skip is not None:
            return Ok(ApplySkipped(skip))

        # Prefetch the CSRF token before pacing: ``post_multipart`` would
        # otherwise lazily scrape it (a ~3 s GET) *between* the delay and the
        # POST, stretching the board-facing POST→POST gap past the minimum and
        # triggering the board's throttle.
        ensured = await self._transport.ensure_csrf()
        if ensured.is_err:
            return Err(ensured.unwrap_err())

        await self._delay()
        first = await self._submit(vacancy_id, message)
        if first.is_err:
            return await self._recover(first.unwrap_err(), vacancy_id)
        outcome = first.unwrap()
        if not isinstance(outcome, _Throttled):
            return Ok(outcome)

        # Throttled: honour the mandated interval once and retry exactly once.
        await self._delay()
        retried = await self._submit(vacancy_id, message)
        if retried.is_err:
            return await self._recover(retried.unwrap_err(), vacancy_id)
        retry_outcome = retried.unwrap()
        if isinstance(retry_outcome, _Throttled):
            return Ok(ApplyFailed(ApplyError(text=retry_outcome.text)))
        return Ok(retry_outcome)

    async def _preflight(self, vacancy_id: ServiceVacancyId) -> Result[ApplySkip | None, ClientError]:
        """Fetch the current detail and make the deterministic skip decisions."""
        detail_result = await fetch_vacancy_detail(self._transport, vacancy_id)
        if detail_result.is_err:
            return Err(detail_result.unwrap_err())
        detail = detail_result.unwrap()
        if detail.response is not None and detail.response.kind == _KIND_APPLIED:
            return Ok(ApplySkip(reason="already_applied", text=f"Habr vacancy {vacancy_id} was already applied to"))
        if detail.archived or detail.hidden:
            return Ok(ApplySkip(reason="vacancy_unavailable", text=f"Habr vacancy {vacancy_id} is archived or hidden"))
        return Ok(None)

    async def _submit(
        self,
        vacancy_id: ServiceVacancyId,
        message: str | None,
    ) -> Result[ApplyResult | _Throttled, ClientError]:
        """Send one multipart response submission (letter only when non-empty)."""
        fields: FormFields = (("body", message),) if message else ()
        path = f"/api/frontend/vacancies/{vacancy_id}/responses"
        response_result = await self._transport.post_multipart(path, fields=fields)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        return _classify_submission(response_result.unwrap(), vacancy_id)

    async def _recover(
        self,
        error: ClientError,
        vacancy_id: ServiceVacancyId,
    ) -> Result[ApplyResult, ClientError]:
        """Reconcile an unconfirmed submission; propagate every other error."""
        if isinstance(error, UnknownApplyOutcomeError):
            return await self._reconcile(vacancy_id)
        return Err(error)

    async def _reconcile(self, vacancy_id: ServiceVacancyId) -> Result[ApplyResult, ClientError]:
        """Establish an unconfirmed POST outcome by refetching the detail.

        Finding ``response.kind == "applied"`` proves the application exists; when
        the detail cannot establish that, the outcome stays unconfirmed. The
        submission is never replayed.
        """
        detail_result = await fetch_vacancy_detail(self._transport, vacancy_id)
        if detail_result.is_ok:
            detail = detail_result.unwrap()
            if detail.response is not None and detail.response.kind == _KIND_APPLIED:
                return Ok(ApplySucceeded())
        return Err(UnknownApplyOutcomeError(message=f"Habr application outcome for {vacancy_id} remains unconfirmed"))


def _classify_submission(  # noqa: PLR0911 - exhaustive observed-contract classification table
    response: httpx.Response,
    vacancy_id: ServiceVacancyId,
) -> Result[ApplyResult | _Throttled, ClientError]:
    """Classify one response-POST outcome per the observed Habr contract."""
    status = response.status_code
    if status == _HTTP_OK:
        envelope = _try_decode(response, _HabrApplyEnvelope)
        if envelope is not None and envelope.response is not None:
            return Ok(ApplySucceeded())
        return Err(ProtocolError(message="Habr application success carried no response id", status=status))
    if status == _HTTP_UNAUTHORIZED:
        message = _error_message(response) or ""
        if _DUPLICATE_MARKER in message:
            return Ok(
                ApplySkipped(
                    ApplySkip(reason="already_applied", text=f"Habr vacancy {vacancy_id} was already applied to")
                )
            )
        return Err(AuthError(message="Habr rejected the session during application"))
    if status == _HTTP_BAD_REQUEST:
        message = _error_message(response) or ""
        if _RESEND_MARKER in message:
            return Ok(_Throttled(text=message))
        if _MONTHLY_CAP_MARKER in message:
            return Err(LimitExceededError(message="Habr monthly application cap is reached"))
        return Ok(ApplyFailed(ApplyError(text=message or "Habr rejected the application", code=f"http_{status}")))
    if status == _HTTP_NOT_FOUND:
        return Err(NotFoundError(message=f"Habr vacancy {vacancy_id} was not found"))
    if status == _HTTP_UNPROCESSABLE:
        # The transport already refreshed the CSRF token once and replayed; a
        # second 422 is a configuration/token problem, not a not-found.
        return Err(ConfigurationError(message="Habr rejected the response submission: the CSRF token was not accepted"))
    if status >= _HTTP_SERVER_ERROR_MIN:
        return Err(TransportError(message="Habr application submission failed", status=status))
    if status >= _HTTP_BAD_REQUEST:
        message = _error_message(response)
        return Ok(
            ApplyFailed(
                ApplyError(text=message or f"Habr rejected the application with status {status}", code=f"http_{status}")
            )
        )
    return Err(ProtocolError(message="Unexpected Habr application status", status=status))


def _error_message(response: httpx.Response) -> str | None:
    """Extract the human message from any of Habr's three error envelope shapes."""
    envelope = _try_decode(response, HabrErrorEnvelope)
    if envelope is not None and envelope.message is not None:
        return envelope.message
    structured = _try_decode(response, HabrStructuredError)
    if structured is not None:
        return structured.message
    message_envelope = _try_decode(response, _HabrMessageEnvelope)
    if message_envelope is not None:
        return message_envelope.message
    return None


def _try_decode[ModelT: BaseModel](response: httpx.Response, model: type[ModelT]) -> ModelT | None:
    """Decode a best-effort error body; a non-matching shape is simply ``None``."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        return None
