"""BDD acceptance for the Habr single-resume listing (test/AGENTS.md §3b)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import ClientError, ResumeInfo
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import HabrSearchEntry, HabrServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver
from test.clients.habr.helpers import (
    DEFAULT_ALIAS,
    RecordingSolver,
    authorized_identity,
    habr_deps,
    profile_id_for,
)

scenarios("bdd/resumes.feature")

_IDENTITY_PATH: Final = "/api/frontend_v1/users/me"


class HabrResumeRouter:
    """Dumb router: the only expected request is the identity healthcheck."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == _IDENTITY_PATH:
            return authorized_identity()
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


@dataclass(frozen=True, slots=True)
class ResumeScenario:
    """Frozen setup: the routed mock and a client factory over it."""

    router: HabrResumeRouter
    make_client: Callable[[], HabrClient]


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """Frozen outcome: the resume result plus the recorded request history."""

    result: Result[list[ResumeInfo], ClientError]
    requests: tuple[httpx.Request, ...]


@given("a Habr client with a healthy session", target_fixture="resume_scenario")
def healthy_session_step(tmp_path: Path) -> ResumeScenario:
    router = HabrResumeRouter()
    data_dir = tmp_path / "data"
    entry = HabrSearchEntry(query="python")

    def make_client() -> HabrClient:
        return HabrClient(
            habr_deps(data_dir, profile_id_for(), RecordingSolver()),
            HabrServiceConfig(searches=(entry,)),
            http_transport=httpx.MockTransport(router),
            browser_driver=FakeHabrBrowserDriver(),
        )

    return ResumeScenario(router=router, make_client=make_client)


async def _list_resumes(client: HabrClient) -> Result[list[ResumeInfo], ClientError]:
    try:
        return await client.get_resumes()
    finally:
        await client.aclose()


@when("the client lists the owned resumes", target_fixture="resume_outcome")
def list_resumes_step(resume_scenario: ResumeScenario) -> ResumeOutcome:
    result = async_run(_list_resumes(resume_scenario.make_client()))
    return ResumeOutcome(result=result, requests=tuple(resume_scenario.router.requests))


@then("the owned resumes contain one entry")
def assert_one_resume_step(resume_outcome: ResumeOutcome) -> None:
    assert resume_outcome.result.is_ok
    assert len(resume_outcome.result.unwrap()) == 1


@then("the entry carries the account alias, the profile name, and no updated timestamp")
def assert_resume_fields_step(resume_outcome: ResumeOutcome) -> None:
    resume = resume_outcome.result.unwrap()[0]
    assert resume.resume_id == DEFAULT_ALIAS
    assert resume.title == "Хабр Человек"
    assert resume.updated_at is None


@then("the identity endpoint was checked exactly once")
def assert_identity_once_step(resume_outcome: ResumeOutcome) -> None:
    identity_requests = [request for request in resume_outcome.requests if request.url.path == _IDENTITY_PATH]
    assert len(identity_requests) == 1
