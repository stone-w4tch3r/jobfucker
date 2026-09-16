"""Focused transport tests for the apply POST safety contract.

The POST boundary may only retry failures that occur before any request bytes
are on the wire; anything post-send must surface as
:class:`UnknownApplyOutcomeError` so the caller reconciles instead of replaying.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Literal

import httpx
import pytest

from jobfucker.clients.base import ClientError, TransportError, UnknownApplyOutcomeError
from jobfucker.clients.hh.transport import HHTransport

AccessToken = "USER-transport"
FailureKind = Literal["connect", "connect_timeout", "pool_timeout", "read"]


@pytest.mark.parametrize(
    ("platform", "expected_fragment"),
    [
        ("win32", "Windows NT 10.0; Win64; x64"),
        ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
        ("linux", "X11; Linux x86_64"),
        ("freebsd", "X11; Linux x86_64"),
    ],
)
def test_user_agent_os_fragment_follows_platform(
    platform: str, expected_fragment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UA OS fragment mirrors the captcha browser engine's real per-OS Chromium."""
    monkeypatch.setattr(sys, "platform", platform)
    transport = HHTransport()
    try:
        assert expected_fragment in transport.client.headers["user-agent"]
        assert "Chrome/140.0.0.0" in transport.client.headers["user-agent"]
    finally:
        # No event loop in this sync test; dispose the pooled client directly.
        asyncio.run(transport.aclose())


class _FlakyTransport:
    """Handler raising a scripted httpx failure before eventually succeeding."""

    def __init__(self, kind: FailureKind, failures: int) -> None:
        self.kind: FailureKind = kind
        self.failures = failures
        self.attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self._error()
        return httpx.Response(201)

    def _error(self) -> httpx.HTTPError:
        match self.kind:
            case "connect":
                return httpx.ConnectError("connection refused")
            case "connect_timeout":
                return httpx.ConnectTimeout("timed out while connecting")
            case "pool_timeout":
                return httpx.PoolTimeout("timed out waiting for a pool slot")
            case "read":
                return httpx.ReadError("connection lost after sending")


async def _post(transport: HHTransport) -> httpx.Response | ClientError:
    result = await transport.post_api("/negotiations", AccessToken, fields=(("resume_id", "r"), ("vacancy_id", "v")))
    if result.is_ok:
        return result.unwrap()
    return result.unwrap_err()


async def test_post_api_retries_presend_failures_and_succeeds() -> None:
    """ConnectError/ConnectTimeout/PoolTimeout never reach the wire: all are retried."""
    for kind in ("connect", "connect_timeout", "pool_timeout"):
        handler = _FlakyTransport(kind, failures=1)
        transport = HHTransport(transport=httpx.MockTransport(handler))
        try:
            outcome = await _post(transport)
        finally:
            await transport.aclose()
        assert isinstance(outcome, httpx.Response)
        assert outcome.status_code == 201
        assert handler.attempts == 2


async def test_post_api_exhausted_presend_failures_return_transport_error() -> None:
    """Exhausted pre-send retries degrade to an ordinary TransportError."""
    handler = _FlakyTransport("connect", failures=99)
    transport = HHTransport(transport=httpx.MockTransport(handler))
    try:
        outcome = await _post(transport)
    finally:
        await transport.aclose()
    assert isinstance(outcome, TransportError)
    assert handler.attempts == 3


async def test_post_api_postsend_failure_is_unknown_and_never_retried() -> None:
    """A failure after the request was sent is unknown: no retry, reconcile upstream."""
    handler = _FlakyTransport("read", failures=99)
    transport = HHTransport(transport=httpx.MockTransport(handler))
    try:
        outcome = await _post(transport)
    finally:
        await transport.aclose()
    assert isinstance(outcome, UnknownApplyOutcomeError)
    assert handler.attempts == 1


async def test_post_api_sends_form_encoded_body() -> None:
    """The submission body is urlencoded and carries the Bearer header."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201)

    transport = HHTransport(transport=httpx.MockTransport(handler))
    try:
        outcome = await _post(transport)
    finally:
        await transport.aclose()
    assert isinstance(outcome, httpx.Response)
    assert captured[0].headers["authorization"] == f"Bearer {AccessToken}"
    assert captured[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert dict(field.split("=", maxsplit=1) for field in captured[0].content.decode().split("&")) == {
        "resume_id": "r",
        "vacancy_id": "v",
    }


@pytest.mark.parametrize("kind", ["connect", "read"])
async def test_post_api_attempt_counts(kind: FailureKind) -> None:
    """Attempt counts match the safety policy exactly (pre-send retries, post-send never)."""
    failures = 2 if kind == "connect" else 1
    handler = _FlakyTransport(kind, failures=failures)
    transport = HHTransport(transport=httpx.MockTransport(handler))
    try:
        outcome = await _post(transport)
    finally:
        await transport.aclose()
    if kind == "read":
        assert isinstance(outcome, UnknownApplyOutcomeError)
        assert handler.attempts == failures  # never retried after send
    else:
        assert isinstance(outcome, httpx.Response)
        assert handler.attempts == failures + 1  # one success attempt after the failures
