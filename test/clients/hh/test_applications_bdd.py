"""BDD acceptance coverage for HH applications: preflight, submission, reconciliation.

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given`` seeds
a frozen ``ApplyScenario`` (data-configured transport + recorders + the test
solution), the ``When`` returns a frozen ``ApplyOutcome``, and ``Then`` steps
only assert.
"""

from __future__ import annotations

import html
import json
import re
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
    ProtocolError,
    ServiceVacancyId,
    UnknownApplyOutcomeError,
)
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHSearchEntry, HHServiceConfig
from jobfucker.clients.hh.models import PersistedCookie
from jobfucker.hh_tests.contract import HhTestProblem, HhTestSolution, HhTestTaskAnswer
from jobfucker.testing.step_runner import async_run
from test.clients.hh.browser_fake import FakeBrowserDriver, SubmitOutcome
from test.clients.hh.helpers import (
    DetailPayload,
    EnvelopePayload,
    ErrorItemPayload,
    JsonBlob,
    RecordingSolver,
    StubAuthInteraction,
    applicant_healthcheck,
    hh_client,
    json_response,
    resume_item,
    resumes_page,
    seed_auth_state,
    vacancy_detail,
)

scenarios("bdd/applications.feature")

_PROFILE_ID = "applications-profile"
_VACANCY_ID = "12345"
_COVER_LETTER = "Здравствуйте! Готов обсудить задачу."
_RECOVERY_URL = "https://hh.ru/account/captcha?state=challenge-state"

SubmissionsResponder = Callable[[httpx.Request, int], httpx.Response]
NegotiationsResponder = Callable[[int], httpx.Response]

_STANDARD_SOLUTION: Final[HhTestSolution] = {
    "1": HhTestTaskAnswer(text="Потому что"),
    "2": HhTestTaskAnswer(option_id="20"),
}
_CHOICE_OPEN_SOLUTION: Final[HhTestSolution] = {
    # «Свой вариант»: the open literal + text, not the preset option.
    "1": HhTestTaskAnswer(text="Потому что"),
    "2": HhTestTaskAnswer(open=True, text="Свой вариант"),
}


