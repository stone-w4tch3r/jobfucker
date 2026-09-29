"""BDD acceptance for the Habr application flow (test/AGENTS.md §3b).

The routed mock exposes the identity probe, the vacancy detail page, the CSRF
page, and the multipart responses POST. Every scenario injects a recording no-op
pacing delay so no wall-clock wait occurs while the delay's invocation count is
still asserted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import (
    ApplyFailed,
    ApplyResult,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    ClientError,
    ConfigurationError,
    LimitExceededError,
    NotFoundError,
    ProtocolError,
    ServiceVacancyId,
    TransportError,
    UnknownApplyOutcomeError,
)
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import HabrSearchEntry, HabrServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver
from test.clients.habr.helpers import (
    JsonBlob,
    RecordingApplyDelay,
    RecordingSolver,
    ScriptedResponses,
    authorized_identity,
    csrf_page_html,
    habr_deps,
    html_response,
    json_response,
    profile_id_for,
    ssr_detail_page,
)

scenarios("bdd/applications.feature")

_IDENTITY_PATH: Final = "/api/frontend_v1/users/me"
_CSRF_PATH: Final = "/vacancies"
_DETAIL_PREFIX: Final = "/vacancies/"
_POST_PREFIX: Final = "/api/frontend/vacancies/"
_VACANCY_ID: Final = "1"
_LETTER: Final = "Здравствуйте, я хочу откликнуться"

_IDENTITY: Final = "identity"
_DETAIL: Final = "detail"
_CSRF: Final = "csrf"
_POST: Final = "post"


class HabrApplyRouter:
    """Dumb router over the identity, detail, CSRF, and responses endpoints."""

    def __init__(
        self,
        *,
        details: ScriptedResponses,
        submissions: ScriptedResponses,
        csrf_pages: ScriptedResponses,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._identity = ScriptedResponses(authorized_identity())
        self._details = details
        self._submissions = submissions
        self._csrf_pages = csrf_pages

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == _IDENTITY_PATH:
            return self._identity.next()
        if path == _CSRF_PATH:
            return self._csrf_pages.next()
        if path.startswith(_POST_PREFIX) and request.method == "POST":
            return self._submissions.next()
        if path.startswith(_DETAIL_PREFIX):
            return self._details.next()
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


@dataclass(frozen=True, slots=True)
class ApplyScenario:
    """Frozen setup: the routed mock, a client factory, and the recording delay."""

    router: HabrApplyRouter
    make_client: Callable[[], HabrClient]
    delay: RecordingApplyDelay
    delay_snapshots: list[tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """Frozen outcome: the apply result, request history, and delay invocations."""

    result: Result[ApplyResult, ClientError]
    requests: tuple[httpx.Request, ...]
    delay_calls: int
    delay_time_paths: tuple[tuple[str, ...], ...]


# --- Responders ---------------------------------------------------------------


def _detail(kind: str = "direct") -> httpx.Response:
    return html_response(ssr_detail_page(int(_VACANCY_ID), response_kind=kind))


def _csrf_pages(*tokens: str) -> ScriptedResponses:
    return ScriptedResponses(*(html_response(csrf_page_html(token)) for token in tokens))


def _submission(status: int, payload: JsonBlob) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _scenario(
    tmp_path: Path,
    *,
    details: ScriptedResponses,
    submissions: ScriptedResponses,
    csrf_pages: ScriptedResponses | None = None,
) -> ApplyScenario:
    router = HabrApplyRouter(
        details=details,
        submissions=submissions,
        csrf_pages=csrf_pages if csrf_pages is not None else _csrf_pages("csrf-1"),
    )
    data_dir = tmp_path / "data"
    delay_snapshots: list[tuple[str, ...]] = []
    delay = RecordingApplyDelay(lambda: delay_snapshots.append(tuple(request.url.path for request in router.requests)))
    entry = HabrSearchEntry(query="python")

    def make_client() -> HabrClient:
        return HabrClient(
            habr_deps(data_dir, profile_id_for(), RecordingSolver()),
            HabrServiceConfig(searches=(entry,)),
            http_transport=httpx.MockTransport(router),
            browser_driver=FakeHabrBrowserDriver(),
            apply_delay=delay,
        )

    return ApplyScenario(router=router, make_client=make_client, delay=delay, delay_snapshots=delay_snapshots)


# --- Given --------------------------------------------------------------------


@given("a Habr client whose application succeeds", target_fixture="apply_scenario")
def success_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(json_response({"response": {"id": 7}})),
    )


@given("a Habr client whose CSRF token cannot be scraped", target_fixture="apply_scenario")
def csrf_unavailable_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(json_response({"response": {"id": 7}})),
        csrf_pages=ScriptedResponses(html_response("<html><body>no token here</body></html>")),
    )


@given("a Habr client whose application succeeds with an empty response", target_fixture="apply_scenario")
def empty_success_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(json_response({"response": {}})),
    )


@given("a Habr client and an already-applied vacancy", target_fixture="apply_scenario")
def already_applied_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail(kind="applied")),
        submissions=ScriptedResponses(json_response({"response": {"id": 7}})),
    )


@given("a Habr client and an archived vacancy", target_fixture="apply_scenario")
def archived_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(html_response(ssr_detail_page(int(_VACANCY_ID), archived=True))),
        submissions=ScriptedResponses(json_response({"response": {"id": 7}})),
    )


@given("a Habr client and a hidden vacancy", target_fixture="apply_scenario")
def hidden_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(html_response(ssr_detail_page(int(_VACANCY_ID), hidden=True))),
        submissions=ScriptedResponses(json_response({"response": {"id": 7}})),
    )


@given("a Habr client whose duplicate submission is rejected", target_fixture="apply_scenario")
def duplicate_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(401, {"error": {"message": "Вы уже откликнулись на эту вакансию"}})),
    )


@given("a Habr client whose submission is rejected as unauthorized", target_fixture="apply_scenario")
def unauthorized_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(401, {"error": "Войдите, прежде чем продолжить"})),
    )


@given("a Habr client whose submission is throttled", target_fixture="apply_scenario")
def throttled_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(
            _submission(400, {"message": "Отклики можно отправлять не чаще, чем раз в 10 секунд"})
        ),
    )


@given("a Habr client whose submission hits the monthly cap", target_fixture="apply_scenario")
def monthly_cap_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(400, {"message": "Нельзя отправить более 150 откликов в месяц"})),
    )


@given("a Habr client whose submission is not found", target_fixture="apply_scenario")
def not_found_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(404, {"error": "not found"})),
    )


@given("a Habr client whose submission needs a CSRF refresh", target_fixture="apply_scenario")
def csrf_refresh_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(422, {"error": "invalid token"})),
        csrf_pages=_csrf_pages("csrf-1", "csrf-2"),
    )


@given("a Habr client whose submission is rejected as forbidden", target_fixture="apply_scenario")
def forbidden_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(403, {"message": "Forbidden"})),
    )


@given("a Habr client whose submission fails with a server error", target_fixture="apply_scenario")
def server_error_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(_submission(503, {"error": "unavailable"})),
    )


@given("a Habr client whose submission cannot connect", target_fixture="apply_scenario")
def connect_failure_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail()),
        submissions=ScriptedResponses(httpx.ConnectError("connection refused")),
    )


@given("a Habr client that loses its submission but holds the application", target_fixture="apply_scenario")
def lost_but_applied_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail(), _detail(kind="applied")),
        submissions=ScriptedResponses(httpx.ReadError("connection lost")),
    )


@given("a Habr client that loses its submission with no application on the board", target_fixture="apply_scenario")
def lost_unresolved_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        details=ScriptedResponses(_detail(), _detail()),
        submissions=ScriptedResponses(httpx.ReadError("connection lost")),
    )


# --- When ---------------------------------------------------------------------


async def _apply(client: HabrClient, *, message: str | None) -> Result[ApplyResult, ClientError]:
    try:
        return await client.apply_to_vacancy(vacancy_id=ServiceVacancyId(_VACANCY_ID), message=message)
    finally:
        await client.aclose()


@when("the client applies to the vacancy with a cover letter", target_fixture="apply_outcome")
def apply_with_letter_step(apply_scenario: ApplyScenario) -> ApplyOutcome:
    result = async_run(_apply(apply_scenario.make_client(), message=_LETTER))
    return ApplyOutcome(
        result=result,
        requests=tuple(apply_scenario.router.requests),
        delay_calls=apply_scenario.delay.calls,
        delay_time_paths=tuple(apply_scenario.delay_snapshots),
    )


@when("the client applies to the vacancy without a cover letter", target_fixture="apply_outcome")
def apply_without_letter_step(apply_scenario: ApplyScenario) -> ApplyOutcome:
    result = async_run(_apply(apply_scenario.make_client(), message=None))
    return ApplyOutcome(
        result=result,
        requests=tuple(apply_scenario.router.requests),
        delay_calls=apply_scenario.delay.calls,
        delay_time_paths=tuple(apply_scenario.delay_snapshots),
    )


# --- Then ---------------------------------------------------------------------


def _outcome(outcome: ApplyOutcome) -> ApplyResult:
    assert outcome.result.is_ok
    return outcome.result.unwrap()


def _kinds(outcome: ApplyOutcome) -> list[str]:
    kinds: list[str] = []
    for request in outcome.requests:
        path = request.url.path
        if path == _IDENTITY_PATH:
            kinds.append(_IDENTITY)
        elif path == _CSRF_PATH:
            kinds.append(_CSRF)
        elif request.method == "POST" and path.startswith(_POST_PREFIX):
            kinds.append(_POST)
        else:
            kinds.append(_DETAIL)
    return kinds


def _submissions(outcome: ApplyOutcome) -> list[httpx.Request]:
    return [
        request
        for request in outcome.requests
        if request.method == "POST" and request.url.path.startswith(_POST_PREFIX)
    ]


@then("the application succeeds")
def assert_success_step(apply_outcome: ApplyOutcome) -> None:
    assert isinstance(_outcome(apply_outcome), ApplySucceeded)


@then("the current detail is fetched before exactly one multipart submission carrying the letter")
def assert_single_lettered_step(apply_outcome: ApplyOutcome) -> None:
    assert _kinds(apply_outcome) == [_IDENTITY, _DETAIL, _CSRF, _POST]
    submitted = _submissions(apply_outcome)[0]
    assert b'name="body"' in submitted.content
    assert _LETTER.encode() in submitted.content


@then("the submission carried no body field")
def assert_letterless_step(apply_outcome: ApplyOutcome) -> None:
    submitted = _submissions(apply_outcome)[0]
    assert b"body" not in submitted.content


@then("the submission is reported as a protocol failure")
def assert_protocol_failure_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), ProtocolError)


@then("the apply fails with a protocol error")
def assert_apply_protocol_error_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), ProtocolError)


@then(parsers.parse("the outcome is a skip with reason {reason}"))
def assert_skip_step(apply_outcome: ApplyOutcome, reason: str) -> None:
    outcome = _outcome(apply_outcome)
    assert isinstance(outcome, ApplySkipped)
    assert outcome.skip.reason == reason


@then("no submission is sent")
def assert_no_submission_step(apply_outcome: ApplyOutcome) -> None:
    assert _submissions(apply_outcome) == []


@then("the apply fails with an authorization error")
def assert_auth_error_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), AuthError)


@then("the outcome is a per-vacancy failure")
def assert_per_vacancy_failure_step(apply_outcome: ApplyOutcome) -> None:
    assert isinstance(_outcome(apply_outcome), ApplyFailed)


@then("the apply fails with a transport error")
def assert_transport_error_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), TransportError)


@then("the submission was sent twice")
def assert_two_submissions_step(apply_outcome: ApplyOutcome) -> None:
    assert len(_submissions(apply_outcome)) == 2


@then("the pacing delay was invoked twice")
def assert_delay_twice_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.delay_calls == 2


@then("the apply fails with a limit stop")
def assert_limit_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), LimitExceededError)


@then("the apply fails with a not-found error")
def assert_not_found_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), NotFoundError)


@then("the apply fails with a configuration error")
def assert_configuration_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), ConfigurationError)


@then("the submission was sent twice after the CSRF refresh")
def assert_csrf_refresh_step(apply_outcome: ApplyOutcome) -> None:
    assert _kinds(apply_outcome) == [_IDENTITY, _DETAIL, _CSRF, _POST, _CSRF, _POST]


@then("the outcome is a per-vacancy failure with the board status code")
def assert_forbidden_step(apply_outcome: ApplyOutcome) -> None:
    outcome = _outcome(apply_outcome)
    assert isinstance(outcome, ApplyFailed)
    assert outcome.error.code == "http_403"
    assert outcome.error.text == "Forbidden"


@then("the detail was refetched to reconcile")
def assert_reconciled_step(apply_outcome: ApplyOutcome) -> None:
    assert _kinds(apply_outcome) == [_IDENTITY, _DETAIL, _CSRF, _POST, _DETAIL]


@then("the apply fails with an unconfirmed outcome")
def assert_unconfirmed_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), UnknownApplyOutcomeError)


@then("a CSRF request was recorded before the pacing delay ran")
def assert_csrf_prefetched_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.delay_time_paths
    assert _CSRF_PATH in apply_outcome.delay_time_paths[0]


@then("the CSRF request precedes the response POST in the recorded order")
def assert_csrf_precedes_post_step(apply_outcome: ApplyOutcome) -> None:
    kinds = _kinds(apply_outcome)
    assert kinds.index(_CSRF) < kinds.index(_POST)


@then("the pacing delay was never invoked")
def assert_delay_never_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.delay_calls == 0
