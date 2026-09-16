"""Typed ownership boundary for all raw HH HTTP traffic."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ClientError, TransportError, UnknownApplyOutcomeError
from jobfucker.clients.hh.models import PersistedCookie

_HH_ORIGIN: Final = "https://hh.ru"
_API_ORIGIN: Final = "https://api.hh.ru"
# OS fragment mirrors the captcha browser engine's real per-OS Chromium so the
# API client and the browser don't look like different devices to HH.
_UA_OS_FRAGMENTS: Final[Mapping[str, str]] = {
    "win32": "Windows NT 10.0; Win64; x64",
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
}
_DEFAULT_UA_OS_FRAGMENT: Final = "X11; Linux x86_64"
_ORDINARY_PACING_SECONDS: Final = 0.345
_SAFE_GET_ATTEMPTS: Final = 3
_CONNECT_ATTEMPTS: Final = 3
_NETWORK_BACKOFFS: Final = (2.0, 2.0)
_TEST_BACKOFFS: Final = (0.0, 0.0)
_SERVER_ERROR_STATUS: Final = 500
_RETRYABLE_STATUSES: Final = frozenset({502, 503, 504})

FormFields = tuple[tuple[str, str], ...]

logger = logging.getLogger(__name__)


def _user_agent() -> str:
    """Build the HH user-agent with the OS fragment matching the running platform."""
    os_fragment = _UA_OS_FRAGMENTS.get(sys.platform, _DEFAULT_UA_OS_FRAGMENT)
    return f"Mozilla/5.0 ({os_fragment}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def fingerprint(value: str) -> str:
    """Stable short id for a secret-ish value: safe to log, enough to compare runs."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _header(response: httpx.Response, name: str) -> str:
    """Read one response header as plain str (httpx header access is untyped)."""
    return str(response.headers.get(name, "-"))  # type: ignore[reportAny]  # rationale: untyped lib


def _header_list(response: httpx.Response, name: str) -> tuple[str, ...]:
    """Read one response header as a list of plain str values (httpx is untyped)."""
    return tuple(str(value) for value in response.headers.get_list(name))  # type: ignore[reportAny]  # rationale: untyped


