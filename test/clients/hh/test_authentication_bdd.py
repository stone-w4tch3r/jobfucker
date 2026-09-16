"""BDD acceptance coverage for HH authentication and per-action healthchecks.

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given`` seeds
a frozen ``AuthScenario`` (data-configured transport + client factory), the
``When`` returns a frozen ``AuthOutcome``, and ``Then`` steps only assert.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    AuthError,
    CaptchaSolvingError,
    ClientCredentials,
    ClientDeps,
    ClientError,
    ConfigurationError,
    ProtocolError,
    ServiceVacancyId,
    TransportError,
)
from jobfucker.clients.hh.auth import TokenStore
from jobfucker.clients.hh.browser import BrowserDriver
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHSearchEntry, HHServiceConfig
from jobfucker.clients.hh.models import PersistedAuthState
from jobfucker.testing.step_runner import async_run
from test.clients.hh.browser_fake import FakeBrowserDriver
from test.clients.hh.helpers import (
    DEFAULT_EXPIRES_AT,
    PNG_IMAGE,
    RecordingSolver,
    ScriptedResponses,
    StubAuthInteraction,
    applicant_healthcheck,
    hh_client,
    json_response,
    resume_item,
    seed_auth_state,
)

scenarios("bdd/authentication.feature")

_LOGIN = "person@example.test"
_PASSWORD = "test-password"
_PROFILE_ID = hashlib.sha256(_LOGIN.strip().casefold().encode()).hexdigest()
_TOKEN_PATH_PARTS = ("hh", _PROFILE_ID, "auth-state.json")
_CAPTCHA_STATE = "opaque-login-captcha-state"

LoginPostResponder = Callable[[httpx.Request, int], httpx.Response]
TokenResponder = Callable[[dict[str, list[str]]], httpx.Response]


class AuthTransport:
    """Stateful external-boundary fake serving realistic HH auth responses.

    A dumb router over the scripted data each ``Given`` configures: healthcheck
    and login-page scripts, a login-POST responder, an authorize-redirect
    script, and a token responder. No scenario enum lives here.
    """

    def __init__(
        self,
        *,
        healthchecks: ScriptedResponses,
        login_pages: ScriptedResponses,
        login_post: LoginPostResponder,
        authorize: AuthorizeScript,
        token: TokenResponder,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.captcha_keys_issued = 0
        self._healthchecks = healthchecks
        self._login_pages = login_pages
        self._login_post = login_post
        self._authorize = authorize
        self._token = token

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one branch per faked endpoint
        self.requests.append(request)
        host, path, method = request.url.host, request.url.path, request.method
        if host == "api.hh.ru" and path == "/me":
            return self._healthchecks.next()
        if host == "api.hh.ru" and path == "/vacancies":
            return json_response({"items": [], "found": 0, "page": 0, "pages": 0, "per_page": 100})
        if host == "api.hh.ru" and path == "/resumes/mine":
            return json_response(
                {
                    "items": [resume_item("resume-1", title="Python разработчик")],
                    "page": 0,
                    "pages": 0,
                    "per_page": 100,
                }
            )
        if host == "api.hh.ru" and path == "/vacancies/vacancy-1" and method == "GET":
            return json_response(
                {
                    "id": "vacancy-1",
                    "name": "Python разработчик",
                    "alternate_url": "https://hh.ru/vacancy/vacancy-1",
                    "description": "<p>Описание</p>",
                    "key_skills": [],
                    "salary": None,
                }
            )
        if host == "api.hh.ru" and path == "/negotiations" and method == "POST":
            return httpx.Response(201)
        if path == "/account/login" and method == "GET":
            return self._login_pages.next()
        if path == "/account/login" and method == "POST":
            prior_posts = sum(
                1 for sent in self.requests[:-1] if sent.url.path == "/account/login" and sent.method == "POST"
            )
            return self._login_post(request, prior_posts)
        if path == "/captcha" and method == "POST":
            self.captcha_keys_issued += 1
            return json_response({"key": f"login-image-key-{self.captcha_keys_issued}"})
        if path == "/captcha/picture" and method == "GET":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_IMAGE)
        if path == "/oauth/authorize":
            return self._authorize.response(request)
        if path == "/oauth/token":
            return self._token(parse_qs(request.content.decode()))
        raise AssertionError(f"Unexpected HH request: {request.method} {request.url}")


# --- Scripted endpoint data (assembled by the Givens) -----------------------


def _login_page() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"set-cookie": "_xsrf=xsrf-value; Domain=.hh.ru; Path=/; Secure"},
        text='<input type="hidden" name="_xsrf" value="xsrf-value">',
    )


def _login_bootstrap_redirect() -> httpx.Response:
    return httpx.Response(
        302,
        headers={
            "location": "/account/login?backurl=%2F&oauth=true&response_type=code",
            "set-cookie": "_xsrf=xsrf-value; Domain=.hh.ru; Path=/; Secure",
        },
    )


def _plain_login(content: bytes = b"{}") -> LoginPostResponder:
    """The ordinary credential-login success (or a dormant-recaptcha body)."""

    def respond(request: httpx.Request, post: int) -> httpx.Response:
        del request, post
        return httpx.Response(
            200,
            headers={"set-cookie": "hhtoken=website-session; Domain=.hh.ru; Path=/; Secure; HttpOnly"},
            content=content,
        )

    return respond


_DORMANT_RECAPTCHA_BODY = (
    b'{"recaptcha":{"siteKey":"configured-but-inactive"},'
    b'"hhcaptcha":{"isBot":false,"captchaError":null,"captchaKey":null}}'
)


def _failing_login(*, fail_once: bool) -> LoginPostResponder:
    """Transient (retried once) or persistent credential-login connection faults."""

    def respond(request: httpx.Request, post: int) -> httpx.Response:
        if fail_once and post == 0:
            raise httpx.ConnectError("transient connection failure", request=request)
        if not fail_once:
            raise httpx.ConnectError("persistent connection failure", request=request)
        return _plain_login()(request, post)

    return respond


class EmbeddedLogin:
    """Scripted embedded login-CAPTCHA responder, configured by flags not enums."""

    def __init__(
        self,
        *,
        reject_first: bool = False,
        exhaust: bool = False,
        drift_state: bool = False,
        mismatch: bool = False,
        state: str | None = _CAPTCHA_STATE,
    ) -> None:
        self._reject_first = reject_first
        self._exhaust = exhaust
        self._drift_state = drift_state
        self._mismatch = mismatch
        self._state = state
        self.submissions = 0

    def __call__(self, request: httpx.Request, post: int) -> httpx.Response:
        del post
        if b'name="captchaText"' not in request.content:
            return self._challenge(error=False, state=self._state)
        self.submissions += 1
        if self._exhaust or (self._reject_first and self.submissions == 1):
            return self._challenge(error=True, state=self._state)
        if self._drift_state:
            return self._challenge(error=True, state="different-captcha-state")
        if self._mismatch:
            return json_response(
                {
                    "hhcaptcha": {"isBot": False, "captchaError": None, "captchaKey": None},
                    "loginError": {"code": "mismatch"},
                    "userType": "anonymous",
                }
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "set-cookie": "hhtoken=website-session; Domain=.hh.ru; Path=/; Secure; HttpOnly",
            },
            json={
                "hhcaptcha": {"isBot": False, "captchaError": None, "captchaKey": None},
                "loginError": None,
                "userType": "applicant",
            },
        )

    def _challenge(self, *, error: bool, state: str | None) -> httpx.Response:
        return json_response(
            {
                "hhcaptcha": {
                    "isBot": True,
                    "captchaState": state,
                    "captchaError": error,
                    "captchaKey": f"server-key-{self.submissions}",
                },
                "loginError": None,
                "userType": "anonymous",
            }
        )


@dataclass(frozen=True, slots=True)
class AuthorizeScript:
    """The /oauth/authorize redirect behavior, as data."""

    origin: str = "hhandroid://oauthresponse"
    echo_state: bool = True
    include_state: bool = True

    def response(self, request: httpx.Request) -> httpx.Response:
        state = next((value for key, value in request.url.params.multi_items() if key == "state"), "")
        returned_state = state if self.echo_state else "mismatched-state"
        state_query = f"&state={returned_state}" if self.include_state else ""
        return httpx.Response(302, headers={"location": f"{self.origin}?code=authorization-code{state_query}"})


def _token_handler(
    *,
    malformed: bool = False,
    recaptcha: bool = False,
) -> TokenResponder:
    """The /oauth/token responder: refresh rotation + the scenario's fresh body."""

    def respond(form: dict[str, list[str]]) -> httpx.Response:
        if recaptcha:
            return httpx.Response(403, content=b'<textarea name="g-recaptcha-response"></textarea>')
        if form.get("grant_type") == ["refresh_token"]:
            return _token_body(b'{"access_token":"USER-refreshed","refresh_token":"refresh-rotated",')
        if malformed:
            return _token_body(b'{"access_token":"not-a-user-token","expires_in":0}')
        return _token_body(b'{"access_token":"USER-new","refresh_token":"refresh-new",')

    return respond


