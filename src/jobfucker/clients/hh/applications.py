"""HH application preflight, submission, and reconciliation boundary."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Final

import httpx
from pydantic import ValidationError
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
    ProtocolError,
    ServiceVacancyId,
    UnknownApplyOutcomeError,
)
from jobfucker.clients.hh.apireads import get_api_with_recovery, status_error
from jobfucker.clients.hh.captcha import CaptchaCoordinator, challenge_error, classify_challenge
from jobfucker.clients.hh.models import HHErrorEnvelope, NegotiationsPage, VacancyDetailResponse
from jobfucker.clients.hh.transport import FormFields, HHTransport

_HTTP_CREATED: Final = 201
_HTTP_BAD_REQUEST: Final = 400
_HTTP_UNAUTHORIZED: Final = frozenset({401, 403})
_HTTP_REDIRECT_MIN: Final = 300
_HTTP_REDIRECT_MAX: Final = 400
_HTTP_SERVER_ERROR_MIN: Final = 500
_NEGOTIATIONS_PAGE_SIZE: Final = 100
# Reconciliation walks server-reported `pages`; the cap bounds a buggy/hostile
# upstream that keeps growing it (10 pages x 100 = far beyond any real active list).
_MAX_RECONCILIATION_PAGES: Final = 10

# The observed apply etiquette: a randomized 1-3 s wait immediately before the
# submission POST (on top of the transport's own pacing).
ApplyDelay = Callable[[], Awaitable[None]]


async def _randomized_apply_delay() -> None:
    await asyncio.sleep(random.uniform(1.0, 3.0))  # noqa: S311 - apply jitter, not cryptographic


class ApplicationService:
    """Apply preflight, submission, and outcome classification for one HH session."""

    def __init__(
        self,
        transport: HHTransport,
        captcha: CaptchaCoordinator,
        *,
        delay: ApplyDelay | None = None,
    ) -> None:
        self._transport = transport
        self._captcha = captcha
        self._delay = delay if delay is not None else _randomized_apply_delay

    async def apply(
        self,
        *,
        access_token: str,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None,
    ) -> Result[ApplyResult, ClientError]:
        """Preflight, submit, and classify one application.

        A post-send transport failure is reconciled through the active
        negotiations list before any outcome is reported; the outcome of an
        unconfirmed submission is never replayed.
        """
        preflight = await self._preflight(access_token, vacancy_id)
        if preflight.is_err:
            return Err(preflight.unwrap_err())
        skip = preflight.unwrap()
        if skip is not None:
            return Ok(ApplySkipped(skip))
        await self._delay()
        fields: FormFields = (("resume_id", resume_id), ("vacancy_id", vacancy_id))
        if message:
            fields += (("message", message),)
        submitted = await self._submit(access_token, fields, vacancy_id, allow_recovery=True)
        if submitted.is_err:
            error = submitted.unwrap_err()
            if isinstance(error, UnknownApplyOutcomeError):
                return await self._reconcile(access_token, vacancy_id)
            return Err(error)
        return Ok(submitted.unwrap())

    async def _preflight(  # noqa: PLR0911 - deterministic skip ladder, one return per documented condition
        self,
        access_token: str,
        vacancy_id: ServiceVacancyId,
    ) -> Result[ApplySkip | None, ClientError]:
        """Fetch current detail and make deterministic skip decisions."""
        detail_result = await get_api_with_recovery(
            self._transport, self._captcha, f"/vacancies/{vacancy_id}", access_token, params=()
        )
        if detail_result.is_err:
            return Err(detail_result.unwrap_err())
        response = detail_result.unwrap()
        status_failure = status_error(response, operation="vacancy detail")
        if status_failure is not None:
            return Err(status_failure)
        try:
            detail = VacancyDetailResponse.model_validate_json(response.content)
        except ValidationError:
            return Err(ProtocolError(message="Malformed HH vacancy detail response", status=response.status_code))
        if detail.id != vacancy_id:
            return Err(ProtocolError(message="HH vacancy detail id does not match the requested vacancy"))
        if detail.archived or detail.closed_for_applicants:
            return Ok(
                ApplySkip(
                    reason="vacancy_unavailable",
                    text=f"HH vacancy {vacancy_id} is archived or closed for applicants",
                )
            )
        # No test-presence skip arm: screening tests are routed through the
        # HhTestCapable web flow (clients/hh/tests.py). A test-bearing vacancy
        # reaching the standard path here still classifies correctly at submit
        # time (the API ``test_required`` envelope maps to a skip below).
        external = detail.response_url or detail.adv_response_url
        if external is not None:
            return Ok(
                ApplySkip(
                    reason="external_application",
                    text=f"HH vacancy {vacancy_id} uses an external application flow",
                    redirect_url=external,
                )
            )
        return Ok(None)

    async def _submit(
        self,
        access_token: str,
        fields: FormFields,
        vacancy_id: ServiceVacancyId,
        *,
        allow_recovery: bool,
    ) -> Result[ApplyResult, ClientError]:
        """POST /negotiations once, clearing at most one challenge in between.

        The replay after a solved challenge is safe: the challenge response
        means the submission was never accepted.
        """
        response_result = await self._transport.post_api("/negotiations", access_token, fields=fields)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        challenge = classify_challenge(response, context="api")
        if challenge is not None:
            if not allow_recovery:
                return Err(challenge_error(challenge))
            solved = await self._captcha.solve(challenge)
            if solved.is_err:
                return Err(solved.unwrap_err())
            return await self._submit(access_token, fields, vacancy_id, allow_recovery=False)
        return _classify_submission(response, vacancy_id)

    async def _reconcile(
        self,
        access_token: str,
        vacancy_id: ServiceVacancyId,
    ) -> Result[ApplyResult, ClientError]:
        """Establish an unknown POST outcome through the active negotiations list.

        Finding the vacancy proves the application exists. Absence after a
        full scan is strong but status-scoped evidence, so it still reports
        :class:`UnknownApplyOutcomeError` — the vacancy is never auto-replayed.
        """
        page = 0
        while True:
            response_result = await get_api_with_recovery(
                self._transport,
                self._captcha,
                "/negotiations",
                access_token,
                params=(("status", "active"), ("page", str(page)), ("per_page", str(_NEGOTIATIONS_PAGE_SIZE))),
            )
            if response_result.is_err:
                reason = response_result.unwrap_err().message
                return Err(UnknownApplyOutcomeError(message=f"HH reconciliation failed: {reason}"))
            response = response_result.unwrap()
            status_failure = status_error(response, operation="negotiation reconciliation")
            if status_failure is not None:
                return Err(UnknownApplyOutcomeError(message=f"HH reconciliation failed: {status_failure.message}"))
            try:
                negotiations = NegotiationsPage.model_validate_json(response.content)
            except ValidationError:
                return Err(UnknownApplyOutcomeError(message="HH reconciliation returned a malformed page"))
            if any(item.vacancy.id == vacancy_id for item in negotiations.items):
                return Ok(ApplySucceeded())
            if page >= min(negotiations.pages, _MAX_RECONCILIATION_PAGES):
                return Err(
                    UnknownApplyOutcomeError(
                        message=(
                            f"HH application outcome for {vacancy_id} remains unconfirmed: "
                            "not found in active negotiations"
                        )
                    )
                )
            page += 1


def _classify_submission(  # noqa: PLR0911, PLR0912 - exhaustive observed-contract classification table
    response: httpx.Response, vacancy_id: ServiceVacancyId
) -> Result[ApplyResult, ClientError]:
    """Classify the POST /negotiations outcome per the observed contract."""
    if response.status_code == _HTTP_CREATED:
        if not response.content:
            return Ok(ApplySucceeded())
        return Err(
            ProtocolError(message="HH application success carried an unexpected body", status=response.status_code)
        )
    if _HTTP_REDIRECT_MIN <= response.status_code < _HTTP_REDIRECT_MAX:
        return Ok(
            ApplySkipped(
                ApplySkip(
                    reason="external_application",
                    text=f"HH vacancy {vacancy_id} redirected the application",
                    redirect_url=_header_value(response, "location"),
                )
            )
        )
    # Envelope values classify BEFORE the status fallback: HH answers business-rule
    # rejections on this endpoint with 403 + envelope (live-verified: already_applied
    # is 403, not 400), so 401/403 alone is not an auth signal.
    envelope = _error_envelope(response)
    if envelope is not None and envelope.errors:
        for error in envelope.errors:
            value = (error.value or "").casefold()
            if value == "already_applied":
                return Ok(
                    ApplySkipped(
                        ApplySkip(
                            reason="already_applied",
                            text=f"HH vacancy {vacancy_id} was already applied to",
                        )
                    )
                )
            if value == "test_required":
                return Ok(
                    ApplySkipped(
                        ApplySkip(
                            reason="test_required",
                            text=f"HH vacancy {vacancy_id} requires an unsupported test",
                        )
                    )
                )
            if value == "resume_not_found":
                return Err(ConfigurationError(message="HH rejected the configured resume: not found for this account"))
            if value == "limit_exceeded":
                return Err(LimitExceededError(message="HH per-auth daily application cap is reached"))
        if response.status_code in _HTTP_UNAUTHORIZED:
            # An envelope without any known routing value on 401/403 is an auth signal.
            return Err(AuthError(message="HH rejected authorization during application"))
        first = envelope.errors[0]
        return Ok(
            ApplyFailed(
                ApplyError(
                    text=envelope.description or "HH rejected the application",
                    code=first.value or first.type,
                )
            )
        )
    if response.status_code in _HTTP_UNAUTHORIZED:
        return Err(AuthError(message="HH rejected authorization during application"))
    if response.status_code >= _HTTP_SERVER_ERROR_MIN:
        return Err(
            UnknownApplyOutcomeError(
                message=f"HH application outcome is unconfirmed after status {response.status_code}"
            )
        )
    if _HTTP_BAD_REQUEST <= response.status_code < _HTTP_SERVER_ERROR_MIN:
        return Ok(ApplyFailed(ApplyError(text="HH rejected the application", code=f"http_{response.status_code}")))
    return Err(ProtocolError(message="Unexpected HH application status", status=response.status_code))


def _error_envelope(response: httpx.Response) -> HHErrorEnvelope | None:
    try:
        return HHErrorEnvelope.model_validate_json(response.content)
    except ValidationError:
        return None


def _header_value(response: httpx.Response, name: str) -> str | None:
    """Read one case-insensitive response header without leaking httpx's weak typing."""
    normalized = name.casefold()
    return next(
        (value for key, value in response.headers.multi_items() if key.casefold() == normalized),
        None,
    )