class ApplyTransport:
    """External-boundary fake serving the documented apply contract.

    A dumb router over the data each ``Given`` configures: the canned detail,
    the apply-page HTML, the popup response, the submission script, and the
    negotiations pages. No scenario enum lives here.
    """

    def __init__(
        self,
        *,
        detail: DetailPayload,
        apply_page: httpx.Response,
        popup: httpx.Response,
        submissions: SubmissionsResponder,
        negotiations: NegotiationsResponder,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._detail = detail
        self._apply_page = apply_page
        self._popup = popup
        self._submissions = submissions
        self._negotiations = negotiations

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.hh.ru":
            return self._api(request)
        if request.url.host == "hh.ru":
            return self._website(request)
        raise AssertionError(f"Unexpected host: {request.url}")

    def _website(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/applicant/vacancy_response":
            return self._apply_page
        if request.method == "POST" and request.url.path == "/applicant/vacancy_response/popup":
            return self._popup
        raise AssertionError(f"Unexpected website request: {request.method} {request.url}")

    def _api(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/me":
            return applicant_healthcheck()
        if request.method == "GET" and request.url.path == "/resumes/mine":
            return json_response(resumes_page([resume_item("resume-1", title="Python разработчик")]))
        if request.method == "GET" and request.url.path == f"/vacancies/{_VACANCY_ID}":
            return json_response(self._detail)
        if request.method == "POST" and request.url.path == "/negotiations":
            prior = self._prior_submissions()
            return self._submissions(request, prior)
        if request.method == "GET" and request.url.path == "/negotiations":
            assert request.url.params["status"] == "active"
            return self._negotiations(int(request.url.params["page"]))
        raise AssertionError(f"Unexpected API request: {request.method} {request.url}")

    def _prior_submissions(self) -> int:
        return sum(1 for sent in self.requests[:-1] if sent.method == "POST" and sent.url.path == "/negotiations")


# --- Scripted endpoint data (assembled by the Givens) -----------------------

_DEFAULT_TASKS: tuple[JsonBlob, ...] = (
    {
        "id": 1,
        "description": "<p>Почему вы?</p>",
        "multiple": "false",
        "open": "true",
        "candidateSolutions": [],
    },
    {
        "id": 2,
        "description": "<p>Готовы?</p>",
        "multiple": "false",
        "open": "false",
        "candidateSolutions": [{"id": "20", "text": "Да"}, {"id": "21", "text": "Нет"}],
    },
)


def _choice_open_tasks() -> tuple[JsonBlob, ...]:
    """Task 2 becomes a choice + «Свой вариант» task (tests.md §1)."""
    return (
        _DEFAULT_TASKS[0],
        {
            "id": 2,
            "description": "<p>Выберите или предложите своё</p>",
            "multiple": "false",
            "open": "true",
            "candidateSolutions": [{"id": "20", "text": "Да"}, {"id": "21", "text": "Нет"}],
        },
    )


def _changed_tasks() -> tuple[JsonBlob, ...]:
    """The submit-time re-fetch sees a test that gained a task after solving."""
    return (
        *_DEFAULT_TASKS,
        {
            "id": 3,
            "description": "<p>Новый вопрос</p>",
            "multiple": "false",
            "open": "false",
            "candidateSolutions": [{"id": "30", "text": "Да"}],
        },
    )


def _apply_page(
    *,
    tasks: tuple[JsonBlob, ...] = _DEFAULT_TASKS,
    response_impossible: bool = False,
    resume_applicable: bool = True,
    with_test: bool = True,
) -> httpx.Response:
    """The apply-page HTML with the escaped HH-Lux-InitialState blob."""
    blob: JsonBlob = {
        "applicantVacancyResponseStatuses": {
            _VACANCY_ID: {
                "alreadyApplied": False,
                "responseImpossible": response_impossible,
                # Keyed by the short resume id; the configured resume id is the hash.
                "resumes": {"283656288": {"_attributes": {"hash": "resume-1", "id": "283656288"}}},
                # The per-vacancy selectable set; empty = the configured resume
                # is listed but not applicable to this vacancy.
                "unusedResumeIds": ["283656288"] if resume_applicable else [],
            }
        }
    }
    if with_test:
        blob["vacancyTests"] = {
            _VACANCY_ID: {
                "uidPk": "389524035",
                "guid": "B117D210",
                "name": "Запрос СТДР",
                "description": "<p>Ответьте на вопросы</p>",
                "required": "true",
                "startTime": "1789147702",
                "tasks": list(tasks),
            }
        }
    escaped = html.escape(json.dumps(blob))
    page = f'<html><template style="display:none" id="HH-Lux-InitialState">{escaped}</template></html>'
    return httpx.Response(200, headers={"content-type": "text/html"}, text=page)


def _popup_success() -> httpx.Response:
    """The popup POST outcome on the happy path."""
    return json_response({"success": "true", "topic_id": "1"})


def _popup_error(code: str, *, redirect_url: str | None = None) -> httpx.Response:
    """The popup POST business-error envelope."""
    payload: JsonBlob = {"error": code}
    if redirect_url is not None:
        payload["redirectUrl"] = redirect_url
    return json_response(payload, status=400)


def _fixed_submissions(response: httpx.Response) -> SubmissionsResponder:
    def respond(request: httpx.Request, call: int) -> httpx.Response:
        del request, call
        return response

    return respond


def _captcha_then_success() -> SubmissionsResponder:
    """The first submission meets the challenge; the replayed one succeeds."""

    def respond(request: httpx.Request, call: int) -> httpx.Response:
        del request
        if call == 0:
            return json_response(
                {"errors": [{"type": "captcha_required", "value": "captcha_required", "captcha_url": _RECOVERY_URL}]},
                status=403,
            )
        return httpx.Response(201)

    return respond


def _read_error_submissions() -> SubmissionsResponder:
    """The connection is lost after sending: the outcome stays unconfirmed."""

    def respond(request: httpx.Request, call: int) -> httpx.Response:
        del call
        raise httpx.ReadError("connection lost after sending", request=request)

    return respond


def _envelope_submissions(envelope: EnvelopePayload, *, status: int) -> SubmissionsResponder:
    return _fixed_submissions(json_response(envelope, status=status))


def _empty_negotiations(page: int) -> httpx.Response:
    return json_response({"items": [], "page": page, "pages": 0, "per_page": 100})


def _reconciled_negotiations(page: int) -> httpx.Response:
    items = [{"vacancy": {"id": _VACANCY_ID}}] if page == 0 else []
    return json_response({"items": items, "page": page, "pages": 0, "per_page": 100})


def _reconciled_page2_negotiations(page: int) -> httpx.Response:
    items = [{"vacancy": {"id": _VACANCY_ID}}] if page == 1 else []
    return json_response({"items": items, "page": page, "pages": 1, "per_page": 100})


# --- Frozen carriers ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApplyScenario:
    """Frozen setup: the client factory, recorders, and the screening-test solution."""

    transport: ApplyTransport
    make_client: Callable[[], HHClient]
    driver: FakeBrowserDriver
    solver: RecordingSolver
    test_solution: HhTestSolution


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """Frozen outcome: the action result and the request history it produced."""

    result: Result[ApplyResult, ClientError] | None
    test_problem: HhTestProblem | None
    requests: tuple[httpx.Request, ...]


async def _instant_delay() -> None:
    return None


def _scenario(
    tmp_path: Path,
    *,
    detail: DetailPayload | None = None,
    apply_page: httpx.Response | None = None,
    popup: httpx.Response | None = None,
    submissions: SubmissionsResponder | None = None,
    negotiations: NegotiationsResponder | None = None,
    browser_outcomes: tuple[SubmitOutcome, ...] = (),
    test_solution: HhTestSolution | None = None,
) -> ApplyScenario:
    # Only the captcha scenario clears a challenge; it succeeds on the first answer.
    driver = FakeBrowserDriver(list(browser_outcomes))
    solver = RecordingSolver()
    transport = ApplyTransport(
        detail=detail if detail is not None else vacancy_detail(_VACANCY_ID),
        apply_page=apply_page if apply_page is not None else _apply_page(),
        popup=popup if popup is not None else _popup_success(),
        submissions=submissions if submissions is not None else _fixed_submissions(httpx.Response(201)),
        negotiations=negotiations if negotiations is not None else _empty_negotiations,
    )
    data_dir = tmp_path / "data"
    seed_auth_state(
        data_dir,
        _PROFILE_ID,
        access_token="USER-apply",
        refresh_token="refresh-apply",
        # The website flow needs the _xsrf cookie (its value is also the
        # multipart _xsrf field, tests.md §2).
        cookies=(PersistedCookie(name="_xsrf", value="xsrf-token", domain="hh.ru"),),
    )

    def make_client() -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(resume_id="resume-1", searches=(HHSearchEntry(query="python"),)),
            solver=solver,
            auth_interaction=StubAuthInteraction(),
            browser_driver=driver,
            apply_delay=_instant_delay,
        )

    solution = test_solution if test_solution is not None else _STANDARD_SOLUTION
    return ApplyScenario(
        transport=transport,
        make_client=make_client,
        driver=driver,
        solver=solver,
        test_solution=solution,
    )


# --- Givens (setup variants are distinct steps, never a scenario enum) -------


@given("a healthy HH session and an applicable vacancy", target_fixture="apply_scenario")
def applicable_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path)


@given("a healthy HH session whose submission succeeds with a body", target_fixture="apply_scenario")
def success_with_body_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_fixed_submissions(json_response({"ignored": True}, status=201)),
    )


