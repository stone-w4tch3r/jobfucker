"""Shared classification helpers for HH API reads (GET endpoints)."""

from __future__ import annotations

from typing import Final

import httpx
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    AuthError,
    BadRequestError,
    ClientError,
    NotFoundError,
    ProtocolError,
)
from jobfucker.clients.hh.captcha import CaptchaCoordinator, challenge_error, classify_challenge
from jobfucker.clients.hh.transport import FormFields, HHTransport

_HTTP_OK: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_HTTP_NOT_FOUND: Final = 404
_HTTP_UNAUTHORIZED: Final = frozenset({401, 403})


async def get_api_with_recovery(  # noqa: PLR0913 - explicit (transport, captcha) injection beats a shared wrapper object here
    transport: HHTransport,
    captcha: CaptchaCoordinator,
    path: str,
    access_token: str,
    *,
    params: FormFields,
    allow_recovery: bool = True,
) -> Result[httpx.Response, ClientError]:
    """Classify API CAPTCHA globally, solve once, and replay one safe GET."""
    response_result = await transport.get_api(path, access_token, params=params)
    if response_result.is_err:
        return Err(response_result.unwrap_err())
    response = response_result.unwrap()
    challenge = classify_challenge(response, context="api")
    if challenge is None:
        return Ok(response)
    if not allow_recovery:
        return Err(challenge_error(challenge))
    solved = await captcha.solve(challenge)
    if solved.is_err:
        return Err(solved.unwrap_err())
    return await get_api_with_recovery(transport, captcha, path, access_token, params=params, allow_recovery=False)


def status_error(response: httpx.Response, *, operation: str) -> ClientError | None:
    """Map endpoint status without exposing an untrusted upstream response body."""
    if response.status_code == _HTTP_OK:
        return None
    if response.status_code in _HTTP_UNAUTHORIZED:
        return AuthError(message=f"HH rejected authorization during {operation}")
    if response.status_code == _HTTP_BAD_REQUEST:
        return BadRequestError(message=f"HH rejected {operation}")
    if response.status_code == _HTTP_NOT_FOUND:
        return NotFoundError(message=f"HH {operation} resource was not found")
    return ProtocolError(message=f"Unexpected HH {operation} status", status=response.status_code)