def _token_body(head: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=head + b'"expires_in":1209599,"token_type":"bearer"}', headers={"content-type": "application/json"}
    )


def _forbidden_healthcheck() -> httpx.Response:
    return httpx.Response(403, content=b'{"errors":[{"type":"forbidden"}]}')


def _non_applicant_healthcheck() -> httpx.Response:
    return json_response({"id": "employer-1", "auth_type": "employer", "is_applicant": False})


# --- Frozen carriers ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthScenario:
    """Frozen setup: the client factory, the transport recorder, and the state path."""

    transport: AuthTransport
    make_client: Callable[[], HHClient]
    state_path: Path
    solver: RecordingSolver


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """Frozen outcome: the authorization error(s) and the request history produced."""

    errors: tuple[ClientError | None, ...]
    requests: tuple[httpx.Request, ...]


async def _no_delay() -> None:
    return None


def _scenario(
    tmp_path: Path,
    *,
    seeded: bool = False,
    expires_at: float = DEFAULT_EXPIRES_AT,
    healthchecks: ScriptedResponses | None = None,
    login_pages: ScriptedResponses | None = None,
    login_post: LoginPostResponder | None = None,
    authorize: AuthorizeScript | None = None,
    token: TokenResponder | None = None,
    browser_driver: BrowserDriver | None = None,
) -> AuthScenario:
    transport = AuthTransport(
        healthchecks=healthchecks if healthchecks is not None else ScriptedResponses(applicant_healthcheck()),
        login_pages=login_pages if login_pages is not None else ScriptedResponses(_login_page()),
        login_post=login_post if login_post is not None else _plain_login(),
        authorize=authorize if authorize is not None else AuthorizeScript(),
        token=token if token is not None else _token_handler(),
    )
    data_dir = tmp_path / "data"
    state_path = (
        seed_auth_state(data_dir, _PROFILE_ID, access_token="USER-restored", expires_at=expires_at)
        if seeded
        else data_dir.joinpath(*_TOKEN_PATH_PARTS)
    )
    solver = RecordingSolver(Ok("два слова"))

    def make_client() -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(resume_id="resume-1", searches=(HHSearchEntry(query="python"),)),
            solver=solver,
            apply_delay=_no_delay,
            browser_driver=browser_driver,
            login=_LOGIN,
            password=_PASSWORD,
        )

    return AuthScenario(transport=transport, make_client=make_client, state_path=state_path, solver=solver)