def _log_exchange(method: str, url: str, response: httpx.Response, started: float) -> None:
    """Emit one DEBUG evidence line per HH exchange (opt-in via --log-level debug).

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
    server = _header(response, "server")
    request_id = _header(response, "x-request-id")[:24]
    content_type = _header(response, "content-type").split(";", 1)[0]
    location_header: str | None = response.headers.get("location")  # type: ignore[reportAny]  # rationale: httpx headers are untyped; narrowed by isinstance below
    loc: str = "-" if not isinstance(location_header, str) else urlparse(location_header).path
    logger.debug(
        "HH %s %s -> %s %dms server=%s req_id=%s ct=%s q=[%s] loc=%s set_cookie=[%s]",
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


@dataclass(frozen=True, slots=True)
class _RequestData:
    """Optional request encodings grouped to keep the send boundary small."""

    params: FormFields = ()
    headers: FormFields = ()
    content: bytes | None = None
    files: Sequence[tuple[str, tuple[None, str]]] = ()


class HHTransport:
    """Own one persistent async HTTP client and its cookie jar."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            verify=True,
            headers={"user-agent": _user_agent()},
        )
        self._closed = False
        self._pace_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        # A deterministic mock transport should not spend test time sleeping.
        self._minimum_interval = 0.0 if transport is not None else _ORDINARY_PACING_SECONDS
        self._retry_backoffs = _TEST_BACKOFFS if transport is not None else _NETWORK_BACKOFFS

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the wrapped client to endpoint-specific client code."""
        return self._client

    async def get_website(
        self,
        path: str,
        *,
        params: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable website GET with redirects disabled."""
        return await self._get(f"{_HH_ORIGIN}{path}", params=params, headers=())

    async def post_login(self, fields: FormFields, *, xsrf: str) -> Result[httpx.Response, ClientError]:
        """Submit credentials, retrying only failures before the connection exists."""
        files = [(name, (None, value)) for name, value in fields]
        return await self._send(
            "POST",
            f"{_HH_ORIGIN}/account/login",
            _RequestData(
                params=(("backurl", "/"), ("oauth", "true"), ("response_type", "code")),
                headers=(
                    ("accept", "application/json"),
                    ("x-xsrftoken", xsrf),
                    ("x-requested-with", "XMLHttpRequest"),
                    ("x-hhtmfrom", ""),
                    ("x-hhtmsource", "account_login"),
                ),
                files=files,
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def post_oauth(self, fields: FormFields) -> Result[httpx.Response, ClientError]:
        """Submit an OAuth form, retrying only failures before the connection exists."""
        return await self._send(
            "POST",
            f"{_HH_ORIGIN}/oauth/token",
            _RequestData(
                headers=(("content-type", "application/x-www-form-urlencoded"),),
                content=urlencode(fields).encode(),
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def post_website_multipart(
        self,
        path: str,
        *,
        fields: FormFields,
        xsrf: str,
        referer: str,
    ) -> Result[httpx.Response, ClientError]:
        """Submit one multipart website form (the apply-with-test POST).

        Carries the website cookie jar (already on the owned client), the
        ``X-XSRFToken`` header and the apply-page Referer. The ``X-Requested-With``
        / ``X-GIB-*`` headers are optional per docs/hh/tests.md §2/§8 — verified
        headless submissions succeed without them; only cookies + the form fields
        matter.
        """
        files = [(name, (None, value)) for name, value in fields]
        return await self._send(
            "POST",
            f"{_HH_ORIGIN}{path}",
            _RequestData(
                headers=(
                    ("x-xsrftoken", xsrf),
                    ("x-requested-with", "XMLHttpRequest"),
                    ("referer", referer),
                ),
                files=files,
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def get_api(
        self,
        path: str,
        access_token: str,
        *,
        params: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable authenticated API GET."""
        headers = (
            ("authorization", f"Bearer {access_token}"),
            ("x-hh-app-active", "true"),
        )
        return await self._get(f"{_API_ORIGIN}{path}", params=params, headers=headers)

    async def post_api(
        self,
        path: str,
        access_token: str,
        *,
        fields: FormFields,
    ) -> Result[httpx.Response, ClientError]:
        """Submit one form-encoded authenticated API POST without ambiguity.

        Only failures that occur **before the request is on the wire**
        (connect errors/timeouts) are retried. Any post-send failure returns
        :class:`UnknownApplyOutcomeError`: the application state on the board
        is unconfirmed and the caller must reconcile, never blind-retry.
        """
        url = f"{_API_ORIGIN}{path}"
        request_data = _RequestData(
            headers=(
                ("authorization", f"Bearer {access_token}"),
                ("x-hh-app-active", "true"),
                ("content-type", "application/x-www-form-urlencoded"),
            ),
            content=urlencode(fields).encode(),
        )
        for attempt in range(_CONNECT_ATTEMPTS):
            await self._pace()
            started = monotonic()
            try:
                response = await self._client.request(
                    "POST",
                    url,
                    headers=request_data.headers,
                    content=request_data.content,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # PoolTimeout is pre-send too (the request never started).
                if attempt + 1 < _CONNECT_ATTEMPTS:
                    await asyncio.sleep(self._retry_backoffs[attempt])
                    continue
                return Err(_transport_error("POST", url, exc))
            except httpx.HTTPError as exc:
                # The request bytes may already have been sent; the outcome is unknown.
                path_only = urlparse(url).path
                return Err(
                    UnknownApplyOutcomeError(
                        message=f"HH POST {path_only} outcome is unconfirmed after {type(exc).__name__}"
                    )
                )
            _log_exchange("POST", url, response, started)
            return Ok(response)
        raise AssertionError("connect retry loop must return")

    async def issue_captcha_key(self, xsrf: str) -> Result[httpx.Response, ClientError]:
        """Issue a fresh image key for the embedded login CAPTCHA (browserless protocol)."""
        return await self._send(
            "POST",
            f"{_HH_ORIGIN}/captcha",
            _RequestData(
                params=(("lang", "RU"),),
                headers=(("x-xsrftoken", xsrf), ("x-requested-with", "XMLHttpRequest")),
                content=b"",
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def get_captcha_picture(self, key: str) -> Result[httpx.Response, ClientError]:
        """Fetch PNG bytes for one server-issued embedded login CAPTCHA key."""
        return await self._get(
            f"{_HH_ORIGIN}/captcha/picture",
            params=(("key", key),),
            headers=(),
        )

    def restore_cookies(self, cookies: tuple[PersistedCookie, ...]) -> None:
        """Restore the allow-listed website cookie subset into the owned jar."""
        for cookie in cookies:
            if _is_hh_cookie_domain(cookie.domain):
                self._client.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)

    def snapshot_cookies(self) -> tuple[PersistedCookie, ...]:
        """Serialize only HH-family cookies from the owned jar."""
        persisted: list[PersistedCookie] = []
        for cookie in self._client.cookies.jar:
            if not _is_hh_cookie_domain(cookie.domain):
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
            if cookie.name == name and _is_hh_cookie_domain(cookie.domain):
                return cookie.value
        return None

    async def _get(
        self,
        url: str,
        *,
        params: FormFields,
        headers: FormFields,
    ) -> Result[httpx.Response, ClientError]:
        """Retry safe GET transport failures and selected upstream failures."""
        last_error: ClientError | None = None
        for attempt in range(_SAFE_GET_ATTEMPTS):
            result = await self._send("GET", url, _RequestData(params=params, headers=headers))
            if result.is_err:
                last_error = result.unwrap_err()
            else:
                response = result.unwrap()
                if response.status_code < _SERVER_ERROR_STATUS or response.status_code not in _RETRYABLE_STATUSES:
                    return result
                last_error = TransportError(
                    message="Unknown error when requesting HH upstream",
                    status=response.status_code,
                )
            if attempt + 1 < _SAFE_GET_ATTEMPTS:
                await asyncio.sleep(self._retry_backoffs[attempt])
        assert last_error is not None
        return Err(last_error)

    async def _send(
        self,
        method: Literal["GET", "POST"],
        url: str,
        request_data: _RequestData,
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
                    params=request_data.params,
                    headers=request_data.headers,
                    content=request_data.content,
                    files=request_data.files,
                )
            except httpx.ConnectError as exc:
                if attempt + 1 < connect_attempts:
                    await asyncio.sleep(self._retry_backoffs[attempt])
                    continue
                return Err(_transport_error(method, url, exc))
            except httpx.HTTPError as exc:
                return Err(_transport_error(method, url, exc))
            _log_exchange(method, url, response, started)
            return Ok(response)
        raise AssertionError("connect retry loop must return")

    async def _pace(self) -> None:
        """Serialize requests and preserve the observed minimum HH interval."""
        async with self._pace_lock:
            now = monotonic()
            if self._last_request_at is not None:
                delay = self._minimum_interval - (now - self._last_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_at = monotonic()

    async def aclose(self) -> None:
        """Close the connection pool exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()


def _is_hh_cookie_domain(domain: str) -> bool:
    """Accept only HH-family cookies and exclude unrelated tracker domains."""
    normalized = domain.lstrip(".").rstrip(".").casefold()
    if normalized.startswith("israel."):
        return False
    return normalized == "hh.ru" or normalized.endswith(".hh.ru")


def _transport_error(method: Literal["GET", "POST"], url: str, exc: httpx.HTTPError) -> TransportError:
    """Describe a failed operation without leaking query parameters or request bodies."""
    path = urlparse(url).path
    return TransportError(message=f"HH transport unavailable during {method} {path}: {type(exc).__name__}")