@given("a healthy HH session and an already-applied vacancy", target_fixture="apply_scenario")
def already_applied_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_envelope_submissions(
            EnvelopePayload(
                description="Already applied",
                bad_argument="vacancy_id",
                errors=[ErrorItemPayload(value="already_applied", type="negotiations")],
            ),
            status=403,
        ),
    )


@given("a healthy HH session and a vacancy whose submission demands a test", target_fixture="apply_scenario")
def envelope_test_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_envelope_submissions(
            EnvelopePayload(errors=[ErrorItemPayload(value="test_required", type="negotiations")]), status=400
        ),
    )


@given("a healthy HH session whose submission answers with a redirect", target_fixture="apply_scenario")
def redirect_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_fixed_submissions(httpx.Response(302, headers={"location": "https://hh.ru/employer/response"})),
    )


@given("a healthy HH session and an archived vacancy", target_fixture="apply_scenario")
def archived_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, detail=vacancy_detail(_VACANCY_ID, archived=True))


@given("a healthy HH session and a vacancy with an attached test", target_fixture="apply_scenario")
def has_test_step(tmp_path: Path) -> ApplyScenario:
    # Test presence alone no longer skips at preflight; the board's
    # test_required submission response is the classifier now.
    return _scenario(
        tmp_path,
        detail=vacancy_detail(_VACANCY_ID, has_test=True),
        submissions=_envelope_submissions(
            EnvelopePayload(errors=[ErrorItemPayload(value="test_required", type="negotiations")]), status=400
        ),
    )