# --- Givens (setup variants are distinct steps, never a kind enum) -----------


@given("a valid persisted HH token", target_fixture="auth_scenario")
def valid_state_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, seeded=True)


class FailingEngineDriver(FakeBrowserDriver):
    """Scripted driver whose startup preflight always fails."""

    def __init__(self) -> None:
        super().__init__([])

    async def ensure_engine(self) -> Result[None, str]:
        return Err("install timed out after 10s (downloaded 55%)")


@given("a browser engine whose startup preflight fails", target_fixture="auth_scenario")
def failing_engine_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, seeded=True, browser_driver=FailingEngineDriver())


@given("no persisted HH authentication state", target_fixture="auth_scenario")
def missing_state_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path)


@given("no persisted state and a login cookie bootstrap redirect", target_fixture="auth_scenario")
def login_bootstrap_redirect_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_pages=ScriptedResponses(_login_bootstrap_redirect(), _login_page()))


@given("no persisted state and one credential-login connection failure", target_fixture="auth_scenario")
def login_connect_error_once_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=_failing_login(fail_once=True))


@given("no persisted state and persistent credential-login connection failures", target_fixture="auth_scenario")
def login_connect_error_persistent_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=_failing_login(fail_once=False))


@given("an expired persisted HH token", target_fixture="auth_scenario")
def expired_state_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, seeded=True, expires_at=1.0)


