"""Phase 5B checkpoint: full-pipeline integration against the mock (Gherkin).

Runs fetch → score → generate_cv → apply end to end through the real engine,
real :class:`Factory` + a registered :class:`MockClient`, and a stub
:class:`AiClient` (no network, no live board). Covers the happy path, idempotency
on a re-run (no double apply), a per-auth limit stop (``Ok`` with
``limit_reached``, remaining vacancies pending), and the pipeline-snapshots
success criteria exercised end to end:

- a no-op update appends **zero** snapshots and keeps the identity (SC1);
- two pipelines sharing one login exhaust **one shared** daily counter (SC4);
- a vacancy re-fetched under a newer snapshot keeps one row with both
  provenance stamps (SC3).

Async-migration BDD convention (D1): steps are **synchronous** and call the
async domain through :func:`jobfucker.testing.step_runner.async_run_result`
(``Ok`` → value, ``Err`` → ``pytest.fail`` at the step), so every ``@when``
below returns the **unwrapped** report / mutation result — never ``async def``
steps (pytest-bdd silently no-ops them → false green).
"""

from __future__ import annotations

from dataclasses import dataclass

from pytest_bdd import given, scenarios, then, when

from jobfucker.app.pipeline_service import PipelineMutationResult, PipelineService
from jobfucker.clients.base import ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.params import MockSearchEntry, MockServiceConfig
from jobfucker.config import LimitsConfig, PipelineConfig
from jobfucker.engine import BatchSelector, Engine, PipelineRunReport
from jobfucker.limits import today_iso
from jobfucker.stages.apply import ApplyReport
from jobfucker.stages.fetch import FetchReport
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot
from jobfucker.testing.step_runner import async_run, async_run_result
from test.pipeline_helpers import (
    MOCK_FILTER,
    MOCK_RESUME_ID,
    ScriptedAi,
    build_pipeline_config,
    create_pipeline,
    make_factory,
    mock_vacancies,
    snapshot_for,
)

scenarios("bdd/pipeline_flow.feature")

_TODAY = today_iso()
_AUTH = "login@example.com"
_SERVICE = "mock"


@dataclass(frozen=True, slots=True)
class EngineCtx:
    """The state BDD steps share for one scenario."""

    storage: Storage
    pipeline: Pipeline
    engine: Engine


@dataclass(frozen=True, slots=True)
class ServiceCtx:
    """A stored pipeline + the service that owns its lifecycle (no-op update)."""

    svc: PipelineService
    storage: Storage
    identity: Pipeline
    snapshot: PipelineSnapshot
    config: PipelineConfig


@dataclass(frozen=True, slots=True)
class SharedLimitCtx:
    """Two pipelines sharing one account login, each with a runnable engine."""

    storage: Storage
    pipelines: tuple[Pipeline, Pipeline]
    engines: tuple[Engine, Engine]


@dataclass(frozen=True, slots=True)
class ProvenanceCtx:
    """A pipeline scored under snapshot #1, ready to be re-fetched under #2."""

    storage: Storage
    svc: PipelineService
    deps: ClientDeps
    pipeline: Pipeline
    config: PipelineConfig
    snap_a: PipelineSnapshot


def _build_engine(storage: Storage, factory: Factory, pipeline: Pipeline, config: PipelineConfig) -> Engine:
    """A real mock-backed engine stamping the pipeline's head snapshot id."""
    return Engine(
        storage=storage,
        factory=factory,
        config=config,
        ai=ScriptedAi(),
        snapshot_id=async_run(snapshot_for(storage, pipeline)).id,
    )


@given("a fresh engine against the mock", target_fixture="engine_ctx")
def fresh_engine(storage: Storage, client_deps: ClientDeps) -> EngineCtx:
    """A fresh stored pipeline + engine wrapping the registered mock client."""
    factory = make_factory(client_deps)
    pipeline = async_run(create_pipeline(storage))
    engine = _build_engine(storage, factory, pipeline, build_pipeline_config(login=_AUTH))
    return EngineCtx(storage=storage, pipeline=pipeline, engine=engine)


@given("an engine whose pipeline has already been run once", target_fixture="engine_ctx")
def already_run(storage: Storage, client_deps: ClientDeps) -> EngineCtx:
    """Same as a fresh engine, but the pipeline is pre-populated by a first run."""
    factory = make_factory(client_deps)
    pipeline = async_run(create_pipeline(storage))
    engine = _build_engine(storage, factory, pipeline, build_pipeline_config(login=_AUTH))
    ctx = EngineCtx(storage=storage, pipeline=pipeline, engine=engine)
    async_run_result(engine.run(pipeline))
    return ctx


@given("an engine with a one-per-day apply limit and several vacancies", target_fixture="engine_ctx")
def limited_engine(storage: Storage, client_deps: ClientDeps) -> EngineCtx:
    """A fresh engine whose pipeline caps at one apply per day."""
    factory = make_factory(client_deps)
    pipeline = async_run(create_pipeline(storage, daily_apply_limit=1))
    engine = _build_engine(storage, factory, pipeline, build_pipeline_config(daily_apply_limit=1, login=_AUTH))
    return EngineCtx(storage=storage, pipeline=pipeline, engine=engine)


