"""Board-neutral HTTP transport: one persistent client, pacing, retries, cookies.

Every board client owns one :class:`httpx.AsyncClient` and repeats the same
mechanics around it: minimum request pacing, safe-GET retry with bounded
backoff, form/multipart POST with a pre-send-vs-post-send failure distinction,
and an allow-listed cookie snapshot. :class:`Transport` owns those mechanics
once; a board keeps its own endpoints and headers and composes a
:class:`Transport` configured by :class:`TransportConfig`.

Boundary rules:

- ``httpx.AsyncBaseTransport`` stays injectable, so client tests run real client
  code against ``httpx.MockTransport`` (with pacing/backoff zeroed).
- A post-send failure on a non-idempotent POST (see :meth:`Transport.send_ambiguous`)
  surfaces as :class:`UnknownApplyOutcomeError`; it is never retried blindly.
- No raw response body is ever placed into an error message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from urllib.parse import parse_qsl, urlparse

import httpx
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ClientError, TransportError, UnknownApplyOutcomeError
from jobfucker.clients.shared.cookies import CookieDomainPredicate, PersistedCookie

FormFields = tuple[tuple[str, str], ...]
# Multipart parts: (field name, (filename, value)); filename None = a plain field.
MultipartFields = Sequence[tuple[str, tuple[None, str]]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestData:
    """Optional request encodings grouped to keep the send boundary small."""

    params: FormFields = ()
    headers: FormFields = ()
    content: bytes | None = None
    files: MultipartFields = ()


def fingerprint(value: str) -> str:
    """Stable short id for a secret-ish value: safe to log, enough to compare runs."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def header_value(response: httpx.Response, name: str) -> str | None:
    """Read one case-insensitive response header without leaking httpx's weak typing."""
    normalized = name.casefold()
    return next(
        (value for key, value in response.headers.multi_items() if key.casefold() == normalized),
        None,
    )


def _header_str(response: httpx.Response, name: str) -> str:
    """Read one response header as plain str, defaulting to ``-`` (httpx is untyped)."""
    return str(response.headers.get(name, "-"))  # type: ignore[reportAny]  # rationale: untyped lib