@given("a rejected persisted HH token", target_fixture="auth_scenario")
def rejected_state_step(tmp_path: Path) -> AuthScenario:
    return _scenario(
        tmp_path,
        seeded=True,
        healthchecks=ScriptedResponses(_forbidden_healthcheck(), applicant_healthcheck()),
    )


@given("no persisted state and an OAuth state mismatch", target_fixture="auth_scenario")
def state_mismatch_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, authorize=AuthorizeScript(echo_state=False))


@given("no persisted state and an OAuth response without state", target_fixture="auth_scenario")
def state_omitted_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, authorize=AuthorizeScript(include_state=False))


@given("no persisted state and an invalid OAuth redirect", target_fixture="auth_scenario")
def invalid_redirect_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, authorize=AuthorizeScript(origin="https://example.invalid/oauth"))


@given("no persisted state and a malformed OAuth token response", target_fixture="auth_scenario")
def malformed_token_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, token=_token_handler(malformed=True))


@given("no persisted state and reCAPTCHA at the OAuth token boundary", target_fixture="auth_scenario")
def token_recaptcha_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, token=_token_handler(recaptcha=True))


@given("no persisted state and an embedded login CAPTCHA", target_fixture="auth_scenario")
def embedded_captcha_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin())


@given("an embedded login CAPTCHA rejecting its first answer", target_fixture="auth_scenario")
def embedded_captcha_wrong_once_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin(reject_first=True))


@given("an embedded login CAPTCHA rejecting every answer", target_fixture="auth_scenario")
def embedded_captcha_exhausted_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin(exhaust=True))


@given("an embedded login CAPTCHA followed by a credential mismatch", target_fixture="auth_scenario")
def embedded_captcha_mismatch_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin(mismatch=True))


@given("an embedded login CAPTCHA without a state", target_fixture="auth_scenario")
def embedded_captcha_missing_state_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin(state=None))


@given("an embedded login CAPTCHA changing state after an answer", target_fixture="auth_scenario")
def embedded_captcha_state_drift_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=EmbeddedLogin(drift_state=True))


@given("no persisted state and dormant reCAPTCHA metadata", target_fixture="auth_scenario")
def dormant_recaptcha_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, login_post=_plain_login(_DORMANT_RECAPTCHA_BODY))


@given("a valid token for a non-applicant account", target_fixture="auth_scenario")
def non_applicant_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, seeded=True, healthchecks=ScriptedResponses(_non_applicant_healthcheck()))


@given("no persisted state and a rejected post-login healthcheck", target_fixture="auth_scenario")
def post_auth_rejected_step(tmp_path: Path) -> AuthScenario:
    return _scenario(tmp_path, healthchecks=ScriptedResponses(_forbidden_healthcheck()))


# --- Whens (the action returns its frozen outcome) ---------------------------


async def _authorize_and_close(client: HHClient) -> ClientError | None:
    result = await client.authorize()
    await client.aclose()
    return result.unwrap_err() if result.is_err else None


async def _invoke_actions(client: HHClient) -> tuple[ClientError | None, ClientError | None, ClientError | None]:
    try:
        search = await client.search_vacancies(0)
        resumes = await client.get_resumes()
        apply = await client.apply_to_vacancy(
            resume_id="resume-1",
            vacancy_id=ServiceVacancyId("vacancy-1"),
        )
    finally:
        await client.aclose()
    return (
        search.unwrap_err() if search.is_err else None,
        resumes.unwrap_err() if resumes.is_err else None,
        apply.unwrap_err() if apply.is_err else None,
    )


@when("the HH client authorizes", target_fixture="auth_outcome")
def authorize_step(auth_scenario: AuthScenario) -> AuthOutcome:
    error = async_run(_authorize_and_close(auth_scenario.make_client()))
    return AuthOutcome(errors=(error,), requests=tuple(auth_scenario.transport.requests))


