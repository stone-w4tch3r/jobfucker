"""BDD acceptance for the apply stage's hh screening-test path (test/AGENTS.md §3b)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyResult,
    ApplySucceeded,
    AuthError,
    ClientDeps,
    ClientError,
    ProtocolError,
    ServiceVacancyId,
)
from jobfucker.hh_tests.contract import (
    HhTestProblem,
    HhTestSolution,
    HhTestSolveOutcome,
    HhTestTask,
    HhTestTaskAnswer,
    HhTestUnsolved,
)
from jobfucker.stages.apply import ApplyFilters, ApplyReport, ApplyTargets, run_apply
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord
from jobfucker.testing.step_runner import async_run
from test.fakes.fake_client import FakeClient
from test.pipeline_helpers import build_pipeline_config, create_pipeline, snapshot_for
from test.storage.builders import build_vacancy

scenarios("bdd/hh_test_apply.feature")

_VACANCY_ID = "v-test"
_PLAIN_VACANCY_ID = "v-plain"
_SOLVER_ERROR = "AI could not answer"
_SOLVER_COMMENT = "resume does not say whether I can relocate"
_FETCH_ERROR = "blob failed validation"

SolveFn = Callable[[HhTestProblem, str], Awaitable[Result[HhTestSolveOutcome, str]]]


class HhTestFakeClient(FakeClient):
    """A :class:`FakeClient` that also implements the hh test capability.

    The capability protocol is the ``isinstance`` gate the apply stage checks;
    this double makes the gate pass and records the submitted solution so the
    scenario can assert what was sent without a live board.
    """

    def __init__(
        self,
        deps: ClientDeps,
        *,
        problem: HhTestProblem | None,
        fetch_error: ClientError | None = None,
    ) -> None:
        super().__init__(deps, build_pipeline_config().service_section)
        self._problem: HhTestProblem | None = problem
        self._fetch_error: ClientError | None = fetch_error
        self.submitted: list[HhTestSolution] = []

    async def get_vacancy_test(self, vacancy_id: ServiceVacancyId) -> Result[HhTestProblem | None, ClientError]:
        del vacancy_id
        if self._fetch_error is not None:
            return Err(self._fetch_error)
        return Ok(self._problem)

    async def apply_to_vacancy_with_test(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None,
        solution: HhTestSolution,
    ) -> Result[ApplyResult, ClientError]:
        del resume_id, vacancy_id, message
        self.submitted.append(solution)
        return Ok(ApplySucceeded())


@dataclass(frozen=True, slots=True)
class SeededPipeline:
    """Frozen carrier: the pipeline identity and its head snapshot id."""

    pipeline: Pipeline
    snapshot_id: int


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """Frozen carrier: the stage report, the client, and the persisted rows."""

    report: ApplyReport
    client: HhTestFakeClient
    vacancies: tuple[VacancyRecord, ...]


def _problem() -> HhTestProblem:
    return HhTestProblem(
        vacancy_id=ServiceVacancyId(_VACANCY_ID),
        name="Test",
        description="intro",
        tasks=(HhTestTask(id="1", kind="free_text", prompt="Why?"),),
    )


@given("a pipeline with a test-bearing eligible vacancy", target_fixture="seeded")
def seeded_step(storage: Storage) -> SeededPipeline:
    async def seed() -> SeededPipeline:
        pipeline = await create_pipeline(storage)
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id=_VACANCY_ID,
                score=5,
                cover_letter="letter",
                apply_status=None,
                has_hh_test=True,
            )
        )
        snapshot = await snapshot_for(storage, pipeline)
        return SeededPipeline(pipeline=pipeline, snapshot_id=snapshot.id)

    return async_run(seed())


@given("a pipeline with an already-applied unscored test-bearing vacancy", target_fixture="seeded")
def seeded_applied_unscored_step(storage: Storage) -> SeededPipeline:
    async def seed() -> SeededPipeline:
        pipeline = await create_pipeline(storage)
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id=_VACANCY_ID,
                score=None,
                cover_letter=None,
                apply_status="applied",
                has_hh_test=True,
            )
        )
        snapshot = await snapshot_for(storage, pipeline)
        return SeededPipeline(pipeline=pipeline, snapshot_id=snapshot.id)

    return async_run(seed())


@given("an hh-capable client whose test fetch returns a screening test", target_fixture="capable_client")
def capable_client_step(client_deps: ClientDeps) -> HhTestFakeClient:
    return HhTestFakeClient(client_deps, problem=_problem())


@given("an hh-capable client whose test fetch returns no test", target_fixture="capable_client")
def capable_client_no_test_step(client_deps: ClientDeps) -> HhTestFakeClient:
    return HhTestFakeClient(client_deps, problem=None)


@given("an hh-capable client whose test fetch fails", target_fixture="capable_client")
def capable_client_error_step(client_deps: ClientDeps) -> HhTestFakeClient:
    return HhTestFakeClient(client_deps, problem=None, fetch_error=ProtocolError(message=_FETCH_ERROR))


@given("an hh-capable client whose test fetch is rejected as unauthorized", target_fixture="capable_client")
def capable_client_auth_error_step(client_deps: ClientDeps) -> HhTestFakeClient:
    return HhTestFakeClient(client_deps, problem=None, fetch_error=AuthError(message=_FETCH_ERROR))


@given("a pipeline with one test-bearing and one testless eligible vacancy", target_fixture="seeded")
def seeded_mixed_step(storage: Storage) -> SeededPipeline:
    async def seed() -> SeededPipeline:
        pipeline = await create_pipeline(storage)
        for external_id, has_test in ((_VACANCY_ID, True), (_PLAIN_VACANCY_ID, False)):
            await storage.vacancies.upsert(
                build_vacancy(
                    pipeline_id=pipeline.id,
                    external_id=external_id,
                    score=5,
                    cover_letter="letter",
                    apply_status=None,
                    has_hh_test=has_test,
                )
            )
        snapshot = await snapshot_for(storage, pipeline)
        return SeededPipeline(pipeline=pipeline, snapshot_id=snapshot.id)

    return async_run(seed())


@given("a scripted test solver returning answers", target_fixture="test_solver")
def solver_step() -> SolveFn:
    async def solve(problem: HhTestProblem, prompt: str) -> Result[HhTestSolveOutcome, str]:
        del problem, prompt
        return Ok({"1": HhTestTaskAnswer(text="Because")})

    return solve


@given("a declining test solver", target_fixture="test_solver")
def declining_solver_step() -> SolveFn:
    async def solve(problem: HhTestProblem, prompt: str) -> Result[HhTestSolveOutcome, str]:
        del problem, prompt
        return Ok(HhTestUnsolved(comment=_SOLVER_COMMENT))

    return solve


@given("a failing test solver", target_fixture="test_solver")
def failing_solver_step() -> SolveFn:
    async def solve(problem: HhTestProblem, prompt: str) -> Result[HhTestSolveOutcome, str]:
        del problem, prompt
        return Err(_SOLVER_ERROR)

    return solve


@given("no test solver", target_fixture="test_solver")
def no_solver_step() -> None:
    return None


def _run_stage(
    storage: Storage,
    seeded: SeededPipeline,
    client: HhTestFakeClient,
    solver: SolveFn | None,
    filters: ApplyFilters | None = None,
) -> ApplyOutcome:
    async def run() -> ApplyOutcome:
        config = build_pipeline_config()
        snapshot = await snapshot_for(storage, seeded.pipeline)
        result = await run_apply(
            storage,
            seeded.pipeline,
            ApplyTargets(client=client, resume_id=config.service_section.resume_id),
            snapshot_id=seeded.snapshot_id,
            min_required_score=snapshot.min_required_score,
            daily_apply_limit=snapshot.daily_apply_limit,
            login=snapshot.login,
            service=snapshot.service,
            filters=filters if filters is not None else ApplyFilters(),
            hh_test_solver=solver,
        )
        report: ApplyReport = result.unwrap()
        vacancies = tuple(await storage.vacancies.list_by_pipeline(seeded.pipeline.id))
        return ApplyOutcome(report=report, client=client, vacancies=vacancies)

    return async_run(run())


@when("the apply stage runs", target_fixture="outcome")
def run_stage_step(
    storage: Storage,
    seeded: SeededPipeline,
    capable_client: HhTestFakeClient,
    test_solver: SolveFn | None,
) -> ApplyOutcome:
    return _run_stage(storage, seeded, capable_client, test_solver)


@when("the apply stage runs allowing unscored vacancies", target_fixture="outcome")
def run_stage_unscored_step(
    storage: Storage,
    seeded: SeededPipeline,
    capable_client: HhTestFakeClient,
    test_solver: SolveFn | None,
) -> ApplyOutcome:
    return _run_stage(storage, seeded, capable_client, test_solver, filters=ApplyFilters(include_unscored=True))


@when(parsers.parse("the apply stage runs selecting only the {side} vacancies"), target_fixture="outcome")
def run_stage_filtered_step(
    storage: Storage,
    seeded: SeededPipeline,
    capable_client: HhTestFakeClient,
    test_solver: SolveFn | None,
    side: str,
) -> ApplyOutcome:
    flag = "only_with_hh_tests" if side == "test-bearing" else "only_without_hh_tests"
    return _run_stage(storage, seeded, capable_client, test_solver, filters=ApplyFilters(has_hh_test=flag))


@then("the vacancy is applied")
def assert_applied(outcome: ApplyOutcome) -> None:
    assert outcome.report.applied == 1
    assert outcome.vacancies[0].apply_status == "applied"


@then("the test answers were submitted through the capability")
def assert_answers_submitted(outcome: ApplyOutcome) -> None:
    assert len(outcome.client.submitted) == 1
    assert outcome.client.submitted[0]["1"].text == "Because"


@then("the vacancy is failed mentioning the solver error")
def assert_solver_error(outcome: ApplyOutcome) -> None:
    assert outcome.vacancies[0].apply_status == "error"
    assert _SOLVER_ERROR in (outcome.vacancies[0].apply_error or "")


@then("the vacancy is skipped mentioning the solver comment")
def assert_solver_decline(outcome: ApplyOutcome) -> None:
    assert outcome.vacancies[0].apply_status == "skipped"
    assert outcome.vacancies[0].skip_reason == _SOLVER_COMMENT
    assert outcome.report.skipped == 1
    assert outcome.report.failed == 0


@then("the vacancy is failed mentioning no solver")
def assert_no_solver(outcome: ApplyOutcome) -> None:
    assert outcome.vacancies[0].apply_status == "error"
    assert "no solver" in (outcome.vacancies[0].apply_error or "")


@then("the vacancy is failed mentioning the test fetch error")
def assert_fetch_error(outcome: ApplyOutcome) -> None:
    assert outcome.vacancies[0].apply_status == "error"
    assert _FETCH_ERROR in (outcome.vacancies[0].apply_error or "")


@then("the batch stops and the vacancy stays pending")
def assert_batch_stop(outcome: ApplyOutcome) -> None:
    assert outcome.report.stopped_early is True
    assert outcome.report.pending == 1
    assert outcome.vacancies[0].apply_status is None


def _status_by_id(outcome: ApplyOutcome) -> dict[str, str | None]:  # lint-ignore[raw-dict]: test lookup map
    return {str(vacancy.external_id): vacancy.apply_status for vacancy in outcome.vacancies}


@then("only the test-bearing vacancy is applied")
def assert_only_test_bearing(outcome: ApplyOutcome) -> None:
    statuses = _status_by_id(outcome)
    assert statuses[_VACANCY_ID] == "applied"
    assert statuses[_PLAIN_VACANCY_ID] is None


@then("only the testless vacancy is applied")
def assert_only_testless(outcome: ApplyOutcome) -> None:
    statuses = _status_by_id(outcome)
    assert statuses[_PLAIN_VACANCY_ID] == "applied"
    assert statuses[_VACANCY_ID] is None


@then("the vacancy is not re-attempted and stays applied")
def assert_not_reattempted(outcome: ApplyOutcome) -> None:
    assert outcome.report.total == 0
    assert outcome.client.submitted == []
    assert outcome.vacancies[0].apply_status == "applied"
