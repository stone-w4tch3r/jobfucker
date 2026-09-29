"""BDD acceptance for the Habr session lifecycle (test/AGENTS.md §3a, §3b).

Drives ``AuthCoordinator`` through a routed ``httpx.MockTransport`` (the full
SSO redirect chain) and the scripted browser double (the login captcha). The
``When`` returns a frozen outcome carrying the error, identity, request history,
and whether the transport was closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs

import httpx
from pytest_bdd import given, parsers, scenarios, then, when

from jobfucker.clients.base import (
    ClientError,
    ConfigurationError,
    ProtocolError,
    ServiceIdentity,
)
from jobfucker.clients.habr.auth import AuthCoordinator
from jobfucker.clients.habr.models import PersistedHabrSession
from jobfucker.clients.habr.transport import HabrTransport
from jobfucker.clients.shared.cookies import PersistedCookie
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver, FakeHabrBrowserSession
from test.clients.habr.helpers import (
    DEFAULT_ALIAS,
    DEFAULT_LOGIN,
    DEFAULT_PASSWORD,
    RecordingSolver,
    ScriptedResponses,
    anonymous_identity,
    authorized_identity,
    habr_captcha,
    habr_deps,
    habr_transport,
    html_response,
    login_page_html,
    login_success,
    profile_id_for,
    seed_habr_session,
    session_path,
)

scenarios("bdd/authentication.feature")

CAREER: Final = "career.habr.com"
ACCOUNT: Final = "account.habr.com"
IDENTITY_PATH: Final = "/api/frontend_v1/users/me"
TMID_PATH: Final = "/users/auth/tmid"
AUTHORIZE_PATH: Final = "/oauth/authorize/"
EXTRA_HOP_PATH: Final = "/oauth/authorize/step-2"
IDENT_PATH: Final = "/ru/ident/state-1"
LOGIN_IN_PATH: Final = "/ru/ident/in/state-1"
AUTHORIZE_DONE_PATH: Final = "/oauth/authorize/done/abc"
CALLBACK_PATH: Final = "/users/auth/tmid/callback_oauth"
SITEKEY: Final = "ysc1_test"
BROWSER_TOKEN: Final = "browser-captcha-token"

_CAREER_SESSION_COOKIE: Final = "_career_session=login-value; Domain=career.habr.com; Path=/; Secure; HttpOnly"


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


def _anonymous_identity_status(status: int) -> httpx.Response:
    """A non-200 identity answer: a redirect carries the auth_required Location."""
    headers = {"location": "https://career.habr.com/users/auth_required"} if 300 <= status < 400 else {}
    return httpx.Response(status, headers=headers, text="")


class HabrAuthRouter:
    """Dumb router over the persistent Habr SSO endpoints (no scenario enum)."""

    def __init__(
        self,
        *,
        healthchecks: ScriptedResponses,
        career_session_cookie: str | None,
        extra_login_hops: tuple[str, ...] = (),
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._healthchecks = healthchecks
        self._career_session_cookie = career_session_cookie
        chain = (*extra_login_hops, IDENT_PATH)
        self._login_chain = chain
        self._login_next = {path: chain[index + 1] for index, path in enumerate(chain[:-1])}

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one branch per faked endpoint
        self.requests.append(request)
        host, path, method = request.url.host, request.url.path, request.method
        if host == CAREER and path == IDENTITY_PATH:
            return self._healthchecks.next()
        if host == CAREER and path == TMID_PATH and method == "GET":
            return _redirect(f"https://{ACCOUNT}/oauth/authorize/?client_id=habr&response_type=code")
        if host == ACCOUNT and path == AUTHORIZE_PATH and method == "GET":
            return _redirect(f"https://{ACCOUNT}{self._login_chain[0]}")
        if host == ACCOUNT and path in self._login_next and method == "GET":
            return _redirect(f"https://{ACCOUNT}{self._login_next[path]}")
        if host == ACCOUNT and path == IDENT_PATH and method == "GET":
            return html_response(login_page_html(action=f"https://{ACCOUNT}{LOGIN_IN_PATH}", sitekey=SITEKEY))
        if host == ACCOUNT and path == LOGIN_IN_PATH and method == "POST":
            return login_success(f"https://{ACCOUNT}{AUTHORIZE_DONE_PATH}")
        if host == ACCOUNT and path == AUTHORIZE_DONE_PATH and method == "GET":
            return _redirect(f"https://{CAREER}{CALLBACK_PATH}?code=authorization-code")
        if host == CAREER and path == CALLBACK_PATH and method == "GET":
            return _redirect(f"https://{CAREER}/")
        if host == CAREER and path == "/" and method == "GET":
            headers = {"set-cookie": self._career_session_cookie} if self._career_session_cookie else {}
            return httpx.Response(200, headers=headers, text="<html><body>career</body></html>")
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


@dataclass(frozen=True, slots=True)
class AuthScenario:
    """Frozen setup: the router, transport, coordinator, and persisted-session path."""

    router: HabrAuthRouter
    transport: HabrTransport
    coordinator: AuthCoordinator
    session_file: Path


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """Frozen outcome: the error, the identity, the requests, and the closed state."""

    error: ClientError | None
    identity: ServiceIdentity | None
    requests: tuple[httpx.Request, ...]
    closed: bool


async def _authorize(coordinator: AuthCoordinator) -> tuple[ClientError | None, ServiceIdentity | None]:
    result = await coordinator.authorize()
    if result.is_err:
        return (result.unwrap_err(), None)
    return (None, coordinator.identity)


async def _authorize_and_close(
    coordinator: AuthCoordinator, transport: HabrTransport
) -> tuple[ClientError | None, ServiceIdentity | None, bool]:
    error, identity = await _authorize(coordinator)
    await coordinator.aclose()
    await coordinator.aclose()
    return (error, identity, transport.client.is_closed)


def _scenario(
    tmp_path: Path,
    *,
    healthchecks: ScriptedResponses,
    cookies: tuple[PersistedCookie, ...] = (),
    seeded: bool = False,
    resume_id: str | None = None,
    career_session_cookie: str | None = None,
    extra_login_hops: tuple[str, ...] = (),
) -> AuthScenario:
    profile_id = profile_id_for(DEFAULT_LOGIN)
    router = HabrAuthRouter(
        healthchecks=healthchecks,
        career_session_cookie=career_session_cookie,
        extra_login_hops=extra_login_hops,
    )
    transport = habr_transport(router)
    data_dir = tmp_path / "data"
    if seeded:
        seed_habr_session(data_dir, profile_id, cookies=cookies)
    deps = habr_deps(data_dir, profile_id, RecordingSolver(), login=DEFAULT_LOGIN, password=DEFAULT_PASSWORD)
    driver = FakeHabrBrowserDriver(FakeHabrBrowserSession(token=BROWSER_TOKEN))
    captcha = habr_captcha(transport, deps.captcha_handler, driver=driver, max_attempts=4)
    coordinator = AuthCoordinator(deps, transport, captcha, resume_id=resume_id)
    return AuthScenario(
        router=router,
        transport=transport,
        coordinator=coordinator,
        session_file=session_path(data_dir, profile_id),
    )


@given("a valid persisted Habr session", target_fixture="auth_scenario")
def valid_session_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="_career_session", value="seed", domain="career.habr.com"),),
        healthchecks=ScriptedResponses(authorized_identity()),
    )


@given(
    "a valid persisted Habr session whose identity call re-issues the session cookie",
    target_fixture="auth_scenario",
)
def reissued_session_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="_career_session", value="stale", domain="career.habr.com"),),
        healthchecks=ScriptedResponses(authorized_identity(career_session="reissued")),
    )


@given("no persisted Habr session", target_fixture="auth_scenario")
def no_session_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        healthchecks=ScriptedResponses(anonymous_identity(), authorized_identity()),
        career_session_cookie=_CAREER_SESSION_COOKIE,
    )


@given("no persisted Habr session and a longer OAuth login chain", target_fixture="auth_scenario")
def longer_login_chain_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        healthchecks=ScriptedResponses(anonymous_identity(), authorized_identity()),
        career_session_cookie=_CAREER_SESSION_COOKIE,
        extra_login_hops=(EXTRA_HOP_PATH,),
    )


@given(
    parsers.parse("a stale persisted Habr session whose identity call answers {status:d}"),
    target_fixture="auth_scenario",
)
def stale_session_step(tmp_path: Path, status: int) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="_career_session", value="stale", domain="career.habr.com"),),
        healthchecks=ScriptedResponses(_anonymous_identity_status(status), authorized_identity()),
        career_session_cookie=_CAREER_SESSION_COOKIE,
    )


@given("a persisted session with only a remember_user_token", target_fixture="auth_scenario")
def remember_only_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="remember_user_token", value="remember", domain=".habr.com"),),
        healthchecks=ScriptedResponses(anonymous_identity(), authorized_identity(career_session="refreshed")),
    )


@given("a persisted session whose remember_user_token retry stays anonymous", target_fixture="auth_scenario")
def remember_then_login_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="remember_user_token", value="remember", domain=".habr.com"),),
        healthchecks=ScriptedResponses(anonymous_identity(), anonymous_identity(), authorized_identity()),
        career_session_cookie=_CAREER_SESSION_COOKIE,
    )


@given(
    "a valid persisted Habr session and a configured resume_id different from the account alias",
    target_fixture="auth_scenario",
)
def resume_mismatch_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        cookies=(PersistedCookie(name="_career_session", value="seed", domain="career.habr.com"),),
        healthchecks=ScriptedResponses(authorized_identity()),
        resume_id="some-other-alias",
    )


@given("no persisted Habr session and a malformed identity response", target_fixture="auth_scenario")
def malformed_identity_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, healthchecks=ScriptedResponses(httpx.Response(200, text="<html>nope</html>")))


@when("the Habr client authorizes", target_fixture="auth_outcome")
def authorize_step(auth_scenario: AuthScenario) -> AuthOutcome:
    error, identity = async_run(_authorize(auth_scenario.coordinator))
    return AuthOutcome(error=error, identity=identity, requests=tuple(auth_scenario.router.requests), closed=False)


@when("the Habr client authorizes and closes twice", target_fixture="auth_outcome")
def authorize_and_close_step(auth_scenario: AuthScenario) -> AuthOutcome:
    error, identity, closed = async_run(_authorize_and_close(auth_scenario.coordinator, auth_scenario.transport))
    return AuthOutcome(error=error, identity=identity, requests=tuple(auth_scenario.router.requests), closed=closed)


@then("authorization succeeds")
def assert_success_step(auth_outcome: AuthOutcome) -> None:
    assert auth_outcome.error is None


@then("authorization fails with a configuration error")
def assert_configuration_error_step(auth_outcome: AuthOutcome) -> None:
    assert isinstance(auth_outcome.error, ConfigurationError)


@then("authorization fails with a protocol error")
def assert_protocol_error_step(auth_outcome: AuthOutcome) -> None:
    assert isinstance(auth_outcome.error, ProtocolError)


@then("the identity carries the account alias, name, and email")
def assert_identity_step(auth_outcome: AuthOutcome) -> None:
    identity = auth_outcome.identity
    assert identity is not None
    assert identity.external_id == DEFAULT_ALIAS
    assert identity.display_name == "Хабр Человек"
    assert identity.email == f"{DEFAULT_ALIAS}@example.test"


@then("the persisted session is reused with exactly one identity check")
def assert_single_healthcheck_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [IDENTITY_PATH]


@then("the full login chain ran")
def assert_full_login_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        IDENTITY_PATH,
        TMID_PATH,
        AUTHORIZE_PATH,
        IDENT_PATH,
        LOGIN_IN_PATH,
        AUTHORIZE_DONE_PATH,
        CALLBACK_PATH,
        "/",
        IDENTITY_PATH,
    ]


@then("the full login chain ran after the remember retry")
def assert_full_login_after_retry_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        IDENTITY_PATH,
        IDENTITY_PATH,
        TMID_PATH,
        AUTHORIZE_PATH,
        IDENT_PATH,
        LOGIN_IN_PATH,
        AUTHORIZE_DONE_PATH,
        CALLBACK_PATH,
        "/",
        IDENTITY_PATH,
    ]


@then("the full login chain ran through the extra hop")
def assert_long_login_chain_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        IDENTITY_PATH,
        TMID_PATH,
        AUTHORIZE_PATH,
        EXTRA_HOP_PATH,
        IDENT_PATH,
        LOGIN_IN_PATH,
        AUTHORIZE_DONE_PATH,
        CALLBACK_PATH,
        "/",
        IDENTITY_PATH,
    ]


@then("the login submission carried the browser captcha token")
def assert_login_submission_step(auth_outcome: AuthOutcome) -> None:
    submission = next(
        request for request in auth_outcome.requests if request.url.path == LOGIN_IN_PATH and request.method == "POST"
    )
    form = parse_qs(submission.content.decode())
    assert form.get("email") == [DEFAULT_LOGIN]
    assert form.get("password") == [DEFAULT_PASSWORD]
    assert form.get("smart-token") == [BROWSER_TOKEN]


@then("the session was persisted with career cookies and the identity")
def assert_persisted_step(auth_scenario: AuthScenario) -> None:
    assert auth_scenario.session_file.is_file()
    persisted = PersistedHabrSession.model_validate_json(auth_scenario.session_file.read_text(encoding="utf-8"))
    assert any(cookie.name == "_career_session" for cookie in persisted.cookies)
    assert persisted.identity is not None
    assert persisted.identity.external_id == DEFAULT_ALIAS


@then(parsers.parse('the persisted session carries the cookie value "{value}"'))
def assert_reissued_cookie_step(auth_scenario: AuthScenario, value: str) -> None:
    persisted = PersistedHabrSession.model_validate_json(auth_scenario.session_file.read_text(encoding="utf-8"))
    assert any(cookie.name == "_career_session" and cookie.value == value for cookie in persisted.cookies)


@then("the remember_user_token was retried exactly once without a login chain")
def assert_remember_retry_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [IDENTITY_PATH, IDENTITY_PATH]
    assert not any(request.url.path == TMID_PATH for request in auth_outcome.requests)


@then("the transport is closed exactly once")
def assert_transport_closed_step(auth_outcome: AuthOutcome) -> None:
    assert auth_outcome.closed is True


def _paths(auth_outcome: AuthOutcome) -> list[str]:
    return [request.url.path for request in auth_outcome.requests]