@when("the HH client authorizes twice with a restart", target_fixture="auth_outcome")
def authorize_twice_step(auth_scenario: AuthScenario) -> AuthOutcome:
    first = async_run(_authorize_and_close(auth_scenario.make_client()))
    second = async_run(_authorize_and_close(auth_scenario.make_client()))
    return AuthOutcome(errors=(first, second), requests=tuple(auth_scenario.transport.requests))


@when("every non-auth HH action is invoked", target_fixture="auth_outcome")
def every_action_step(auth_scenario: AuthScenario) -> AuthOutcome:
    errors = async_run(_invoke_actions(auth_scenario.make_client()))
    return AuthOutcome(errors=errors, requests=tuple(auth_scenario.transport.requests))


# --- Thens (assert only) ------------------------------------------------------


def _paths(outcome: AuthOutcome) -> list[str]:
    return [request.url.path for request in outcome.requests]


def _login_posts(outcome: AuthOutcome) -> list[httpx.Request]:
    return [
        request for request in outcome.requests if request.url.path == "/account/login" and request.method == "POST"
    ]


def _captcha_answer_posts(outcome: AuthOutcome) -> list[httpx.Request]:
    return [request for request in _login_posts(outcome) if b'name="captchaText"' in request.content]


def _captcha_key_posts(outcome: AuthOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.url.path == "/captcha" and request.method == "POST"]


@then("authorization succeeds")
def authorization_succeeds_step(auth_outcome: AuthOutcome) -> None:
    assert auth_outcome.errors == (None,)


@then("both authorizations succeed")
def both_authorizations_succeed_step(auth_outcome: AuthOutcome) -> None:
    assert auth_outcome.errors == (None, None)


@then("authorization fails with a protocol error")
def protocol_error_step(auth_outcome: AuthOutcome) -> None:
    assert len(auth_outcome.errors) == 1
    assert isinstance(auth_outcome.errors[0], ProtocolError)


@then("authorization fails with a CAPTCHA error")
def captcha_error_step(auth_outcome: AuthOutcome) -> None:
    assert len(auth_outcome.errors) == 1
    assert isinstance(auth_outcome.errors[0], CaptchaSolvingError)


@then("authorization fails with a browser engine error")
def browser_engine_error_step(auth_outcome: AuthOutcome) -> None:
    assert len(auth_outcome.errors) == 1
    error = auth_outcome.errors[0]
    assert isinstance(error, ConfigurationError)
    assert "patchright install chromium" in error.message
    assert "install timed out" in error.message


@then("HH received no requests at all")
def no_requests_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == []


@then("authorization fails with an authentication error")
def authentication_error_step(auth_outcome: AuthOutcome) -> None:
    assert len(auth_outcome.errors) == 1
    assert isinstance(auth_outcome.errors[0], AuthError)


@then("authorization fails with a credential-login transport error")
def credential_login_transport_error_step(auth_outcome: AuthOutcome) -> None:
    assert len(auth_outcome.errors) == 1
    error = auth_outcome.errors[0]
    assert isinstance(error, TransportError)
    assert error.message == "HH transport unavailable during POST /account/login: ConnectError"


@then("HH received only an applicant healthcheck")
def only_healthcheck_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == ["/me"]


@then("HH received the complete credential OAuth flow")
def complete_oauth_flow_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/account/login",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
    ]


@then("HH received one login bootstrap replay before the credential OAuth flow")
def login_bootstrap_replay_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/account/login",
        "/account/login",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
    ]


@then("HH received two credential-login connection attempts")
def two_login_connection_attempts_step(auth_outcome: AuthOutcome) -> None:
    assert len(_login_posts(auth_outcome)) == 2


@then("HH received three credential-login connection attempts")
def three_login_connection_attempts_step(auth_outcome: AuthOutcome) -> None:
    assert len(_login_posts(auth_outcome)) == 3


@then("HH received one refresh followed by an applicant healthcheck")
def refresh_flow_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == ["/oauth/token", "/me"]
    refresh = parse_qs(auth_outcome.requests[0].content.decode())
    assert refresh.get("grant_type") == ["refresh_token"]


