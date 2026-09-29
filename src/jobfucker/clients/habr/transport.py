"""Typed ownership boundary for all raw Habr HTTP traffic.

:class:`HabrTransport` is the board-specific shell: it owns the Habr endpoints,
origins, headers, the cookie allow-list and the CSRF token, and delegates the
shared mechanics (pacing, safe-GET retry, pre-send connect retry, cookie
snapshots) to :class:`jobfucker.clients.shared.transport.Transport`.

CSRF is board-owned (the shared transport has no CSRF concept): the token is
scraped from any HTML page's ``meta[name=csrf-token]``, cached, sent as
``X-CSRF-Token`` on mutations, and refreshed once (never looped) on a ``422``.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ClientError, ProtocolError, TransportError
from jobfucker.clients.shared.cookies import PersistedCookie
from jobfucker.clients.shared.transport import (
    FormFields,
    RequestData,
    Transport,
    TransportConfig,
    fingerprint,
)

__all__ = [
    "ACCOUNT_ORIGIN",
    "CAREER_ORIGIN",
    "HABR_USER_AGENT",
    "SMARTCAPTCHA_ORIGIN",
    "FormFields",
    "HabrTransport",
    "decode_json",
    "fingerprint",
    "is_analytics_cookie",
    "is_habr_cookie_domain",
    "transport_config",
]

CAREER_ORIGIN: Final = "https://career.habr.com"
ACCOUNT_ORIGIN: Final = "https://account.habr.com"
SMARTCAPTCHA_ORIGIN: Final = "https://smartcaptcha.cloud.yandex.ru"

# A normal desktop Chrome UA. The ``HeadlessChrome`` string is the decisive tell
# that escalates Habr's SmartCaptcha from a checkbox to an image challenge
# (docs/habr/captcha.md), so the HTTP client and the browser engine share this
# exact UA.
HABR_USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Ordinary read pacing: a burst at 0.3 s produced a ``429`` once (docs/habr), so
# the client keeps a conservative ≥1 s interval between requests and backs off on
# connection resets (the shared safe-GET retry). Response POSTs add their own
# board-mandated ≥10 s interval on top of this.
_ORDINARY_PACING_SECONDS: Final = 1.0
_CONNECT_ATTEMPTS: Final = 3
_RETRY_BACKOFFS: Final = (2.0, 2.0)
# The shared GET retry only fires on 5xx (>= server_error_status AND in this
# set), so listing a 4xx like 429 here would be inert. A GET 429 stays an
# immediate ``TransportError`` mapped by the caller; a POST 429 is an
# ``ApplyFailed``. Keep this set to the transient 5xx shape.
_RETRYABLE_STATUSES: Final = frozenset({502, 503, 504})

_JSON_ACCEPT: Final = "application/json, text/javascript, */*; q=0.01"
_XHR_HEADERS: Final[FormFields] = (
    ("accept", _JSON_ACCEPT),
    ("x-requested-with", "XMLHttpRequest"),
)
_HTML_ACCEPT: Final = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_HTML_HEADERS: Final[FormFields] = (("accept", _HTML_ACCEPT),)

_HTTP_OK: Final = 200
_HTTP_UNPROCESSABLE: Final = 422

# Rails emits ``<meta name="csrf-token" content="…">``; accept either attribute
# order because templates have been seen both ways.
_CSRF_META_NAME_FIRST: Final = re.compile(
    r'<meta[^>]*\bname=["\']csrf-token["\'][^>]*\bcontent=["\']([^"\']+)["\']', re.IGNORECASE
)
_CSRF_META_CONTENT_FIRST: Final = re.compile(
    r'<meta[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bname=["\']csrf-token["\']', re.IGNORECASE
)

# Analytics/tracker cookies share the ``.habr.com`` domain, so the domain
# predicate alone cannot drop them: filter by name on snapshot.
_ANALYTICS_COOKIE_PREFIXES: Final = ("_ga", "_gid", "_fbp", "mp_")


def is_habr_cookie_domain(domain: str) -> bool:
    """Accept only Habr-family cookies and exclude unrelated tracker domains."""
    normalized = domain.lstrip(".").rstrip(".").casefold()
    return normalized == "habr.com" or normalized.endswith(".habr.com")


def is_analytics_cookie(name: str) -> bool:
    """Whether a cookie name belongs to the analytics/tracker set (never persisted)."""
    normalized = name.casefold()
    return any(normalized.startswith(prefix) for prefix in _ANALYTICS_COOKIE_PREFIXES)


def transport_config() -> TransportConfig:
    """The Habr transport policy (origins/headers/endpoints stay on the class).

    Kept as a public function so the policy (pacing, redirects, cookie
    allow-list) is directly assertable in tests without a live request.
    """
    return TransportConfig(
        user_agent=HABR_USER_AGENT,
        cookie_domain_allowed=is_habr_cookie_domain,
        label="Habr",
        follow_redirects=False,
        timeout_s=30.0,
        min_request_interval_s=_ORDINARY_PACING_SECONDS,
        safe_get_attempts=3,
        retry_backoffs_s=_RETRY_BACKOFFS,
        retryable_statuses=_RETRYABLE_STATUSES,
    )


def decode_json[ModelT: BaseModel](
    response: httpx.Response,
    model: type[ModelT],
    *,
    operation: str,
) -> Result[ModelT, ClientError]:
    """Decode a promised-JSON response; an unparseable body is a ``ProtocolError``.

    Centralizes the "JSON promised but unreadable/wrong type" boundary so no
    caller parses raw payloads itself and no raw body ever leaks into an error.
    """
    try:
        return Ok(model.model_validate_json(response.content))
    except ValidationError:
        return Err(ProtocolError(message=f"Malformed Habr {operation} response", status=response.status_code))


class HabrTransport:
    """Own one persistent async HTTP client, its cookie jar, and the CSRF token."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = Transport(transport_config(), transport)
        self._csrf_token: str | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the wrapped client to endpoint-specific client code and tests."""
        return self._transport.client

    @property
    def csrf_token(self) -> str | None:
        """The currently cached CSRF token, if any (never logged)."""
        return self._csrf_token

    async def get_website(self, path: str, *, params: FormFields = ()) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable career HTML GET with redirects disabled."""
        return await self._transport.get(f"{CAREER_ORIGIN}{path}", params=params, headers=_HTML_HEADERS)

    async def get_json(self, path: str, *, params: FormFields = ()) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable career JSON GET (XHR headers, no CSRF needed)."""
        return await self._transport.get(f"{CAREER_ORIGIN}{path}", params=params, headers=_XHR_HEADERS)

    async def get_absolute(
        self,
        url: str,
        *,
        params: FormFields = (),
        headers: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable GET to an absolute URL (login chain, captcha image)."""
        return await self._transport.get(url, params=params, headers=headers)

    async def post_form(
        self,
        url: str,
        *,
        fields: FormFields,
        params: FormFields = (),
        headers: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Submit one form-urlencoded POST to an absolute URL (login, captcha /check).

        Not a career mutation: no CSRF token is attached. Used for the SSO login
        POST and the SmartCaptcha ladder, both of which live on other origins.
        """
        return await self._transport.send(
            "POST",
            url,
            RequestData(
                params=params,
                headers=(("content-type", "application/x-www-form-urlencoded"), *headers),
                content=urlencode(fields).encode(),
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def post_multipart(self, path: str, *, fields: FormFields = ()) -> Result[httpx.Response, ClientError]:
        """Submit one career mutation as multipart with the cached CSRF token.

        Non-idempotent: a post-send failure surfaces as
        :class:`UnknownApplyOutcomeError` (the caller reconciles, never replays).
        A ``422`` refreshes the CSRF token once and replays the single request
        once; a second ``422`` is returned to the caller unchanged.
        """
        ensured = await self.ensure_csrf()
        if ensured.is_err:
            return Err(ensured.unwrap_err())
        first = await self._send_mutation(path, fields)
        if first.is_err:
            return first
        response = first.unwrap()
        if response.status_code != _HTTP_UNPROCESSABLE:
            return Ok(response)
        refreshed = await self.refresh_csrf()
        if refreshed.is_err:
            return Err(refreshed.unwrap_err())
        # Rebuild the request so the replay carries the freshly refreshed token.
        return await self._send_mutation(path, fields)

    async def _send_mutation(self, path: str, fields: FormFields) -> Result[httpx.Response, ClientError]:
        """Send one multipart mutation with the currently cached CSRF token."""
        files = [(name, (None, value)) for name, value in fields]
        request = RequestData(headers=self._mutation_headers(), files=files)
        return await self._transport.send_ambiguous(
            "POST", f"{CAREER_ORIGIN}{path}", request, connect_attempts=_CONNECT_ATTEMPTS
        )

    async def delete_mutation(self, path: str) -> Result[httpx.Response, ClientError]:
        """Withdraw one resource with the cached CSRF token.

        The shared transport only speaks GET/POST, so this idempotent mutation
        goes through the owned client directly. V1 has no withdraw flow
        (spec non-goals); kept for parity and future use.
        """
        ensured = await self.ensure_csrf()
        if ensured.is_err:
            return Err(ensured.unwrap_err())
        first = await self._raw_request("DELETE", path)
        if first.is_err:
            return first
        response = first.unwrap()
        if response.status_code != _HTTP_UNPROCESSABLE:
            return Ok(response)
        refreshed = await self.refresh_csrf()
        if refreshed.is_err:
            return Err(refreshed.unwrap_err())
        return await self._raw_request("DELETE", path)

    async def refresh_csrf(self, page_path: str = "/vacancies") -> Result[None, ClientError]:
        """Scrape and cache the session-bound CSRF token from an HTML page."""
        response_result = await self.get_website(page_path)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(ProtocolError(message="Habr CSRF page is unavailable", status=response.status_code))
        token = _parse_csrf_token(response.text)
        if token is None:
            return Err(ProtocolError(message="Habr page carried no CSRF token", status=response.status_code))
        self._csrf_token = token
        return Ok(None)

    def restore_cookies(self, cookies: tuple[PersistedCookie, ...]) -> None:
        """Restore the allow-listed cookie subset into the owned jar."""
        self._transport.restore_cookies(cookies)

    def snapshot_cookies(self) -> tuple[PersistedCookie, ...]:
        """Serialize allow-listed Habr cookies, dropping analytics by name."""
        return tuple(cookie for cookie in self._transport.snapshot_cookies() if not is_analytics_cookie(cookie.name))

    def cookie_value(self, name: str) -> str | None:
        """Find one allow-listed cookie by name without domain ambiguity."""
        return self._transport.cookie_value(name)

    async def aclose(self) -> None:
        """Close the connection pool exactly once (idempotent)."""
        await self._transport.aclose()

    def _mutation_headers(self) -> FormFields:
        """Mutation headers: JSON accept, XHR marker, and the cached CSRF token."""
        headers: list[tuple[str, str]] = [*_XHR_HEADERS]
        if self._csrf_token is not None:
            headers.append(("x-csrf-token", self._csrf_token))
        return tuple(headers)

    async def ensure_csrf(self) -> Result[None, ClientError]:
        """Ensure a CSRF token is cached, scraping and caching it when absent.

        Idempotent: a cached token is returned without a request. Callers that
        must not spend a request between their pacing wait and a mutation can
        prefetch it up front (see ``ApplicationService.apply``).
        """
        if self._csrf_token is not None:
            return Ok(None)
        return await self.refresh_csrf()

    async def _raw_request(self, method: str, path: str) -> Result[httpx.Response, ClientError]:
        """Send one mutation outside the shared GET/POST wrappers (DELETE only)."""
        try:
            response = await self._transport.client.request(
                method,
                f"{CAREER_ORIGIN}{path}",
                headers=dict(self._mutation_headers()),
            )
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
            return Err(TransportError(message=f"Habr transport unavailable during {method} {path}: {reason}"))
        return Ok(response)


def _parse_csrf_token(html: str) -> str | None:
    """Extract ``meta[name=csrf-token]`` content from an HTML page."""
    for pattern in (_CSRF_META_NAME_FIRST, _CSRF_META_CONTENT_FIRST):
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
    return None
