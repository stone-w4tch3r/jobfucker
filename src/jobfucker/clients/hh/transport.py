"""Typed ownership boundary for all raw HH HTTP traffic.

:class:`HHTransport` is the board-specific shell: it owns the HH endpoint
methods, origins, headers, and cookie allow-list, and delegates the shared
mechanics (pacing, safe-GET retry, pre-send connect retry, cookie snapshots) to
:class:`jobfucker.clients.shared.transport.Transport`.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlencode

import httpx
from rusty_results.prelude import Result

from jobfucker.clients.base import ClientError
from jobfucker.clients.shared.cookies import PersistedCookie
from jobfucker.clients.shared.transport import (
    FormFields,
    RequestData,
    Transport,
    TransportConfig,
    fingerprint,
)

__all__ = ["FormFields", "HHTransport", "fingerprint"]

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
_CONNECT_ATTEMPTS: Final = 3
_RETRY_BACKOFFS: Final = (2.0, 2.0)


def _user_agent() -> str:
    """Build the HH user-agent with the OS fragment matching the running platform."""
    os_fragment = _UA_OS_FRAGMENTS.get(sys.platform, _DEFAULT_UA_OS_FRAGMENT)
    return f"Mozilla/5.0 ({os_fragment}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def _is_hh_cookie_domain(domain: str) -> bool:
    """Accept only HH-family cookies and exclude unrelated tracker domains."""
    normalized = domain.lstrip(".").rstrip(".").casefold()
    if normalized.startswith("israel."):
        return False
    return normalized == "hh.ru" or normalized.endswith(".hh.ru")


def _config() -> TransportConfig:
    """The HH transport policy (origins/headers/endpoints stay on the class)."""
    return TransportConfig(
        user_agent=_user_agent(),
        cookie_domain_allowed=_is_hh_cookie_domain,
        label="HH",
        follow_redirects=False,
        timeout_s=30.0,
        min_request_interval_s=_ORDINARY_PACING_SECONDS,
        safe_get_attempts=3,
        retry_backoffs_s=_RETRY_BACKOFFS,
    )


class HHTransport:
    """Own one persistent async HTTP client and its cookie jar."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = Transport(_config(), transport)

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the wrapped client to endpoint-specific client code and tests."""
        return self._transport.client

    async def get_website(
        self,
        path: str,
        *,
        params: FormFields = (),
    ) -> Result[httpx.Response, ClientError]:
        """Send a safely retryable website GET with redirects disabled."""
        return await self._transport.get(f"{_HH_ORIGIN}{path}", params=params, headers=())

    async def post_login(self, fields: FormFields, *, xsrf: str) -> Result[httpx.Response, ClientError]:
        """Submit credentials, retrying only failures before the connection exists."""
        files = [(name, (None, value)) for name, value in fields]
        return await self._transport.send(
            "POST",
            f"{_HH_ORIGIN}/account/login",
            RequestData(
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
        return await self._transport.send(
            "POST",
            f"{_HH_ORIGIN}/oauth/token",
            RequestData(
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
        return await self._transport.send(
            "POST",
            f"{_HH_ORIGIN}{path}",
            RequestData(
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
        return await self._transport.get(f"{_API_ORIGIN}{path}", params=params, headers=headers)

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
        return await self._transport.send_ambiguous(
            "POST",
            f"{_API_ORIGIN}{path}",
            RequestData(
                headers=(
                    ("authorization", f"Bearer {access_token}"),
                    ("x-hh-app-active", "true"),
                    ("content-type", "application/x-www-form-urlencoded"),
                ),
                content=urlencode(fields).encode(),
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def issue_captcha_key(self, xsrf: str) -> Result[httpx.Response, ClientError]:
        """Issue a fresh image key for the embedded login CAPTCHA (browserless protocol)."""
        return await self._transport.send(
            "POST",
            f"{_HH_ORIGIN}/captcha",
            RequestData(
                params=(("lang", "RU"),),
                headers=(("x-xsrftoken", xsrf), ("x-requested-with", "XMLHttpRequest")),
                content=b"",
            ),
            connect_attempts=_CONNECT_ATTEMPTS,
        )

    async def get_captcha_picture(self, key: str) -> Result[httpx.Response, ClientError]:
        """Fetch PNG bytes for one server-issued embedded login CAPTCHA key."""
        return await self._transport.get(
            f"{_HH_ORIGIN}/captcha/picture",
            params=(("key", key),),
            headers=(),
        )

    def restore_cookies(self, cookies: tuple[PersistedCookie, ...]) -> None:
        """Restore the allow-listed website cookie subset into the owned jar."""
        self._transport.restore_cookies(cookies)

    def snapshot_cookies(self) -> tuple[PersistedCookie, ...]:
        """Serialize only HH-family cookies from the owned jar."""
        return self._transport.snapshot_cookies()

    def cookie_value(self, name: str) -> str | None:
        """Find one allow-listed cookie by name without domain ambiguity."""
        return self._transport.cookie_value(name)

    async def aclose(self) -> None:
        """Close the connection pool exactly once."""
        await self._transport.aclose()