def _header_list(response: httpx.Response, name: str) -> tuple[str, ...]:
    """Read one response header as a list of plain str values (httpx is untyped)."""
    return tuple(str(value) for value in response.headers.get_list(name))  # type: ignore[reportAny]  # rationale: untyped


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Per-board transport policy (everything a board would otherwise hardcode)."""

    user_agent: str
    cookie_domain_allowed: CookieDomainPredicate
    label: str = ""  # short board label prefixed to debug/error lines
    follow_redirects: bool = False
    timeout_s: float = 30.0
    min_request_interval_s: float = 0.0
    safe_get_attempts: int = 3
    retry_backoffs_s: tuple[float, ...] = (2.0, 2.0)
    server_error_status: int = 500
    retryable_statuses: frozenset[int] = field(default_factory=lambda: frozenset({502, 503, 504}))


class Transport:
    """Own one persistent async HTTP client, its pacing gate, and its cookie jar."""

    def __init__(self, config: TransportConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=config.follow_redirects,
            timeout=httpx.Timeout(config.timeout_s),
            verify=True,
            headers={"user-agent": config.user_agent},
        )
        self._closed = False
        self._pace_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        # A deterministic mock transport should not spend test time sleeping.
        self._minimum_interval = 0.0 if transport is not None else config.min_request_interval_s
        self._retry_backoffs = (
            (0.0,) * len(config.retry_backoffs_s) if transport is not None else config.retry_backoffs_s
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the wrapped client to endpoint-specific board code and tests."""
        return self._client

    async def get(
        self,
        url: str,
        *,
        params: FormFields = (),
        headers: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable GET (bound backoff on transport/selected 5xx failures)."""
        last_error: ClientError | None = None
        for attempt in range(self._config.safe_get_attempts):
            result = await self.send("GET", url, RequestData(params=params, headers=headers))
            if result.is_err:
                last_error = result.unwrap_err()
            else:
                response = result.unwrap()
                if (
                    response.status_code < self._config.server_error_status
                    or response.status_code not in self._config.retryable_statuses
                ):
                    return result
                last_error = TransportError(
                    message=f"Unknown error when requesting {self._config.label} upstream",
                    status=response.status_code,
                )
            if attempt + 1 < self._config.safe_get_attempts:
                await asyncio.sleep(self._retry_backoffs[attempt])
        assert last_error is not None
        return Err(last_error)

    async def send(
        self,
        method: Literal["GET", "POST"],
        url: str,
        request: RequestData,
        *,
        connect_attempts: int = 1,
    ) -> Result[httpx.Response, ClientError]:
        """Pace and send, retrying only failures that occur before a connection exists."""
        if not 1 <= connect_attempts <= len(self._retry_backoffs) + 1:
            raise ValueError("connect_attempts exceeds the configured retry schedule")
        for attempt in range(connect_attempts):
            await self._pace()
            started = monotonic()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=request.params,
                    headers=request.headers,
                    content=request.content,
                    files=request.files,
                )
            except httpx.ConnectError as exc:
                if attempt + 1 < connect_attempts:
                    await asyncio.sleep(self._retry_backoffs[attempt])
                    continue
                return Err(self._transport_error(method, url, exc))
            except httpx.HTTPError as exc:
                return Err(self._transport_error(method, url, exc))
            self._log_exchange(method, url, response, started)
            return Ok(response)
        raise AssertionError("connect retry loop must return")

    async def send_ambiguous(
        self,
        method: Literal["GET", "POST"],
        url: str,
        request: RequestData,
        *,
        connect_attempts: int = 3,
    ) -> Result[httpx.Response, ClientError]:
        """Submit a non-idempotent request whose post-send failure must not be replayed.

        Only failures that occur **before the request is on the wire** (connect
        errors/timeouts) are retried. Any post-send failure returns
        :class:`UnknownApplyOutcomeError`: the board state is unconfirmed and the
        caller must reconcile, never blind-retry.
        """
        if not 1 <= connect_attempts <= len(self._retry_backoffs) + 1:
            raise ValueError("connect_attempts exceeds the configured retry schedule")
        pre_send_errors = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        for attempt in range(connect_attempts):
            await self._pace()
            started = monotonic()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=request.params,
                    headers=request.headers,
                    content=request.content,
                    files=request.files,
                )
            except pre_send_errors as exc:
                # These are pre-send (the request never started).
                if attempt + 1 < connect_attempts:
                    await asyncio.sleep(self._retry_backoffs[attempt])
                    continue
                return Err(self._transport_error(method, url, exc))
            except httpx.HTTPError as exc:
                # The request bytes may already have been sent; the outcome is unknown.
                path_only = urlparse(url).path
                return Err(
                    UnknownApplyOutcomeError(
                        message=(
                            f"{self._config.label} {method} {path_only} outcome is unconfirmed "
                            f"after {type(exc).__name__}"
                        ).strip()
                    )
                )
            self._log_exchange(method, url, response, started)
            return Ok(response)
        raise AssertionError("connect retry loop must return")

    def restore_cookies(self, cookies: Sequence[PersistedCookie]) -> None:
        """Restore the allow-listed cookie subset into the owned jar."""
        for cookie in cookies:
            if self._config.cookie_domain_allowed(cookie.domain):
                self._client.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)

    def snapshot_cookies(self) -> tuple[PersistedCookie, ...]:
        """Serialize only allow-listed cookies from the owned jar."""
        persisted: list[PersistedCookie] = []
        for cookie in self._client.cookies.jar:
            if not self._config.cookie_domain_allowed(cookie.domain):
                continue
            if cookie.value is None:
                continue
            persisted.append(
                PersistedCookie(
                    name=cookie.name,
                    value=cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                    secure=cookie.secure,
                    expires=cookie.expires,
                )
            )
        return tuple(persisted)

    def cookie_value(self, name: str) -> str | None:
        """Find one allow-listed cookie by name without domain ambiguity."""
        for cookie in self._client.cookies.jar:
            if cookie.name == name and self._config.cookie_domain_allowed(cookie.domain):
                return cookie.value
        return None

    async def _pace(self) -> None:
        """Serialize requests and preserve the configured minimum interval."""
        async with self._pace_lock:
            now = monotonic()
            if self._last_request_at is not None:
                delay = self._minimum_interval - (now - self._last_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_at = monotonic()

    def _log_exchange(self, method: str, url: str, response: httpx.Response, started: float) -> None:
        """Emit one DEBUG evidence line per exchange (opt-in via --log-level debug).

        Deliberately bounded: paths only (no query values — they can carry captcha
        answers), response header whitelist, and cookie names fingerprinted, never
        raw secret values.
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        parsed = urlparse(url)
        query_keys = ",".join(name for name, _ in parse_qsl(parsed.query)) or "-"
        cookie_evidence = ",".join(
            f"{raw.split('=', 1)[0].strip()}:{fingerprint(raw)}" for raw in _header_list(response, "set-cookie")
        )
        server = _header_str(response, "server")
        request_id = _header_str(response, "x-request-id")[:24]
        content_type = _header_str(response, "content-type").split(";", 1)[0]
        location_header = header_value(response, "location")
        loc: str = "-" if location_header is None else urlparse(location_header).path
        logger.debug(
            "%s %s %s -> %s %dms server=%s req_id=%s ct=%s q=[%s] loc=%s set_cookie=[%s]",
            self._config.label,
            method,
            parsed.path,
            response.status_code,
            round((monotonic() - started) * 1000),
            server,
            request_id,
            content_type,
            query_keys,
            loc,
            cookie_evidence or "-",
        )

    def _transport_error(self, method: Literal["GET", "POST"], url: str, exc: httpx.HTTPError) -> TransportError:
        """Describe a failed operation without leaking query parameters or request bodies."""
        path = urlparse(url).path
        return TransportError(
            message=f"{self._config.label} transport unavailable during {method} {path}: {type(exc).__name__}".strip()
        )

    async def aclose(self) -> None:
        """Close the connection pool exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()