@when("the pipeline runs end to end", target_fixture="run_report")
def run_end_to_end(engine_ctx: EngineCtx) -> PipelineRunReport:
    """Run all four stages in sequence and capture the combined report."""
    return async_run_result(engine_ctx.engine.run(engine_ctx.pipeline))


@when("the pipeline runs a second time", target_fixture="run_report")
def run_second_time(engine_ctx: EngineCtx) -> ApplyReport:
    """Re-run idempotently: apply only, skipping already-processed vacancies.

    Fetch is an insert-only mirror sync (it never overwrites processed state),
    so no re-fetch can wipe results; true idempotency — no double apply — is
    exercised by calling ``apply`` directly with ``skip_already_processed`` to
    protect the earlier results.
    """
    return async_run_result(
        engine_ctx.engine.apply(
            engine_ctx.pipeline,
            selector=BatchSelector(skip_already_processed=True),
        )
    )


@then("every fetched vacancy is applied and the daily counters reflect it")
def assert_all_applied(engine_ctx: EngineCtx, run_report: PipelineRunReport) -> None:
    """All 3 mock vacancies end applied and the daily counters record 3."""
    assert run_report.fetch[0].fetched == 3
    assert run_report.apply.applied == 3

    stored = async_run(engine_ctx.storage.vacancies.list_by_pipeline(engine_ctx.pipeline.id))
    assert all(v.apply_status == "applied" for v in stored)
    limit = async_run(engine_ctx.storage.daily_limits.get(_SERVICE, _AUTH, _TODAY))
    assert limit is not None and limit.count == 3


@then("no vacancy is applied again")
def assert_no_double_apply(engine_ctx: EngineCtx, run_report: ApplyReport) -> None:
    """A re-run applies to nothing (idempotent) and counters are unchanged."""
    assert run_report.applied == 0
    stored = async_run(engine_ctx.storage.vacancies.list_by_pipeline(engine_ctx.pipeline.id))
    assert all(v.apply_status == "applied" for v in stored)
    limit = async_run(engine_ctx.storage.daily_limits.get(_SERVICE, _AUTH, _TODAY))
    assert limit is not None and limit.count == 3


@then("only one apply happens and the report flags limit_reached")
def assert_limited(engine_ctx: EngineCtx, run_report: PipelineRunReport) -> None:
    """Exactly one apply; the rest stay pending; the Ok report flags limit_reached."""
    report = run_report.apply
    assert report.applied == 1
    assert report.limit_reached is True
    assert report.pending == 2

    stored = {
        v.external_id: v for v in async_run(engine_ctx.storage.vacancies.list_by_pipeline(engine_ctx.pipeline.id))
    }
    applied = [v for v in stored.values() if v.apply_status == "applied"]
    pending = [v for v in stored.values() if v.apply_status is None]
    assert len(applied) == 1
    assert len(pending) == 2


# --- SC1: a no-op update appends zero snapshots and keeps the id --------------
@given("a stored pipeline with its config snapshot", target_fixture="svc_ctx")
def stored_pipeline_with_snapshot(storage: Storage, client_deps: ClientDeps) -> ServiceCtx:
    """A pipeline created through the service (identity + snapshot #1)."""
    svc = PipelineService(storage, make_factory(client_deps))
    config = build_pipeline_config(name="noop")
    m = async_run_result(svc.create(config))
    return ServiceCtx(svc=svc, storage=storage, identity=m.pipeline, snapshot=m.snapshot, config=config)


@when("its identical config is re-applied", target_fixture="update_result")
def reapply_identical(svc_ctx: ServiceCtx) -> PipelineMutationResult:
    """Append a snapshot with the exact same config (must be a no-op in core)."""
    return async_run_result(svc_ctx.svc.new_snapshot(svc_ctx.identity.id, svc_ctx.config))


@then("no snapshot is appended and the id is unchanged")
def assert_noop_update(svc_ctx: ServiceCtx, update_result: PipelineMutationResult) -> None:
    """Zero new snapshot rows; the identity id is stable and never soft-deleted."""
    assert update_result.unchanged is True
    snaps = async_run(svc_ctx.storage.snapshots.list(svc_ctx.identity.id))
    assert [s.snapshot_no for s in snaps] == [1]
    stored = async_run(svc_ctx.storage.pipelines.get(svc_ctx.identity.id))
    assert stored is not None and stored.id == svc_ctx.identity.id and stored.soft_deleted_at is None