@given("a healthy HH session and a vacancy with an external response form", target_fixture="apply_scenario")
def external_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, detail=vacancy_detail(_VACANCY_ID, response_url="https://example.com/apply"))


@given("a healthy HH session whose configured resume the board rejects", target_fixture="apply_scenario")
def resume_not_found_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_envelope_submissions(
            EnvelopePayload(errors=[ErrorItemPayload(value="resume_not_found", type="bad_argument")]), status=400
        ),
    )


@given("a healthy HH session whose submission hits the daily cap", target_fixture="apply_scenario")
def limit_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(
        tmp_path,
        submissions=_envelope_submissions(
            EnvelopePayload(errors=[ErrorItemPayload(value="limit_exceeded", type="applier")]), status=400
        ),
    )


@given("a healthy HH session whose submission is rejected as unauthorized", target_fixture="apply_scenario")
def unauthorized_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_fixed_submissions(httpx.Response(401)))


@given(
    "a healthy HH session whose submission fails with a server error and no application on the board",
    target_fixture="apply_scenario",
)
def server_error_miss_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_fixed_submissions(httpx.Response(500)))


@given("a healthy HH session whose first submission meets a challenge", target_fixture="apply_scenario")
def captcha_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_captcha_then_success(), browser_outcomes=("success",))


@given("a healthy HH session that loses its submission but holds the application", target_fixture="apply_scenario")
def reconciled_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_read_error_submissions(), negotiations=_reconciled_negotiations)


@given(
    "a healthy HH session that loses its submission and holds the application on page 2",
    target_fixture="apply_scenario",
)
def reconciled_page2_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_read_error_submissions(), negotiations=_reconciled_page2_negotiations)


@given(
    "a healthy HH session that loses its submission with no application on the board",
    target_fixture="apply_scenario",
)
def reconcile_miss_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, submissions=_read_error_submissions())


@given("a healthy HH session and a vacancy whose apply page carries a screening test", target_fixture="apply_scenario")
def screening_test_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path)


@given(
    "a healthy HH session and a vacancy whose apply page no longer carries the test",
    target_fixture="apply_scenario",
)
def test_removed_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, apply_page=_apply_page(with_test=False))


@given(
    "a healthy HH session and a vacancy whose apply page reports response impossible",
    target_fixture="apply_scenario",
)
def response_impossible_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, apply_page=_apply_page(response_impossible=True))


@given("a healthy HH session whose test submission is already applied", target_fixture="apply_scenario")
def error_already_applied_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, popup=_popup_error("alreadyApplied"))


@given("a healthy HH session whose test submission still demands a test", target_fixture="apply_scenario")
def error_test_required_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, popup=_popup_error("test-required"))


@given("a healthy HH session whose test submission requires a cover letter", target_fixture="apply_scenario")
def error_letter_required_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, popup=_popup_error("letter-required"))


@given("a healthy HH session whose test submission has an incomplete resume", target_fixture="apply_scenario")
def error_resume_incomplete_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, popup=_popup_error("resume-incomplete", redirect_url="/profile/resume?resume=deadbeef"))


@given("a healthy HH session whose test gains a task after solving", target_fixture="apply_scenario")
def changed_after_solving_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, apply_page=_apply_page(tasks=_changed_tasks()))


@given(
    "a healthy HH session whose configured resume is not applicable to the vacancy",
    target_fixture="apply_scenario",
)
def resume_not_applicable_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, apply_page=_apply_page(resume_applicable=False))


@given(
    "a healthy HH session and a vacancy whose apply page carries a choice-open test",
    target_fixture="apply_scenario",
)
def choice_open_test_step(tmp_path: Path) -> ApplyScenario:
    return _scenario(tmp_path, apply_page=_apply_page(tasks=_choice_open_tasks()), test_solution=_CHOICE_OPEN_SOLUTION)


# --- Whens (the action returns its frozen outcome) ---------------------------


async def _apply_once(client: HHClient) -> Result[ApplyResult, ClientError]:
    try:
        return await client.apply_to_vacancy(
            resume_id="resume-1",
            vacancy_id=ServiceVacancyId(_VACANCY_ID),
            message=_COVER_LETTER,
        )
    finally:
        await client.aclose()


