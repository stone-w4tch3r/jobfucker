"""HH browserless login, OAuth, token persistence, and healthcheck coordination."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    AuthError,
    ClientDeps,
    ClientError,
    InternalError,
    ProtocolError,
)
from jobfucker.clients.hh.captcha import (
    CaptchaAnswer,
    CaptchaCoordinator,
    Challenge,
    ChallengeKind,
    challenge_error,
    classify_challenge,
)
from jobfucker.clients.hh.models import CurrentUserResponse, LoginResponse, OAuthTokenResponse, PersistedAuthState
from jobfucker.clients.hh.transport import FormFields, HHTransport
from jobfucker.reporting import RunEvent

_CLIENT_ID: Final = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
# Documented third-party OAuth application credential; this is not a user secret.
_CLIENT_SECRET: Final = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"  # noqa: S105
_REDIRECT_URI: Final = "hhandroid://oauthresponse"
_EXPIRY_SKEW_SECONDS: Final = 30.0
_HTTP_OK: Final = 200
_HTTP_FOUND: Final = 302
_AUTH_REJECTED_STATUSES: Final = frozenset({401, 403})
_LOGIN_PATH: Final = "/account/login"
_LOGIN_PARAMS: Final[FormFields] = (("backurl", "/"), ("oauth", "true"), ("response_type", "code"))
_LOGIN_FAIL_URL: Final = "/account/login?backurl=/&oauth=true&response_type=code"


@dataclass(frozen=True, slots=True)
class _HealthcheckAuthorized:
    """The Bearer token belongs to a healthy applicant account."""


@dataclass(frozen=True, slots=True)
class _HealthcheckRejected:
    """HH rejected the Bearer token and a full login may recover it."""


@dataclass(frozen=True, slots=True)
class _HealthcheckFailed:
    """The healthcheck failed in a way reauthentication cannot safely fix."""

    error: ClientError


_Healthcheck = _HealthcheckAuthorized | _HealthcheckRejected | _HealthcheckFailed


class TokenStore:
    """Atomically persist one validated HH authentication snapshot."""

    def __init__(self, deps: ClientDeps) -> None:
        profile_id = deps.profile_id
        if profile_id is None or not profile_id.strip():
            profile_id = hashlib.sha256(deps.credentials.login.strip().casefold().encode()).hexdigest()
        self._directory = deps.data_dir / "hh" / profile_id
        self._path = self._directory / "auth-state.json"

    async def load(self) -> PersistedAuthState | None:
        """Load validated state; missing or corrupt state is treated as unusable."""
        return await asyncio.to_thread(self._load_sync)

    async def save(self, state: PersistedAuthState) -> Result[None, ClientError]:
        """Write a complete snapshot atomically with user-only permissions."""
        try:
            await asyncio.to_thread(self._save_sync, state)
        except OSError as exc:
            return Err(InternalError(message=f"Cannot persist HH authentication state: {type(exc).__name__}"))
        return Ok(None)

    def _load_sync(self) -> PersistedAuthState | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            return PersistedAuthState.model_validate_json(raw)
        except FileNotFoundError, OSError, ValidationError:
            return None

    def _save_sync(self, state: PersistedAuthState) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._directory.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".auth-state-", dir=self._directory, text=True)
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            self._path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path.exists():
                temporary_path.unlink()


class AuthCoordinator:
    """Ensure a healthy applicant Bearer session before every client action."""

    def __init__(self, deps: ClientDeps, transport: HHTransport, captcha: CaptchaCoordinator) -> None:
        self._deps = deps
        self._transport = transport
        self._captcha = captcha
        self._store = TokenStore(deps)
        self._access_token: str | None = None

    @property
    def authorized_access_token(self) -> str:
        """Return the token established by the latest successful preflight."""
        if self._access_token is None:
            raise RuntimeError("HH access token requested before successful authorization")
        return self._access_token

    async def ensure_authorized(self) -> Result[None, ClientError]:
        """Healthcheck persisted state and perform one bounded recovery when needed."""
        state = await self._store.load()
        if state is not None:
            self._transport.restore_cookies(state.cookies)
            if _is_expired(state):
                refreshed = await self._refresh(state.refresh_token)
                if refreshed.is_ok:
                    verified = await self._verify_and_persist(refreshed.unwrap())
                    if verified.is_ok:
                        return verified
                    if not isinstance(verified.unwrap_err(), AuthError):
                        return verified
            else:
                health = await self._healthcheck(state.access_token)
                match health:
                    case _HealthcheckAuthorized():
                        self._access_token = state.access_token
                        await self._report("authorize: token healthy")
                        return Ok(None)
                    case _HealthcheckFailed(error=error):
                        return Err(error)
                    case _HealthcheckRejected():
                        pass

        fresh = await self._full_authorization()
        if fresh.is_err:
            return Err(fresh.unwrap_err())
        return await self._verify_and_persist(fresh.unwrap())

    async def _verify_and_persist(self, state: PersistedAuthState) -> Result[None, ClientError]:
        health = await self._healthcheck(state.access_token)
        match health:
            case _HealthcheckAuthorized():
                saved = await self._store.save(state)
                if saved.is_err:
                    return saved
                self._access_token = state.access_token
                await self._report("authorize: applicant healthcheck passed")
                return Ok(None)
            case _HealthcheckRejected():
                return Err(AuthError(message="HH rejected newly-issued authorization"))
            case _HealthcheckFailed(error=error):
                return Err(error)

    async def _healthcheck(self, access_token: str, *, allow_captcha_recovery: bool = True) -> _Healthcheck:
        response_result = await self._transport.get_api("/me", access_token)
        if response_result.is_err:
            return _HealthcheckFailed(response_result.unwrap_err())
        response = response_result.unwrap()
        challenge = classify_challenge(response, context="api")
        if challenge is not None:
            return await self._recover_healthcheck_challenge(
                access_token,
                challenge,
                allow_recovery=allow_captcha_recovery,
            )
        return _decode_healthcheck(response)

    async def _recover_healthcheck_challenge(
        self,
        access_token: str,
        challenge: Challenge,
        *,
        allow_recovery: bool,
    ) -> _Healthcheck:
        """Solve once and replay once; challenge re-entry fails without another solve."""
        if not allow_recovery:
            return _HealthcheckFailed(challenge_error(challenge))
        solved = await self._captcha.solve(challenge)
        if solved.is_err:
            return _HealthcheckFailed(solved.unwrap_err())
        return await self._healthcheck(access_token, allow_captcha_recovery=False)

    async def _refresh(self, refresh_token: str) -> Result[PersistedAuthState, ClientError]:
        await self._report("authorize: refreshing expired token")
        response_result = await self._transport.post_oauth(
            (("grant_type", "refresh_token"), ("refresh_token", refresh_token))
        )
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        return self._decode_token(response_result.unwrap())

    async def _full_authorization(self) -> Result[PersistedAuthState, ClientError]:
        await self._report("authorize: starting credential OAuth flow")
        login = await self._login()
        if login.is_err:
            return Err(login.unwrap_err())
        code = await self._capture_authorization_code()
        if code.is_err:
            return Err(code.unwrap_err())
        return await self._exchange_authorization_code(code.unwrap())

    async def _login(self) -> Result[None, ClientError]:
        """Establish an OAuth-capable website cookie session."""
        xsrf = await self._login_xsrf()
        if xsrf.is_err:
            return Err(xsrf.unwrap_err())
        return await self._submit_credentials(xsrf.unwrap())

    async def _login_xsrf(self) -> Result[str, ClientError]:
        """Open the login surface and obtain its cookie-bound XSRF value."""
        login_page = await self._get_website_with_captcha(_LOGIN_PATH, params=_LOGIN_PARAMS)
        if login_page.is_err:
            return Err(login_page.unwrap_err())
        response = login_page.unwrap()
        if _is_login_bootstrap_redirect(response):
            replay = await self._get_website_with_captcha(_LOGIN_PATH, params=_LOGIN_PARAMS)
            if replay.is_err:
                return Err(replay.unwrap_err())
            response = replay.unwrap()
        if response.status_code != _HTTP_OK:
            return Err(AuthError(message=f"HH login page is unavailable (status {response.status_code})"))
        xsrf = self._transport.cookie_value("_xsrf")
        if xsrf is None:
            return Err(ProtocolError(message="HH login did not issue an XSRF cookie"))
        return Ok(xsrf)

    async def _submit_credentials(self, xsrf: str) -> Result[None, ClientError]:
        """Submit resolved applicant credentials and require an OAuth-capable cookie."""
        login_response = await self._post_login(xsrf)
        if login_response.is_err:
            return Err(login_response.unwrap_err())
        response = login_response.unwrap()
        challenge = classify_challenge(response, context="login")
        if challenge is not None:
            if challenge.kind is not ChallengeKind.EMBEDDED_LOGIN:
                return Err(challenge_error(challenge))
            recovered = await self._captcha.solve_login(
                challenge,
                xsrf=xsrf,
                submit=lambda answer, state: self._post_login(xsrf, answer=answer, captcha_state=state),
            )
            if recovered.is_err:
                return Err(recovered.unwrap_err())
            response = recovered.unwrap()
        login_error = _login_error(response)
        if login_error is not None:
            return Err(login_error)
        if self._transport.cookie_value("hhtoken") is None:
            return Err(AuthError(message="HH credential login did not establish an OAuth-capable session"))
        return Ok(None)

    async def _post_login(
        self,
        xsrf: str,
        *,
        answer: CaptchaAnswer | None = None,
        captcha_state: str | None = None,
    ) -> Result[httpx.Response, ClientError]:
        """Send the researched legacy OAuth login form, optionally with one CAPTCHA answer."""
        credentials = self._deps.credentials
        fields: FormFields = (
            ("_xsrf", xsrf),
            ("failUrl", _LOGIN_FAIL_URL),
            ("accountType", "EMPLOYER"),
            ("remember", "yes"),
            ("username", credentials.login),
            ("password", credentials.password),
            ("password", credentials.password),
            ("isBot", "false"),
        )
        if answer is not None:
            if captcha_state is None:
                return Err(ProtocolError(message="HH embedded login CAPTCHA has no state"))
            fields += (
                ("captchaKey", answer.key),
                ("captchaText", answer.text),
                ("captchaState", captcha_state),
            )
        return await self._transport.post_login(fields, xsrf=xsrf)

    async def _capture_authorization_code(self) -> Result[str, ClientError]:
        """Request and validate one custom-scheme OAuth code redirect."""
        state = secrets.token_urlsafe(32)
        authorize = await self._get_website_with_captcha(
            "/oauth/authorize",
            params=(
                ("client_id", _CLIENT_ID),
                ("redirect_uri", _REDIRECT_URI),
                ("response_type", "code"),
                ("state", state),
                ("skip_choose_account", "true"),
                ("oauth", "true"),
                ("hhtmFrom", "account_login"),
            ),
        )
        if authorize.is_err:
            return Err(authorize.unwrap_err())
        return _authorization_code(authorize.unwrap(), state)

    async def _get_website_with_captcha(
        self,
        path: str,
        *,
        params: FormFields = (),
        allow_recovery: bool = True,
    ) -> Result[httpx.Response, ClientError]:
        """Classify every website GET, solve one supported challenge, and replay once."""
        response_result = await self._transport.get_website(path, params=params)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        challenge = classify_challenge(response, context="website")
        if challenge is None:
            return Ok(response)
        if not allow_recovery:
            return Err(challenge_error(challenge))
        solved = await self._captcha.solve(challenge)
        if solved.is_err:
            return Err(solved.unwrap_err())
        return await self._get_website_with_captcha(path, params=params, allow_recovery=False)

    async def _exchange_authorization_code(self, code: str) -> Result[PersistedAuthState, ClientError]:
        """Exchange a validated authorization code for a token pair."""
        token_response = await self._transport.post_oauth(
            (
                ("client_id", _CLIENT_ID),
                ("client_secret", _CLIENT_SECRET),
                ("code", code),
                ("grant_type", "authorization_code"),
                ("redirect_uri", _REDIRECT_URI),
            )
        )
        if token_response.is_err:
            return Err(token_response.unwrap_err())
        return self._decode_token(token_response.unwrap())

    def _decode_token(self, response: httpx.Response) -> Result[PersistedAuthState, ClientError]:
        challenge = classify_challenge(response, context="website")
        if challenge is not None:
            return Err(challenge_error(challenge))
        if response.status_code != _HTTP_OK:
            return Err(AuthError(message=f"HH OAuth token request failed with status {response.status_code}"))
        try:
            token = OAuthTokenResponse.model_validate_json(response.content)
        except ValidationError:
            return Err(ProtocolError(message="Malformed HH OAuth token response", status=response.status_code))
        if token.token_type.casefold() != "bearer" or not token.access_token.startswith("USER"):
            return Err(ProtocolError(message="Invalid HH OAuth token response", status=response.status_code))
        return Ok(
            PersistedAuthState(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=time.time() + token.expires_in,
                cookies=self._transport.snapshot_cookies(),
            )
        )

    async def _report(self, message: str) -> None:
        await self._deps.reporter.publish(RunEvent(stage="client", message=message, level="debug"))


def _is_expired(state: PersistedAuthState) -> bool:
    return state.expires_at <= time.time() + _EXPIRY_SKEW_SECONDS


def _decode_healthcheck(response: httpx.Response) -> _Healthcheck:
    """Decode a challenge-free HH /me response into the auth state machine."""
    if response.status_code in _AUTH_REJECTED_STATUSES:
        return _HealthcheckRejected()
    if response.status_code != _HTTP_OK:
        return _HealthcheckFailed(ProtocolError(message="Unexpected HH /me status", status=response.status_code))
    try:
        current_user = CurrentUserResponse.model_validate_json(response.content)
    except ValidationError:
        return _HealthcheckFailed(ProtocolError(message="Malformed HH /me response", status=response.status_code))
    if current_user.auth_type != "applicant" or not current_user.is_applicant:
        return _HealthcheckFailed(AuthError(message="HH account is not an applicant account"))
    return _HealthcheckAuthorized()


def _login_error(response: httpx.Response) -> AuthError | None:
    """Extract a credential rejection without exposing the raw login response."""
    try:
        decoded = LoginResponse.model_validate_json(response.content)
    except ValidationError:
        return None
    if decoded.login_error is None:
        return None
    return AuthError(message=f"HH credential login was rejected ({decoded.login_error.code})")


def _is_login_bootstrap_redirect(response: httpx.Response) -> bool:
    """Accept only HH's bounded cookie-handshake redirect back to the same login surface."""
    if response.status_code != _HTTP_FOUND:
        return False
    location = next(
        (value for key, value in response.headers.multi_items() if key.casefold() == "location"),
        None,
    )
    if location is None:
        return False
    parsed = urlparse(location)
    if parsed.scheme and parsed.scheme != "https":
        return False
    if parsed.netloc and parsed.netloc != "hh.ru":
        return False
    if parsed.path != _LOGIN_PATH or parsed.fragment:
        return False
    return parse_qs(parsed.query) == {"backurl": ["/"], "oauth": ["true"], "response_type": ["code"]}


def _authorization_code(response: httpx.Response, expected_state: str) -> Result[str, ClientError]:
    if response.status_code != _HTTP_FOUND:
        return Err(
            ProtocolError(
                message="HH OAuth authorize did not return a code redirect",
                status=response.status_code,
            )
        )
    location = next(
        (value for key, value in response.headers.multi_items() if key.casefold() == "location"),
        None,
    )
    if location is None:
        return Err(ProtocolError(message="HH OAuth authorize redirect has no Location header", status=_HTTP_FOUND))
    parsed = urlparse(location)
    if parsed.scheme != "hhandroid" or parsed.netloc != "oauthresponse":
        return Err(ProtocolError(message="HH OAuth authorize returned an invalid redirect target", status=_HTTP_FOUND))
    query = parse_qs(parsed.query)
    code_values = query.get("code", [])
    state_values = query.get("state", [])
    if len(code_values) != 1 or not code_values[0]:
        return Err(ProtocolError(message="HH OAuth authorize redirect has no code", status=_HTTP_FOUND))
    if state_values and state_values != [expected_state]:
        return Err(ProtocolError(message="HH OAuth state mismatch", status=_HTTP_FOUND))
    return Ok(code_values[0])