# --- SC4: two pipelines sharing one login share one daily counter -------------
@given("two pipelines sharing one account login", target_fixture="shared_ctx")
def two_pipelines_shared_login(storage: Storage, client_deps: ClientDeps) -> SharedLimitCtx:
    """Two distinct identities on the same auth account, each with an engine."""
    factory = make_factory(client_deps)
    login = "shared@example.com"
    pipelines = (
        async_run(create_pipeline(storage, name="Shared-A", login=login, daily_apply_limit=100)),
        async_run(create_pipeline(storage, name="Shared-B", login=login, daily_apply_limit=100)),
    )
    engines = (
        _build_engine(
            storage,
            factory,
            pipelines[0],
            build_pipeline_config(name="Shared-A", login=login, daily_apply_limit=100),
        ),
        _build_engine(
            storage,
            factory,
            pipelines[1],
            build_pipeline_config(name="Shared-B", login=login, daily_apply_limit=100),
        ),
    )
    return SharedLimitCtx(storage=storage, pipelines=pipelines, engines=engines)


@when("both pipelines run end to end", target_fixture="shared_runs")
def run_both_pipelines(shared_ctx: SharedLimitCtx) -> tuple[PipelineRunReport, PipelineRunReport]:
    """Run each pipeline's four stages; both share the same auth counter."""
    return (
        async_run_result(shared_ctx.engines[0].run(shared_ctx.pipelines[0])),
        async_run_result(shared_ctx.engines[1].run(shared_ctx.pipelines[1])),
    )


@then("the shared daily counter records the combined total and both pipelines are applied")
def assert_shared_counter(shared_ctx: SharedLimitCtx, shared_runs: tuple[PipelineRunReport, PipelineRunReport]) -> None:
    """One counter row for the auth (never per pipeline), counting both runs."""
    for pipeline in shared_ctx.pipelines:
        stored = async_run(shared_ctx.storage.vacancies.list_by_pipeline(pipeline.id))
        assert len(stored) == 3
        assert all(v.apply_status == "applied" for v in stored)
    auth_rows = [
        row
        for row in async_run(shared_ctx.storage.daily_limits.list())
        if row.service == _SERVICE and row.login == "shared@example.com" and row.date == _TODAY
    ]
    # One shared counter row, counting every application across both pipelines.
    assert len(auth_rows) == 1
    assert auth_rows[0].count == 6


# --- SC3: a re-fetch under a newer snapshot keeps one row with both stamps ----
@given("a pipeline scored under its first snapshot", target_fixture="prov_ctx")
def scored_under_first(storage: Storage, client_deps: ClientDeps) -> ProvenanceCtx:
    """Fetch + score under snapshot #1, then hold the pieces for a re-fetch."""
    factory = make_factory(client_deps)
    config = build_pipeline_config(name="prov")
    svc = PipelineService(storage, factory)
    m = async_run_result(svc.create(config))
    engine = _build_engine(storage, factory, m.pipeline, config)
    async_run_result(engine.fetch(m.pipeline))
    async_run_result(engine.score(m.pipeline))
    return ProvenanceCtx(
        storage=storage,
        svc=svc,
        deps=client_deps,
        pipeline=m.pipeline,
        config=config,
        snap_a=m.snapshot,
    )


@when("a newer snapshot is appended and the pipeline is re-fetched under it", target_fixture="refetch_report")
def re_fetch_under_newer(prov_ctx: ProvenanceCtx) -> tuple[FetchReport, ...]:
    """Append snapshot #2 with a changed query, then refetch under it (--refresh)."""
    changed = prov_ctx.config.model_copy(update={"limits": LimitsConfig(daily_apply_limit=99)})
    m = async_run_result(prov_ctx.svc.new_snapshot(prov_ctx.pipeline.id, changed))
    snap_b = m.snapshot
    assert snap_b.snapshot_no == 2
    # The listing changed under snapshot #2, so the refresh writes each row.
    changed_factory = make_factory(
        prov_ctx.deps,
        section=MockServiceConfig(
            resume_id=MOCK_RESUME_ID,
            searches=(
                MockSearchEntry(
                    query="python",
                    filter=MOCK_FILTER,
                    vacancies=[v.model_copy(update={"title": f"{v.title} v2"}) for v in mock_vacancies()],
                ),
            ),
        ),
    )
    engine_b = Engine(
        storage=prov_ctx.storage,
        factory=changed_factory,
        config=changed,
        ai=ScriptedAi(),
        snapshot_id=snap_b.id,
    )
    return async_run_result(engine_b.fetch(prov_ctx.pipeline, refresh=True))


@then("one row survives with fetched and scored snapshot ids pointing at their snapshots")
def assert_provenance(prov_ctx: ProvenanceCtx, refetch_report: tuple[FetchReport, ...]) -> None:
    """One live row per external id; fetched/scored stamps point at their snapshots."""
    snap_b = async_run(prov_ctx.storage.snapshots.current(prov_ctx.pipeline.id))
    assert snap_b is not None and snap_b.snapshot_no == 2
    rows = async_run(prov_ctx.storage.vacancies.list_by_pipeline(prov_ctx.pipeline.id))
    assert len(rows) == 3  # one row per external id, never duplicated across snapshots
    for vacancy in rows:
        assert vacancy.fetched_snapshot_id == snap_b.id
        assert vacancy.scored_snapshot_id == prov_ctx.snap_a.id
