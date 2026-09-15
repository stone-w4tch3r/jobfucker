"""BDD acceptance coverage for HH resume listing and configured-resume validation.

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given`` seeds
a frozen ``ResumeScenario`` (data-configured transport + recorders), the
``When`` returns a frozen ``ResumeOutcome``, and ``Then`` steps only assert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import (
    ApplyResult,
    ApplySucceeded,
    AuthError,
    ClientError,
    ConfigurationError,
    ProtocolError,
    ResumeInfo,
    ServiceVacancyId,
)
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHSearchEntry, HHServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.hh.browser_fake import FakeBrowserDriver
from test.clients.hh.helpers import (
    RecordingSolver,
    ResumeItemPayload,
    applicant_healthcheck,
    hh_client,
    json_response,
    resume_item,
    resumes_page,
    seed_auth_state,
    vacancy_detail,
)

scenarios("bdd/resumes.feature")

_PROFILE_ID = "resumes-profile"
_VACANCY_ID = "12345"
_RESUME_ID = "resume-1"
_COVER_LETTER = "Здравствуйте! Готов обсудить задачу."
_RECOVERY_URL = "https://hh.ru/account/captcha?state=challenge-state"

ResumesResponder = Callable[[httpx.Request, int], httpx.Response]


class ResumeTransport:
    """External-boundary fake: a dumb router over the scripted resume reads."""

    def __init__(self, *, resumes_for: ResumesResponder, healthcheck: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._resumes_for = resumes_for
        self._healthcheck = healthcheck if healthcheck is not None else applicant_healthcheck()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "api.hh.ru":
            raise AssertionError(f"Unexpected host: {request.url}")
        if request.method == "GET" and request.url.path == "/me":
            return self._healthcheck
        if request.method == "GET" and request.url.path == "/resumes/mine":
            read = sum(1 for sent in self.requests[:-1] if sent.url.path == "/resumes/mine")
            return self._resumes_for(request, read)
        if request.method == "GET" and request.url.path == f"/vacancies/{_VACANCY_ID}":
            return json_response(vacancy_detail(_VACANCY_ID))
        if request.method == "POST" and request.url.path == "/negotiations":
            return httpx.Response(201)
        raise AssertionError(f"Unexpected API request: {request.method} {request.url}")


# --- Scripted resume-page responders (one per setup, built from data) --------


def _resume_read(items: list[ResumeItemPayload], page: int = 0, *, pages: int = 0) -> httpx.Response:
    return json_response(resumes_page(items, page, pages=pages))


def _multi_page(request: httpx.Request, read: int) -> httpx.Response:
    del read
    page = int(request.url.params["page"])
    # `pages` is the TOTAL page count; two pages means 0..1.
    items = [resume_item(_RESUME_ID)] if page == 0 else [resume_item("resume-2")]
    return _resume_read(items, page, pages=2)


def _empty(request: httpx.Request, read: int) -> httpx.Response:
    del read
    return _resume_read([], int(request.url.params["page"]))


def _null_flag(request: httpx.Request, read: int) -> httpx.Response:
    del read
    return _resume_read([resume_item(_RESUME_ID, can_publish=None)], int(request.url.params["page"]))


def _malformed(request: httpx.Request, read: int) -> httpx.Response:
    del request, read
    return httpx.Response(200, headers={"content-type": "application/json"}, content=b"not-json")


def _unauthorized(request: httpx.Request, read: int) -> httpx.Response:
    del request, read
    return httpx.Response(401)


def _unpublished(request: httpx.Request, read: int) -> httpx.Response:
    del read
    return _resume_read([resume_item(_RESUME_ID, status="draft")], int(request.url.params["page"]))


def _missing(request: httpx.Request, read: int) -> httpx.Response:
    del read
    return _resume_read([resume_item("foreign-resume")], int(request.url.params["page"]))


def _published(request: httpx.Request, read: int) -> httpx.Response:
    del read
    return _resume_read([resume_item(_RESUME_ID, can_publish=True)], int(request.url.params["page"]))


def _challenge_first(responder: ResumesResponder) -> ResumesResponder:
    """Wrap a responder so the first read meets the standalone challenge."""

    def respond(request: httpx.Request, read: int) -> httpx.Response:
        if read == 0:
            return json_response(
                {"errors": [{"type": "captcha_required", "value": "captcha_required", "captcha_url": _RECOVERY_URL}]},
                status=403,
            )
        return responder(request, read)

    return respond


@dataclass(frozen=True, slots=True)
class ResumeScenario:
    """Frozen setup: the client factory plus the interaction recorders to assert on."""

    transport: ResumeTransport
    make_client: Callable[[], HHClient]
    driver: FakeBrowserDriver
    solver: RecordingSolver


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """Frozen outcome: the action result and the request history it produced."""

    listing: Result[list[ResumeInfo], ClientError] | None
    applications: tuple[Result[ApplyResult, ClientError], ...]
    requests: tuple[httpx.Request, ...]


async def _instant_delay() -> None:
    return None


def _scenario(
    tmp_path: Path,
    *,
    responder: ResumesResponder,
    healthcheck: httpx.Response | None = None,
    driver: FakeBrowserDriver | None = None,
) -> ResumeScenario:
    browser = driver if driver is not None else FakeBrowserDriver([])
    solver = RecordingSolver()
    transport = ResumeTransport(resumes_for=responder, healthcheck=healthcheck)
    data_dir = tmp_path / "data"
    seed_auth_state(data_dir, _PROFILE_ID, access_token="USER-resumes", refresh_token="refresh-resumes")

    def make_client() -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(resume_id=_RESUME_ID, searches=(HHSearchEntry(query="python"),)),
            solver=solver,
            browser_driver=browser,
            apply_delay=_instant_delay,
        )

    return ResumeScenario(transport=transport, make_client=make_client, driver=browser, solver=solver)


@given("a healthy HH session whose resumes span two pages", target_fixture="resume_scenario")
def multi_page_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_multi_page)


@given("a healthy HH session whose account holds no resumes", target_fixture="resume_scenario")
def empty_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_empty)


@given("a healthy HH session whose resume carries a null publish flag", target_fixture="resume_scenario")
def null_flag_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_null_flag)


@given("a healthy HH session whose resume page is malformed", target_fixture="resume_scenario")
def malformed_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_malformed)


@given("a healthy HH session whose resume read is rejected as unauthorized", target_fixture="resume_scenario")
def unauthorized_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_unauthorized)


@given("a healthy HH session whose first resume read meets a challenge", target_fixture="resume_scenario")
def captcha_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_challenge_first(_published), driver=FakeBrowserDriver(["success"]))


@given("a session whose healthcheck fails with a server error", target_fixture="resume_scenario")
def auth_failure_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_empty, healthcheck=json_response({}, status=500))


@given("a healthy HH session whose configured resume is unpublished", target_fixture="resume_scenario")
def unpublished_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_unpublished)


@given("a healthy HH session whose configured resume is not owned", target_fixture="resume_scenario")
def missing_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_missing)


@given("a healthy HH session whose configured resume is published", target_fixture="resume_scenario")
def published_step(tmp_path: Path) -> ResumeScenario:
    return _scenario(tmp_path, responder=_published)


async def _list_once(client: HHClient) -> Result[list[ResumeInfo], ClientError]:
    try:
        return await client.get_resumes()
    finally:
        await client.aclose()


async def _apply_once(client: HHClient) -> Result[ApplyResult, ClientError]:
    try:
        return await client.apply_to_vacancy(
            resume_id=_RESUME_ID,
            vacancy_id=ServiceVacancyId(_VACANCY_ID),
            message=_COVER_LETTER,
        )
    finally:
        await client.aclose()


async def _apply_twice_on_client(client: HHClient) -> tuple[Result[ApplyResult, ClientError], ...]:
    # One client instance: validation memoization is per client, so a batch
    # of two applications must read /resumes/mine exactly once.
    try:
        return tuple(
            [
                await client.apply_to_vacancy(
                    resume_id=_RESUME_ID,
                    vacancy_id=ServiceVacancyId(_VACANCY_ID),
                    message=_COVER_LETTER,
                )
                for _ in range(2)
            ]
        )
    finally:
        await client.aclose()


@when("the client lists the owned resumes", target_fixture="resume_outcome")
def list_step(resume_scenario: ResumeScenario) -> ResumeOutcome:
    listing = async_run(_list_once(resume_scenario.make_client()))
    return ResumeOutcome(listing=listing, applications=(), requests=tuple(resume_scenario.transport.requests))


@when("the client applies to the vacancy with a cover letter", target_fixture="resume_outcome")
def apply_step(resume_scenario: ResumeScenario) -> ResumeOutcome:
    applied = async_run(_apply_once(resume_scenario.make_client()))
    return ResumeOutcome(listing=None, applications=(applied,), requests=tuple(resume_scenario.transport.requests))


@when("the client applies to the vacancy twice with a cover letter", target_fixture="resume_outcome")
def apply_twice_step(resume_scenario: ResumeScenario) -> ResumeOutcome:
    applied = async_run(_apply_twice_on_client(resume_scenario.make_client()))
    return ResumeOutcome(listing=None, applications=applied, requests=tuple(resume_scenario.transport.requests))


def _resume_reads(outcome: ResumeOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.method == "GET" and request.url.path == "/resumes/mine"]


def _submissions(outcome: ResumeOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.method == "POST" and request.url.path == "/negotiations"]


@then("the owned resumes are reported")
def reported_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.listing is not None and resume_outcome.listing.is_ok


@then(parsers.parse("the listing contains exactly {count:d} resumes"))
def count_step(resume_outcome: ResumeOutcome, count: int) -> None:
    assert resume_outcome.listing is not None
    assert len(resume_outcome.listing.unwrap()) == count


@then("the reported resumes keep the board order and fields")
def order_and_fields_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.listing is not None
    reported = resume_outcome.listing.unwrap()
    assert [resume.resume_id for resume in reported] == [_RESUME_ID, "resume-2"]
    first = reported[0]
    assert first.title == f"Резюме {_RESUME_ID}"
    assert first.updated_at == "2026-08-30T10:00:00+0300"


@then("the listing is a protocol failure")
def protocol_failure_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.listing is not None and resume_outcome.listing.is_err
    assert isinstance(resume_outcome.listing.unwrap_err(), ProtocolError)


@then("the listing fails with an authorization error")
def auth_failure_listing_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.listing is not None and resume_outcome.listing.is_err
    assert isinstance(resume_outcome.listing.unwrap_err(), AuthError)


@then("the listing fails before any resume read")
def short_circuit_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.listing is not None and resume_outcome.listing.is_err
    assert _resume_reads(resume_outcome) == []


@then("HH receives exactly two resume reads")
def two_reads_step(resume_outcome: ResumeOutcome) -> None:
    # Pins the stop condition: pages=2 means exactly pages 0 and 1 are read —
    # never a third (past-the-end reads return clamped duplicate content).
    assert len(_resume_reads(resume_outcome)) == 2


@then("HH receives two resume reads after the solved challenge")
def replayed_read_step(resume_outcome: ResumeOutcome) -> None:
    assert len(_resume_reads(resume_outcome)) == 2


@then("the browser engine submitted one answer through the real page")
def one_answer_step(resume_scenario: ResumeScenario) -> None:
    assert resume_scenario.driver.answers_submitted == ["answer"]
    assert resume_scenario.driver.sessions[0].pages_opened == 1
    assert resume_scenario.driver.sessions[0].images_served == 1
    assert resume_scenario.solver.images == resume_scenario.driver.images_served


@then("no resume read is sent")
def no_read_step(resume_outcome: ResumeOutcome) -> None:
    assert _resume_reads(resume_outcome) == []


@then("the apply stops with a configuration error")
def configuration_stop_step(resume_outcome: ResumeOutcome) -> None:
    assert len(resume_outcome.applications) == 1
    result = resume_outcome.applications[0]
    assert result.is_err
    assert isinstance(result.unwrap_err(), ConfigurationError)


@then("no submission POST is sent")
def no_submission_step(resume_outcome: ResumeOutcome) -> None:
    assert _submissions(resume_outcome) == []


@then("both applications succeed")
def both_succeed_step(resume_outcome: ResumeOutcome) -> None:
    assert len(resume_outcome.applications) == 2
    for result in resume_outcome.applications:
        assert result.is_ok
        assert isinstance(result.unwrap(), ApplySucceeded)


@then("the resume listing is requested exactly once across both applications")
def validated_once_step(resume_outcome: ResumeOutcome) -> None:
    assert len(_resume_reads(resume_outcome)) == 1
