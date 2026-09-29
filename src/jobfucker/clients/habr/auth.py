"""Habr SSO session lifecycle: cookie reuse, browserless login chain, identity.

The coordinator owns one login story for the whole client, verified against live
Habr (docs/habr/authentication.md, docs/habr/captcha.md):

1. Load the persisted session snapshot and restore its cookies.
2. ``GET /api/frontend_v1/users/me`` — ``user`` present is the only auth signal
   (an anonymous call is ``200 {}``).
3. A lone ``remember_user_token`` re-issues the session, so retry the identity
   call **once** before committing to a fresh login.
4. Fresh login: follow ``GET /users/auth/tmid`` through the OAuth ``Location``s
   to the ``/ru/ident/<state>`` page, solve its SmartCaptcha through
   :class:`LoginCaptcha`, submit the ``email``/``password``/``smart-token`` form,
   then follow the returned ``rurl`` callback with the same cookie jar.
5. Persist the new cookie snapshot + identity.

The configured ``resume_id`` (when given) must equal the account alias; a
mismatch stops the pipeline with a ``ConfigurationError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    AuthError,
    ClientDeps,
    ClientError,
    ConfigurationError,
    ProtocolError,
    ServiceIdentity,
)
from jobfucker.clients.habr.captcha import LoginCaptcha
from jobfucker.clients.habr.models import (
    HabrIdentityResponse,
    HabrLoginResponse,
    PersistedHabrIdentity,
    PersistedHabrSession,
)
from jobfucker.clients.habr.transport import ACCOUNT_ORIGIN, CAREER_ORIGIN, HabrTransport, decode_json
from jobfucker.clients.shared.cookies import PersistedCookie
from jobfucker.clients.shared.store import AtomicJsonStore
from jobfucker.clients.shared.transport import header_value
from jobfucker.reporting import RunEvent

__all__ = ["SESSION_FILENAME", "AuthCoordinator", "LoginSurface"]

SESSION_FILENAME: Final = "session.json"
_IDENTITY_PATH: Final = "/api/frontend_v1/users/me"
_TMID_PATH: Final = "/users/auth/tmid"
_IDENT_PATH_MARKER: Final = "/ru/ident/"
_REMEMBER_COOKIE: Final = "remember_user_token"

_HTTP_OK: Final = 200
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# A non-200 identity answer that is a redirect (the anonymous ``302`` to
# ``/users/auth_required``) or a ``401``/``403`` means the session is stale, not
# that the endpoint is broken. Treat it as anonymous so ``authorize()`` can
# recover through the remembered cookie or a full login; any other non-200 stays
# a ``ProtocolError``.
_ANONYMOUS_STATUSES: Final = _REDIRECT_STATUSES | {401, 403}
_MAX_LOGIN_HOPS: Final = 6

_LOGIN_POST_HEADERS: Final = (
    ("accept", "application/json, text/javascript, */*; q=0.01"),
    ("x-requested-with", "XMLHttpRequest"),
)

# The login form action and the widget sitekey both live in the SSR login page.
_IDENT_FORM_ACTION_RE: Final = re.compile(r'<form[^>]*\baction="([^"]*?/ru/ident/in/[^"]+)"', re.IGNORECASE)
_SITEKEY_RE: Final = re.compile(r'data-sitekey="([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LoginSurface:
    """The three coordinates needed to log in: page URL, form action, sitekey."""

    login_url: str
    form_action: str
    sitekey: str


@dataclass(frozen=True, slots=True)
class _Authorized:
    identity: ServiceIdentity


@dataclass(frozen=True, slots=True)
class _Anonymous:
    pass


@dataclass(frozen=True, slots=True)
class _HealthFailure:
    error: ClientError


_HealthOutcome = _Authorized | _Anonymous | _HealthFailure


class AuthCoordinator:
    """Own the Habr session lifecycle for one ``(deps, section)`` pair."""

    def __init__(
        self,
        deps: ClientDeps,
        transport: HabrTransport,
        captcha: LoginCaptcha,
        *,
        resume_id: str | None = None,
    ) -> None:
        self._deps = deps
        self._transport = transport
        self._captcha = captcha
        self._resume_id = resume_id
        self._identity: ServiceIdentity | None = None
        self._store: AtomicJsonStore[PersistedHabrSession] = AtomicJsonStore(
            data_dir=deps.data_dir,
            service="habr",
            profile_id=deps.profile_id or "default",
            filename=SESSION_FILENAME,
            model=PersistedHabrSession,
            entity="the Habr session",
        )

    @property
    def identity(self) -> ServiceIdentity:
        """The authorized identity; raises when read before :meth:`authorize`."""
        if self._identity is None:
            raise RuntimeError("Habr identity requested before authorize()")
        return self._identity

    async def authorize(self) -> Result[None, ClientError]:
        """Ensure an authenticated session, reusing persisted cookies when possible."""
        state = await self._store.load()
        if state is not None and state.cookies:
            self._transport.restore_cookies(state.cookies)
        health = await self._healthcheck()
        match health:
            case _Authorized(identity):
                # The board re-issues ``_career_session`` on ordinary requests
                # (e.g. a reuse driven by a lone ``remember_user_token``); a
                # successful healthcheck that changed the cookie jar must
                # persist the fresh snapshot, or the next run reloads stale
                # cookies.
                reissued = state is not None and _cookies_differ(self._transport.snapshot_cookies(), state.cookies)
                return await self._accept(identity, persist=state is None or reissued)
            case _HealthFailure(error):
                return Err(error)
            case _Anonymous():
                return await self._recover()

    async def aclose(self) -> None:
        """Close the shared transport (idempotent); no long-lived browser to close."""
        await self._transport.aclose()

    async def _recover(self) -> Result[None, ClientError]:
        """Retry a lone ``remember_user_token`` once, else run the full SSO login."""
        if self._transport.cookie_value(_REMEMBER_COOKIE) is not None:
            retry = await self._healthcheck()
            match retry:
                case _Authorized(identity):
                    return await self._accept(identity, persist=True)
                case _HealthFailure(error):
                    return Err(error)
                case _Anonymous():
                    pass
        return await self._full_login()

    async def _full_login(self) -> Result[None, ClientError]:  # noqa: PLR0911 - a Result cascade: each guard returns early
        """Run the browserless SSO login chain and adopt the resulting session."""
        surface_result = await self._resolve_login_surface()
        if surface_result.is_err:
            return Err(surface_result.unwrap_err())
        surface = surface_result.unwrap()
        token_result = await self._captcha.solve(surface.login_url, surface.sitekey)
        if token_result.is_err:
            return Err(token_result.unwrap_err())
        rurl_result = await self._submit_login(surface.form_action, token_result.unwrap())
        if rurl_result.is_err:
            return Err(rurl_result.unwrap_err())
        followed = await self._follow_redirects(rurl_result.unwrap(), max_hops=_MAX_LOGIN_HOPS)
        if followed.is_err:
            return Err(followed.unwrap_err())
        health = await self._healthcheck()
        match health:
            case _Authorized(identity):
                return await self._accept(identity, persist=True)
            case _HealthFailure(error):
                return Err(error)
            case _Anonymous():
                return Err(AuthError(message="Habr login completed but the session is still anonymous"))

    async def _resolve_login_surface(self) -> Result[LoginSurface, ClientError]:
        """Follow the OAuth ``Location`` chain to the login page and parse it.

        The chain length is server-driven, so it is walked hop by hop (bounded by
        :data:`_MAX_LOGIN_HOPS`) until the resolved URL is the ``/ru/ident/``
        login page, rather than assuming exactly two hops.
        """
        current = f"{CAREER_ORIGIN}{_TMID_PATH}"
        for _ in range(_MAX_LOGIN_HOPS):
            location_result = await self._follow_location(current)
            if location_result.is_err:
                return Err(location_result.unwrap_err())
            current = location_result.unwrap()
            if _IDENT_PATH_MARKER in current:
                return await self._parse_login_page(current)
        return Err(ProtocolError(message="Habr login chain did not reach the login page"))

    async def _parse_login_page(self, url: str) -> Result[LoginSurface, ClientError]:
        """Fetch and parse the resolved login page's form action and sitekey."""
        page_result = await self._transport.get_absolute(url)
        if page_result.is_err:
            return Err(page_result.unwrap_err())
        page = page_result.unwrap()
        if page.status_code != _HTTP_OK:
            return Err(ProtocolError(message="Habr login page is unavailable", status=page.status_code))
        surface = _parse_login_surface(page.text, str(page.request.url))
        if surface is None:
            return Err(ProtocolError(message="Habr login page carried no login form"))
        return Ok(surface)

    async def _follow_location(self, url: str) -> Result[str, ClientError]:
        """Perform one login-chain GET and return the resolved next ``Location``.

        The whole chain stays on ``account.habr.com``; a hop that leaves it (or a
        missing ``Location``) is a protocol change, not a login step.
        """
        response_result = await self._transport.get_absolute(url, headers=(("accept", "text/html"),))
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code not in _REDIRECT_STATUSES:
            return Err(ProtocolError(message="Habr login chain did not redirect", status=response.status_code))
        location = header_value(response, "location")
        if location is None:
            return Err(ProtocolError(message="Habr login redirect carried no Location", status=response.status_code))
        resolved = urljoin(str(response.request.url), location)
        if not resolved.startswith(ACCOUNT_ORIGIN):
            return Err(ProtocolError(message="Habr login chain left account.habr.com"))
        return Ok(resolved)

    async def _submit_login(self, form_action: str, token: str) -> Result[str, ClientError]:
        """POST the login form and return the callback ``rurl`` on success."""
        fields = (
            ("email", self._deps.credentials.login),
            ("password", self._deps.credentials.password),
            ("smart-token", token),
        )
        response_result = await self._transport.post_form(form_action, fields=fields, headers=_LOGIN_POST_HEADERS)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(AuthError(message="Habr login form submission was rejected"))
        decoded = decode_json(response, HabrLoginResponse, operation="login")
        if decoded.is_err:
            return Err(decoded.unwrap_err())
        payload = decoded.unwrap()
        if not payload.success or payload.rurl is None:
            return Err(AuthError(message=payload.error or "Habr login failed"))
        if not payload.rurl.startswith(ACCOUNT_ORIGIN):
            return Err(ProtocolError(message="Habr login callback left account.habr.com"))
        return Ok(payload.rurl)

    async def _follow_redirects(self, url: str, *, max_hops: int) -> Result[None, ClientError]:
        """Follow the callback chain to a final ``200`` with the same cookie jar."""
        current = url
        for _ in range(max_hops):
            response_result = await self._transport.get_absolute(current, headers=(("accept", "text/html"),))
            if response_result.is_err:
                return Err(response_result.unwrap_err())
            response = response_result.unwrap()
            if response.status_code in _REDIRECT_STATUSES:
                location = header_value(response, "location")
                if location is None:
                    return Err(ProtocolError(message="Habr callback redirect carried no Location"))
                current = urljoin(str(response.request.url), location)
                continue
            if response.status_code == _HTTP_OK:
                return Ok(None)
            return Err(ProtocolError(message="Habr login callback failed", status=response.status_code))
        return Err(ProtocolError(message="Habr login callback exceeded the redirect budget"))

    async def _healthcheck(self) -> _HealthOutcome:
        """Read the identity endpoint and classify authorized / anonymous / failure."""
        response_result = await self._transport.get_json(_IDENTITY_PATH)
        if response_result.is_err:
            return _HealthFailure(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code in _ANONYMOUS_STATUSES:
            return _Anonymous()
        if response.status_code != _HTTP_OK:
            return _HealthFailure(ProtocolError(message="Habr identity endpoint failed", status=response.status_code))
        decoded = decode_json(response, HabrIdentityResponse, operation="identity")
        if decoded.is_err:
            return _HealthFailure(decoded.unwrap_err())
        user = decoded.unwrap().user
        if user is None:
            return _Anonymous()
        identity = ServiceIdentity(external_id=user.alias, display_name=user.full_name, email=user.email)
        return _Authorized(identity)

    async def _accept(self, identity: ServiceIdentity, *, persist: bool) -> Result[None, ClientError]:
        """Adopt an identity after validating the configured resume, optionally persisting."""
        mismatch = _resume_mismatch(self._resume_id, identity)
        if mismatch is not None:
            return Err(ConfigurationError(message=mismatch))
        self._identity = identity
        await self._deps.reporter.publish(
            RunEvent(stage="authorize", message=f"Habr: authorized as {identity.external_id}", level="info")
        )
        if not persist:
            return Ok(None)
        snapshot = PersistedHabrSession(
            cookies=self._transport.snapshot_cookies(),
            identity=PersistedHabrIdentity(
                external_id=identity.external_id,
                display_name=identity.display_name,
                email=identity.email,
            ),
        )
        return await self._store.save(snapshot)


def _resume_mismatch(configured: str | None, identity: ServiceIdentity) -> str | None:
    """Return a ConfigurationError message when the configured resume id disagrees."""
    if configured is None or configured == identity.external_id:
        return None
    return f"configured resume_id '{configured}' does not match the Habr account alias '{identity.external_id}'"


def _cookies_differ(current: tuple[PersistedCookie, ...], previous: tuple[PersistedCookie, ...]) -> bool:
    """Whether two cookie snapshots differ in any cookie's value (order-insensitive)."""
    return _cookie_signature(current) != _cookie_signature(previous)


def _cookie_signature(cookies: tuple[PersistedCookie, ...]) -> tuple[tuple[str, str, str, str], ...]:
    """Sort cookies by identity (name/domain/path) with their value for comparison."""
    return tuple(
        sorted((cookie.name, cookie.domain.lstrip(".").casefold(), cookie.path, cookie.value) for cookie in cookies)
    )


def _parse_login_surface(html: str, page_url: str) -> LoginSurface | None:
    """Extract the login form action and sitekey from the SSR login page."""
    action_match = _IDENT_FORM_ACTION_RE.search(html)
    sitekey_match = _SITEKEY_RE.search(html)
    if action_match is None or sitekey_match is None:
        return None
    return LoginSurface(login_url=page_url, form_action=action_match.group(1), sitekey=sitekey_match.group(1))