async def _fetch_test_once(client: HHClient) -> HhTestProblem | None:
    try:
        fetched = await client.get_vacancy_test(ServiceVacancyId(_VACANCY_ID))
        return fetched.unwrap() if fetched.is_ok else None
    finally:
        await client.aclose()


async def _apply_with_test_once(client: HHClient, solution: HhTestSolution) -> Result[ApplyResult, ClientError]:
    try:
        return await client.apply_to_vacancy_with_test(
            resume_id="resume-1",
            vacancy_id=ServiceVacancyId(_VACANCY_ID),
            message=_COVER_LETTER,
            solution=solution,
        )
    finally:
        await client.aclose()


@when("the client applies to the vacancy with a cover letter", target_fixture="apply_outcome")
def apply_step(apply_scenario: ApplyScenario) -> ApplyOutcome:
    result = async_run(_apply_once(apply_scenario.make_client()))
    return ApplyOutcome(result=result, test_problem=None, requests=tuple(apply_scenario.transport.requests))


@when("the client fetches the vacancy's screening test", target_fixture="apply_outcome")
def fetch_test_step(apply_scenario: ApplyScenario) -> ApplyOutcome:
    test_problem = async_run(_fetch_test_once(apply_scenario.make_client()))
    return ApplyOutcome(result=None, test_problem=test_problem, requests=tuple(apply_scenario.transport.requests))


@when("the client applies to the vacancy with test answers", target_fixture="apply_outcome")
def apply_with_test_step(apply_scenario: ApplyScenario) -> ApplyOutcome:
    result = async_run(_apply_with_test_once(apply_scenario.make_client(), apply_scenario.test_solution))
    return ApplyOutcome(result=result, test_problem=None, requests=tuple(apply_scenario.transport.requests))


# --- Thens (assert only) ------------------------------------------------------


def _submissions(outcome: ApplyOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.method == "POST" and request.url.path == "/negotiations"]


def _popup_submissions(outcome: ApplyOutcome) -> list[httpx.Request]:
    return [
        request
        for request in outcome.requests
        if request.method == "POST" and request.url.path == "/applicant/vacancy_response/popup"
    ]


def _hosts_and_paths(outcome: ApplyOutcome) -> list[str]:
    return [f"{request.url.host}{request.url.path}" for request in outcome.requests]


@then("the application succeeds")
def succeeds_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_ok
    assert isinstance(apply_outcome.result.unwrap(), ApplySucceeded)


@then("the submission is reported as a protocol failure")
def protocol_failure_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), ProtocolError)


@then(parsers.parse("the outcome is a skip with reason {reason}"))
def skip_reason_step(apply_outcome: ApplyOutcome, reason: str) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_ok
    outcome = apply_outcome.result.unwrap()
    assert isinstance(outcome, ApplySkipped)
    assert outcome.skip.reason == reason


@then("the apply fails with a configuration error")
def configuration_error_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), ConfigurationError)


@then("the apply fails with a limit stop")
def limit_stop_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), LimitExceededError)


@then("the apply fails with an authorization error")
def authorization_error_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), AuthError)


