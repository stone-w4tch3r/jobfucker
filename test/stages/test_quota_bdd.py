"""BDD acceptance for per-window apply quotas (test/AGENTS.md §3b).

Covers the two independent counters (shared per-auth + per-pipeline) over a
board-declared window: a monthly account cap stops at the quota, a new month
starts a fresh counter, and pipelines sharing an account keep their own
per-pipeline counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pytest_bdd import given, parsers, scenarios, then, when

from jobfucker.clients.base import ClientDeps, ServiceInfo
from jobfucker.stages.apply import ApplyFilters, ApplyReport, ApplyTargets, run_apply
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline
from jobfucker.testing.step_runner import async_run
from test.fakes.fake_client import FakeClient
from test.pipeline_helpers import build_pipeline_config, create_pipeline, snapshot_for
from test.storage.builders import build_vacancy

scenarios("bdd/quota.feature")

_LOGIN = "login@example.com"
_SERVICE = "mock"
_THIS_MONTH = "2026-08"
_LAST_MONTH = "2026-07"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _FivePerMonthClient(FakeClient):
    """A fake whose account quota is 5 applications per calendar month."""

    service_info = ServiceInfo(service="fake", per_auth_apply_cap=5, apply_period="month", max_search_items=None)


class _FiftyPerMonthClient(FakeClient):
    """A fake with a large monthly account cap (the per-pipeline cap binds instead)."""

    service_info = ServiceInfo(service="fake", per_auth_apply_cap=50, apply_period="month", max_search_items=None)


@dataclass(frozen=True, slots=True)
class PairCtx:
    """Two pipelines on one account, each with its own fake client and counter."""

    storage: Storage
    pipelines: tuple[Pipeline, Pipeline]
    client: _FiftyPerMonthClient


def _seed_eligible(storage: Storage, pipeline: Pipeline, count: int) -> None:
    async def seed() -> None:
        for index in range(count):
            await storage.vacancies.upsert(
                build_vacancy(
                    pipeline_id=pipeline.id,
                    external_id=f"v{index}",
                    score=5,
                    score_reasoning="good",
                    cover_letter=f"letter {index}",
                    apply_status=None,
                )
            )

    async_run(seed())


def _run_once(storage: Storage, pipeline: Pipeline, client: FakeClient, *, now: datetime = _NOW) -> ApplyReport:
    async def run() -> ApplyReport:
        config = build_pipeline_config()
        snapshot = await snapshot_for(storage, pipeline)
        result = await run_apply(
            storage,
            pipeline,
            ApplyTargets(client=client, resume_id=config.service_section.resume_id),
            snapshot_id=snapshot.id,
            min_required_score=snapshot.min_required_score,
            apply_limit=snapshot.apply_limit,
            login=snapshot.login,
            service=snapshot.service,
            filters=ApplyFilters(),
            now=now,
        )
        return result.unwrap()

    return async_run(run())


# --- Given -------------------------------------------------------------------
@given("a monthly client with a 5-per-month account cap", target_fixture="monthly_client")
def five_per_month_client_step(client_deps: ClientDeps) -> _FivePerMonthClient:
    return _FivePerMonthClient(client_deps, build_pipeline_config().service_section)


@given("a monthly client with a 50-per-month account cap", target_fixture="monthly_client")
def fifty_per_month_client_step(client_deps: ClientDeps) -> _FiftyPerMonthClient:
    return _FiftyPerMonthClient(client_deps, build_pipeline_config().service_section)


@given("a pipeline with apply limit 10", target_fixture="pipeline")
def pipeline_step(storage: Storage) -> Pipeline:
    return async_run(create_pipeline(storage, apply_limit=10, login=_LOGIN))


@given(parsers.parse("{count:d} applications already recorded for the account {window} month"))
def recorded_step(storage: Storage, count: int, window: str) -> None:
    """Pre-spend the shared per-auth counter in the current or previous month's window."""
    _record(storage, _THIS_MONTH if window == "this" else _LAST_MONTH, count)


def _record(storage: Storage, month_key: str, count: int) -> None:
    async def record() -> None:
        for _ in range(count):
            await storage.auth_apply_limits.increment(_SERVICE, _LOGIN, "month", month_key)

    async_run(record())


@given("2 eligible vacancies")
def eligible_vacancies_step(storage: Storage, pipeline: Pipeline) -> None:
    _seed_eligible(storage, pipeline, 2)


@given("two pipelines with apply limit 1 sharing one login", target_fixture="pair")
def shared_pair_step(storage: Storage, client_deps: ClientDeps) -> PairCtx:
    first = async_run(create_pipeline(storage, name="quota-a", apply_limit=1, login=_LOGIN))
    second = async_run(create_pipeline(storage, name="quota-b", apply_limit=1, login=_LOGIN))
    _seed_eligible(storage, first, 2)
    _seed_eligible(storage, second, 2)
    client = _FiftyPerMonthClient(client_deps, build_pipeline_config().service_section)
    return PairCtx(storage=storage, pipelines=(first, second), client=client)


# --- When --------------------------------------------------------------------
@when("the apply stage runs", target_fixture="report")
def run_stage_step(storage: Storage, pipeline: Pipeline, monthly_client: FakeClient) -> ApplyReport:
    return _run_once(storage, pipeline, monthly_client)


@when("each pipeline runs once over 2 eligible vacancies", target_fixture="pair_reports")
def run_pair_step(pair: PairCtx) -> tuple[ApplyReport, ApplyReport]:
    first, second = pair.pipelines
    return (
        _run_once(pair.storage, first, pair.client),
        _run_once(pair.storage, second, pair.client),
    )


# --- Then --------------------------------------------------------------------
@then("1 vacancy is applied")
def assert_one_applied(report: ApplyReport) -> None:
    assert report.applied == 1
    assert report.limit_reached is True
    assert report.pending == 1


@then("2 vacancies are applied")
def assert_two_applied(report: ApplyReport) -> None:
    assert report.applied == 2
    assert report.limit_reached is False


@then("0 vacancies are applied")
def assert_none_applied(report: ApplyReport) -> None:
    """A pre-spent account counter stops before the first attempt."""
    assert report.applied == 0
    assert report.stopped_early is True
    assert report.limit_reached is True
    assert report.pending == 2


@then("the stop names the monthly account cap")
def assert_monthly_stop(report: ApplyReport) -> None:
    assert report.stop_message == ("monthly limit reached — 5 of 5 applied this month (board per-account cap)")


@then("the account counter for this month is 2")
def assert_this_month_counter(storage: Storage) -> None:
    row = async_run(storage.auth_apply_limits.get(_SERVICE, _LOGIN, "month", _THIS_MONTH))
    assert row is not None and row.count == 2


@then("each pipeline applied 1 vacancy")
def assert_each_applied(pair_reports: tuple[ApplyReport, ApplyReport]) -> None:
    assert pair_reports[0].applied == 1
    assert pair_reports[1].applied == 1


@then("the account counter is 2")
def assert_account_counter(pair: PairCtx) -> None:
    row = async_run(pair.storage.auth_apply_limits.get(_SERVICE, _LOGIN, "month", _THIS_MONTH))
    assert row is not None and row.count == 2


@then("each pipeline counter is 1")
def assert_pipeline_counters(pair: PairCtx) -> None:
    for pipeline in pair.pipelines:
        row = async_run(pair.storage.pipeline_apply_limits.get(pipeline.id, "month", _THIS_MONTH))
        assert row is not None and row.count == 1