@then("HH received no refresh before the replacement credential OAuth flow")
def rejected_token_flow_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/me",
        "/account/login",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
    ]


@then("HH received only one credential OAuth flow")
def one_oauth_flow_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/account/login",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
    ]


@then("verified authentication state was persisted securely")
def persisted_securely_step(auth_scenario: AuthScenario) -> None:
    assert auth_scenario.state_path.is_file()
    if os.name == "posix":
        # POSIX-only semantics: Windows chmod carries no owner-rw bits, so the
        # product's best-effort mode hardening is unobservable there.
        assert stat.S_IMODE(auth_scenario.state_path.stat().st_mode) == 0o600
    persisted = PersistedAuthState.model_validate_json(auth_scenario.state_path.read_text(encoding="utf-8"))
    assert persisted.access_token == "USER-new"


@then("the rotated token state was persisted")
def rotated_state_step(auth_scenario: AuthScenario) -> None:
    persisted = PersistedAuthState.model_validate_json(auth_scenario.state_path.read_text(encoding="utf-8"))
    assert persisted.access_token == "USER-refreshed"
    assert persisted.refresh_token == "refresh-rotated"


@then("no authentication state was persisted")
def no_state_step(auth_scenario: AuthScenario) -> None:
    assert not auth_scenario.state_path.exists()


@then("HH received no standalone CAPTCHA submission")
def no_captcha_submit_step(auth_outcome: AuthOutcome) -> None:
    assert not any(
        request.url.path == "/account/captcha" and request.method == "POST" for request in auth_outcome.requests
    )


@then("HH received the researched embedded CAPTCHA login flow")
def embedded_captcha_flow_step(auth_scenario: AuthScenario, auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/account/login",
        "/account/login",
        "/captcha",
        "/captcha/picture",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
    ]
    submit = auth_outcome.requests[4]
    assert submit.url.params.get("backurl") == "/"
    assert submit.url.params.get("oauth") == "true"
    assert submit.url.params.get("response_type") == "code"
    assert submit.headers.get("x-xsrftoken") == "xsrf-value"
    assert submit.headers.get("x-requested-with") == "XMLHttpRequest"
    assert submit.headers.get("x-hhtmsource") == "account_login"
    key_request = auth_outcome.requests[2]
    assert key_request.headers.get("x-xsrftoken") == "xsrf-value"
    assert key_request.headers.get("x-requested-with") == "XMLHttpRequest"
    assert submit.content.count(b'name="password"') == 2
    assert b"\r\n\r\nEMPLOYER\r\n" in submit.content
    assert b"\r\n\r\nyes\r\n" in submit.content
    assert "два слова".encode() in submit.content
    assert b"\r\n\r\nlogin-image-key-1\r\n" in submit.content
    assert b"\r\n\r\nfalse\r\n" in submit.content
    assert _LOGIN.encode() in submit.content
    assert _CAPTCHA_STATE.encode() in submit.content
    assert auth_scenario.solver.images == [PNG_IMAGE]


@then("HH received two fresh embedded CAPTCHA image keys")
def two_embedded_keys_step(auth_outcome: AuthOutcome) -> None:
    picture_keys = [
        request.url.params.get("key") for request in auth_outcome.requests if request.url.path == "/captcha/picture"
    ]
    assert picture_keys == ["login-image-key-1", "login-image-key-2"]
    assert len(_captcha_answer_posts(auth_outcome)) == 2


@then("HH received exactly four embedded CAPTCHA answers")
def four_embedded_answers_step(auth_outcome: AuthOutcome) -> None:
    assert len(_captcha_answer_posts(auth_outcome)) == 4
    assert len(_captcha_key_posts(auth_outcome)) == 4


@then("HH received exactly one embedded CAPTCHA answer")
def one_embedded_answer_step(auth_outcome: AuthOutcome) -> None:
    assert len(_captcha_answer_posts(auth_outcome)) == 1
    assert len(_captcha_key_posts(auth_outcome)) == 1


@then("HH requested no embedded CAPTCHA image")
def no_embedded_image_step(auth_scenario: AuthScenario, auth_outcome: AuthOutcome) -> None:
    assert len(_captcha_key_posts(auth_outcome)) == 0
    assert auth_scenario.solver.images == []