@then("the apply fails with an unconfirmed outcome")
def unconfirmed_step(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_err
    assert isinstance(apply_outcome.result.unwrap_err(), UnknownApplyOutcomeError)


@then("HH receives exactly one form-encoded submission carrying the resume, vacancy, and message")
def exact_submission_step(apply_outcome: ApplyOutcome) -> None:
    submissions = _submissions(apply_outcome)
    assert len(submissions) == 1
    request = submissions[0]
    assert request.url.host == "api.hh.ru"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = dict(field.split("=", maxsplit=1) for field in request.content.decode().split("&"))
    assert body["resume_id"] == "resume-1"
    assert body["vacancy_id"] == _VACANCY_ID
    assert "message" in body


@then("the current detail is fetched before the submission")
def assert_detail_first(apply_outcome: ApplyOutcome) -> None:
    hosts = _hosts_and_paths(apply_outcome)
    detail_index = hosts.index(f"api.hh.ru/vacancies/{_VACANCY_ID}")
    submission_index = next(i for i, h in enumerate(hosts) if h == "api.hh.ru/negotiations")
    assert detail_index < submission_index


@then("the test carries the parsed free-text and choice tasks")
def assert_test_tasks(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.test_problem is not None
    problem = apply_outcome.test_problem
    assert problem.name == "Запрос СТДР"
    assert [task.kind for task in problem.tasks] == ["free_text", "choice"]
    assert problem.tasks[1].options[0].id == "20"


@then("the website submission carried the answers and resume hash")
def assert_website_submission(apply_outcome: ApplyOutcome) -> None:
    submissions = _popup_submissions(apply_outcome)
    assert len(submissions) == 1
    body = submissions[0].content.decode("utf-8", "replace")
    # resume_hash is the blob's _attributes.hash (the configured resume id),
    # found despite the short resume-id map key.
    resume_hash = re.search(r'name="resume_hash"\r?\n\r?\n([^\r\n]*)', body)
    assert resume_hash is not None and resume_hash.group(1) == "resume-1"
    # Exact answer encoding, not just field presence.
    assert re.search(r'name="task_1_text"\r?\n\r?\nПотому что', body)
    assert re.search(r'name="task_2"\r?\n\r?\n20', body)
    # Mandatory blob-derived fields carry the page's values.
    for field, value in (("_xsrf", "xsrf-token"), ("uidPk", "389524035"), ("startTime", "1789147702")):
        assert re.search(rf'name="{field}"\r?\n\r?\n{value}', body), field
    without_test = re.search(r'name="withoutTest"\r?\n\r?\n([^\r\n]*)', body)
    assert without_test is not None and without_test.group(1) == "no"
    assert apply_outcome.result is not None and apply_outcome.result.is_ok


@then("the website submission uses the choice-open encoding")
def assert_choice_open_submission(apply_outcome: ApplyOutcome) -> None:
    submissions = _popup_submissions(apply_outcome)
    assert len(submissions) == 1
    body = submissions[0].content.decode("utf-8", "replace")
    # «Свой вариант»: the literal ``open`` plus the free text, never the preset id.
    assert re.search(r'name="task_2"\r?\n\r?\nopen', body)
    assert re.search(r'name="task_2_text"\r?\n\r?\nСвой вариант', body)
    assert apply_outcome.result is not None and apply_outcome.result.is_ok


@then("the standard API submission was used")
def assert_standard_submission(apply_outcome: ApplyOutcome) -> None:
    assert _popup_submissions(apply_outcome) == []
    assert len(_submissions(apply_outcome)) == 1


@then("no website submission is sent")
def assert_no_website_submission(apply_outcome: ApplyOutcome) -> None:
    assert _popup_submissions(apply_outcome) == []


@then(parsers.parse("the test apply fails with code {code}"))
def assert_test_apply_failed_code(apply_outcome: ApplyOutcome, code: str) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_ok
    outcome = apply_outcome.result.unwrap()
    assert isinstance(outcome, ApplyFailed)
    assert outcome.error.code == code


@then("the failure names the resume edit page")
def assert_resume_edit_page(apply_outcome: ApplyOutcome) -> None:
    assert apply_outcome.result is not None and apply_outcome.result.is_ok
    outcome = apply_outcome.result.unwrap()
    assert isinstance(outcome, ApplyFailed)
    assert "/profile/resume" in outcome.error.text


@then("no submission is sent")
def assert_no_submission(apply_outcome: ApplyOutcome) -> None:
    assert _submissions(apply_outcome) == []


@then("a submission is sent")
def assert_submission_sent(apply_outcome: ApplyOutcome) -> None:
    # Test presence alone no longer skips at preflight; the board's
    # test_required response is the classifier now.
    assert len(_submissions(apply_outcome)) == 1


@then("HH receives two submissions after the solved challenge")
def replayed_submission_step(apply_outcome: ApplyOutcome) -> None:
    assert len(_submissions(apply_outcome)) == 2


@then("the browser engine submitted one answer through the real page")
def one_answer_step(apply_scenario: ApplyScenario) -> None:
    assert apply_scenario.driver.answers_submitted == ["answer"]
    assert apply_scenario.driver.sessions[0].pages_opened == 1
    assert apply_scenario.driver.sessions[0].images_served == 1
    assert apply_scenario.solver.images == apply_scenario.driver.images_served


@then("the active negotiations list is consulted")
def reconciliation_step(apply_outcome: ApplyOutcome) -> None:
    assert "api.hh.ru/negotiations" in _hosts_and_paths(apply_outcome)
