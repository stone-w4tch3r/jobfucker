"""BDD acceptance for the Habr transport boundary (test/AGENTS.md §3a, §3b).

The routed ``httpx.MockTransport`` records every request; the ``When`` returns a
frozen outcome carrying the response status and the request history, and
``Then`` steps assert on the recorded sequencing and headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from jobfucker.clients.base import ClientError, ProtocolError
from jobfucker.clients.habr.models import HabrErrorEnvelope, HabrLoginResponse, HabrStructuredError
from jobfucker.clients.habr.transport import (
    HABR_USER_AGENT,
    HabrTransport,
    decode_json,
    is_analytics_cookie,
    is_habr_cookie_domain,
    transport_config,
)
from jobfucker.clients.shared.cookies import PersistedCookie
from jobfucker.clients.shared.transport import TransportConfig
from jobfucker.testing.step_runner import async_run_result
from test.clients.habr.helpers import (
    RoutedTransport,
    csrf_page_html,
    habr_transport,
    html_response,
)

scenarios("bdd/transport.feature")

_CAREER: Final = "career.habr.com"
_ACCOUNT: Final = "account.habr.com"
_AUTHORIZE_PATH: Final = "/oauth/authorize/"
# The login chain's OAuth hop carries its query inside the URL (no explicit
# params), which httpx strips when handed an empty params container.
_AUTHORIZE_URL: Final = f"https://{_ACCOUNT}{_AUTHORIZE_PATH}?response_type=code&client_id=career-test"
_MUTATION_PATH: Final = "/api/frontend/vacancies/1/responses"
_CSRF_PATH: Final = "/vacancies"


@dataclass(frozen=True, slots=True)
class TransportScenario:
    """Frozen setup: the routed mock and the transport under test."""

    routed: RoutedTransport
    transport: HabrTransport


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    """Frozen outcome: the mutation status plus the recorded request history."""

    status: int
    requests: tuple[httpx.Request, ...]


@dataclass(frozen=True, slots=True)
class AbsoluteGetScenario:
    """Frozen setup: a routed mock and a transport for the login-chain GETs."""

    routed: RoutedTransport
    transport: HabrTransport


@dataclass(frozen=True, slots=True)
class AbsoluteGetOutcome:
    """Frozen outcome: the recorded request history for one absolute GET."""

    requests: tuple[httpx.Request, ...]


def _mutation_scenario(
    *,
    csrf_tokens: tuple[str, ...],
    statuses: tuple[int, ...],
    content_first: bool = False,
) -> TransportScenario:
    routed = RoutedTransport()
    routed.on(
        "GET",
        _CAREER,
        _CSRF_PATH,
        *(html_response(csrf_page_html(token, content_first=content_first)) for token in csrf_tokens),
    )
    routed.on("POST", _CAREER, _MUTATION_PATH, *(httpx.Response(status) for status in statuses))
    return TransportScenario(routed=routed, transport=habr_transport(routed))


@given(parsers.parse('a Habr transport whose CSRF page issues "{token}"'), target_fixture="transport_scenario")
def single_csrf_step(token: str) -> TransportScenario:
    return _mutation_scenario(csrf_tokens=(token,), statuses=(200,))


@given(
    parsers.parse('a Habr transport whose CSRF page issues "{token}" with the content attribute first'),
    target_fixture="transport_scenario",
)
def single_csrf_content_first_step(token: str) -> TransportScenario:
    return _mutation_scenario(csrf_tokens=(token,), statuses=(200,), content_first=True)


@given(
    parsers.parse(
        'a Habr transport whose CSRF pages issue "{first}" then "{second}" '
        "and whose mutation answers {first_status:d} then {second_status:d}"
    ),
    target_fixture="transport_scenario",
)
def two_csrf_step(first: str, second: str, first_status: int, second_status: int) -> TransportScenario:
    return _mutation_scenario(csrf_tokens=(first, second), statuses=(first_status, second_status))


@given(
    parsers.parse(
        'a Habr transport whose CSRF pages issue "{first}" then "{second}" and whose mutation always answers {status:d}'
    ),
    target_fixture="transport_scenario",
)
def two_csrf_always_step(first: str, second: str, status: int) -> TransportScenario:
    return _mutation_scenario(csrf_tokens=(first, second), statuses=(status,))


@given(
    "a Habr transport whose cookie jar holds a career session and an analytics cookie",
    target_fixture="cookie_transport",
)
def cookie_transport_step() -> HabrTransport:
    transport = habr_transport(RoutedTransport())
    transport.restore_cookies(
        (
            PersistedCookie(name="_career_session", value="session-value", domain="career.habr.com"),
            PersistedCookie(name="_ga", value="tracker-value", domain=".habr.com"),
        )
    )
    return transport


@given(parsers.parse("the raw Habr error body {body}"), target_fixture="raw_body")
def raw_body_step(body: str) -> str:
    return body


@given("a Habr transport that records an absolute GET", target_fixture="absolute_get_scenario")
def absolute_get_step() -> AbsoluteGetScenario:
    routed = RoutedTransport()
    routed.on(
        "GET",
        _ACCOUNT,
        _AUTHORIZE_PATH,
        httpx.Response(302, headers={"location": f"https://{_ACCOUNT}/ru/ident/state"}),
    )
    return AbsoluteGetScenario(routed=routed, transport=habr_transport(routed))


@given("the Habr transport policy", target_fixture="policy")
def policy_step() -> TransportConfig:
    return transport_config()


@when("a mutation is submitted", target_fixture="mutation_outcome")
def submit_mutation_step(transport_scenario: TransportScenario) -> MutationOutcome:
    response = async_run_result(transport_scenario.transport.post_multipart(_MUTATION_PATH))
    return MutationOutcome(status=response.status_code, requests=tuple(transport_scenario.routed.requests))


@when(
    "the transport GETs the OAuth authorize URL with its embedded query",
    target_fixture="absolute_get_outcome",
)
def get_embedded_query_step(absolute_get_scenario: AbsoluteGetScenario) -> AbsoluteGetOutcome:
    async_run_result(absolute_get_scenario.transport.get_absolute(_AUTHORIZE_URL))
    return AbsoluteGetOutcome(requests=tuple(absolute_get_scenario.routed.requests))


@when("the transport GETs the authorize URL with explicit params", target_fixture="absolute_get_outcome")
def get_explicit_params_step(absolute_get_scenario: AbsoluteGetScenario) -> AbsoluteGetOutcome:
    async_run_result(
        absolute_get_scenario.transport.get_absolute(
            f"https://{_ACCOUNT}{_AUTHORIZE_PATH}",
            params=(("state", "bslogin"), ("action", "login")),
        )
    )
    return AbsoluteGetOutcome(requests=tuple(absolute_get_scenario.routed.requests))


@when("the body is decoded as an error envelope", target_fixture="envelope")
def decode_envelope_step(raw_body: str) -> HabrErrorEnvelope:
    result = decode_json(httpx.Response(404, content=raw_body.encode()), HabrErrorEnvelope, operation="error")
    if result.is_err:
        pytest.fail("expected the plain error envelope to decode")
    return result.unwrap()


@when("the body is decoded as a structured error", target_fixture="structured_error")
def decode_structured_step(raw_body: str) -> HabrStructuredError:
    result = decode_json(httpx.Response(422, content=raw_body.encode()), HabrStructuredError, operation="error")
    if result.is_err:
        pytest.fail("expected the structured error envelope to decode")
    return result.unwrap()


@when("the body is decoded as a login response", target_fixture="decode_error")
def decode_login_step(raw_body: str) -> ClientError | None:
    result = decode_json(httpx.Response(200, content=raw_body.encode()), HabrLoginResponse, operation="login")
    return result.unwrap_err() if result.is_err else None


@when("the session cookies are snapshotted", target_fixture="cookie_names")
def snapshot_cookies_step(cookie_transport: HabrTransport) -> tuple[str, ...]:
    return tuple(cookie.name for cookie in cookie_transport.snapshot_cookies())


def _csrf_gets(outcome: MutationOutcome) -> list[httpx.Request]:
    return [
        request
        for request in outcome.requests
        if request.method == "GET" and request.url.host == _CAREER and request.url.path == _CSRF_PATH
    ]


def _mutations(outcome: MutationOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.method == "POST"]


@then("the transport sent a CSRF-scraping GET first")
def assert_csrf_get_first_step(mutation_outcome: MutationOutcome) -> None:
    first = mutation_outcome.requests[0]
    assert first.method == "GET"
    assert first.url.host == _CAREER
    assert first.url.path == _CSRF_PATH


@then(parsers.parse('the mutation carried the CSRF token "{token}", the XHR header, and a JSON accept'))
def assert_mutation_headers_step(mutation_outcome: MutationOutcome, token: str) -> None:
    mutation = _mutations(mutation_outcome)[-1]
    assert mutation.headers["x-csrf-token"] == token
    assert mutation.headers["x-requested-with"] == "XMLHttpRequest"
    assert mutation.headers["accept"].startswith("application/json")


@then(parsers.parse("the mutation result status is {status:d}"))
def assert_mutation_status_step(mutation_outcome: MutationOutcome, status: int) -> None:
    assert mutation_outcome.status == status


@then("the transport sent two CSRF-scraping GETs")
def assert_two_csrf_gets_step(mutation_outcome: MutationOutcome) -> None:
    assert len(_csrf_gets(mutation_outcome)) == 2


@then(parsers.parse('the transport sent two mutations, the second carrying the CSRF token "{token}"'))
def assert_replay_token_step(mutation_outcome: MutationOutcome, token: str) -> None:
    mutations = _mutations(mutation_outcome)
    assert len(mutations) == 2
    assert mutations[1].headers["x-csrf-token"] == token


@then("the transport sent exactly two mutations and two CSRF-scraping GETs")
def assert_bounded_replay_step(mutation_outcome: MutationOutcome) -> None:
    assert len(_mutations(mutation_outcome)) == 2
    assert len(_csrf_gets(mutation_outcome)) == 2


@then(parsers.parse('the envelope error message is "{message}"'))
def assert_envelope_message_step(envelope: HabrErrorEnvelope, message: str) -> None:
    assert envelope.message == message


@then(parsers.parse('the structured error message is "{message}"'))
def assert_structured_message_step(structured_error: HabrStructuredError, message: str) -> None:
    assert structured_error.message == message


@then("decoding fails with a protocol error")
def assert_protocol_error_step(decode_error: ClientError | None) -> None:
    assert isinstance(decode_error, ProtocolError)


@then("only the career session cookie is persisted")
def assert_cookie_snapshot_step(cookie_names: tuple[str, ...]) -> None:
    assert cookie_names == ("_career_session",)


@then("the read pacing is at least 0.3 seconds")
def assert_pacing_step(policy: TransportConfig) -> None:
    assert policy.min_request_interval_s >= 0.3


@then("redirects are not followed")
def assert_no_redirects_step(policy: TransportConfig) -> None:
    assert policy.follow_redirects is False


@then("the user agent is a desktop Chrome, never HeadlessChrome")
def assert_user_agent_step(policy: TransportConfig) -> None:
    assert policy.user_agent == HABR_USER_AGENT
    assert "HeadlessChrome" not in HABR_USER_AGENT
    assert "Chrome/" in HABR_USER_AGENT


@then("only Habr-family cookie domains are allowed")
def assert_cookie_domains_step() -> None:
    assert is_habr_cookie_domain("career.habr.com")
    assert is_habr_cookie_domain(".habr.com")
    assert not is_habr_cookie_domain("example.com")
    assert not is_habr_cookie_domain("habr.com.example.com")


@then("analytics cookie names are recognised")
def assert_analytics_step() -> None:
    assert is_analytics_cookie("_ga")
    assert is_analytics_cookie("_gid")
    assert not is_analytics_cookie("_career_session")


@then("the retryable statuses are transient 5xx only")
def assert_retryable_statuses_step(policy: TransportConfig) -> None:
    # 429 is a 4xx: listing it as "retryable" would be inert (the shared GET
    # retry only fires on 5xx). Keep the set to the transient server errors.
    assert policy.retryable_statuses == frozenset({502, 503, 504})
    assert 429 not in policy.retryable_statuses


@then("the recorded request kept the embedded response_type and client_id")
def assert_embedded_query_step(absolute_get_outcome: AbsoluteGetOutcome) -> None:
    params = absolute_get_outcome.requests[0].url.params
    assert params["response_type"] == "code"
    assert params["client_id"] == "career-test"


@then("the recorded request carried the explicit state and action params")
def assert_explicit_params_step(absolute_get_outcome: AbsoluteGetOutcome) -> None:
    params = absolute_get_outcome.requests[0].url.params
    assert params["state"] == "bslogin"
    assert params["action"] == "login"