@then("the second client used the persisted token healthcheck")
def second_client_restore_step(auth_outcome: AuthOutcome) -> None:
    assert _paths(auth_outcome) == [
        "/account/login",
        "/account/login",
        "/oauth/authorize",
        "/oauth/token",
        "/me",
        "/me",
    ]


@then("each action performed an applicant healthcheck")
def every_action_healthchecked_step(auth_outcome: AuthOutcome) -> None:
    # search (healthcheck + catalog), resumes (healthcheck + listing walk),
    # apply (healthcheck + resume validation + detail preflight + submission).
    assert _paths(auth_outcome) == [
        "/me",
        "/vacancies",
        "/me",
        "/resumes/mine",
        "/me",
        "/resumes/mine",
        "/vacancies/vacancy-1",
        "/negotiations",
    ]


@then("every non-auth action succeeds")
def actions_all_succeed_step(auth_outcome: AuthOutcome) -> None:
    assert auth_outcome.errors == (None, None, None)


# --- Windows platform guard (token persistence W1) --------------------------

_WIN_SIM_PROFILE_ID = "win-sim-profile"


@dataclass(frozen=True, slots=True)
class WinTokenStoreSetup:
    """Frozen setup carrier: the store under a simulated win32 + its state path."""

    store: TokenStore
    state_path: Path


@dataclass(frozen=True, slots=True)
class WinSaveOutcome:
    """Frozen outcome carrier: the save result plus the reloaded snapshot."""

    save_ok: bool
    reloaded: PersistedAuthState | None


async def _forbidden_captcha(image: bytes) -> Result[str, str]:
    """The token store never solves captchas; fail loudly if it tries."""
    del image
    raise AssertionError("TokenStore must not invoke the captcha handler")


@given("Windows simulated for the HH token store", target_fixture="win_token_store")
def win_token_store_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WinTokenStoreSetup:
    """Simulate win32: os.fchmod does not exist there and must never be called.

    The fchmod double is registered with ``raising=False`` so the same scenario
    runs as a tripwire on POSIX (where os.fchmod exists) and on real Windows
    (where the attribute is absent by design — the branch must skip it).
    """

    def _forbidden_fchmod(descriptor: int, mode: int) -> None:
        raise AssertionError("os.fchmod must not be called on win32")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "fchmod", _forbidden_fchmod, raising=False)
    data_dir = tmp_path / "data"
    store = TokenStore(
        ClientDeps(
            service="hh",
            profile_id=_WIN_SIM_PROFILE_ID,
            data_dir=data_dir,
            credentials=ClientCredentials(login=_LOGIN, password=_PASSWORD),
            auth_interaction=StubAuthInteraction(),
            captcha_handler=_forbidden_captcha,
        )
    )
    state_path = data_dir / "hh" / _WIN_SIM_PROFILE_ID / "auth-state.json"
    return WinTokenStoreSetup(store=store, state_path=state_path)


@when("a token state snapshot is saved", target_fixture="win_save_outcome")
def win_save_outcome_step(win_token_store: WinTokenStoreSetup) -> WinSaveOutcome:
    """Save under the simulated platform, then reload the persisted snapshot."""
    state = PersistedAuthState(
        access_token="USER-new",
        refresh_token="refresh-new",
        expires_at=DEFAULT_EXPIRES_AT,
        cookies=(),
    )
    # Deliberate asyncio.run, not async_run_result (§6): Err here is data
    # (carried in the outcome carrier), not a step failure.
    result = asyncio.run(win_token_store.store.save(state))
    reloaded = asyncio.run(win_token_store.store.load()) if result.is_ok else None
    return WinSaveOutcome(save_ok=result.is_ok, reloaded=reloaded)


@then("the save succeeds and the snapshot roundtrips without fchmod")
def win_save_roundtrip_step(win_token_store: WinTokenStoreSetup, win_save_outcome: WinSaveOutcome) -> None:
    assert win_save_outcome.save_ok
    persisted = PersistedAuthState.model_validate_json(win_token_store.state_path.read_text(encoding="utf-8"))
    assert persisted.access_token == "USER-new"
    assert win_save_outcome.reloaded is not None
    assert win_save_outcome.reloaded.refresh_token == "refresh-new"
