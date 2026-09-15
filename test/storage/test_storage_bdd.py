"""Phase 3 (tasks 3.2/3.3): Gherkin acceptance scenarios for storage semantics.

Behavioral acceptance of the storage layer expressed as a ``.feature`` file with
typed ``pytest-bdd`` steps (the harness convention from test/AGENTS.md §3:
explicit param/return types, ``parsers.parse`` + ``{x:q}``-style string
placeholders, ``target_fixture`` to carry pipeline state between steps).

Per the async-migration spec (D2 / §6), every step drives the async domain
through the shared file-DB harness :func:`jobfucker.storage.testing.with_db`:
a fresh ``NullPool`` ``AsyncEngine`` on the per-scenario file path
(``scenario_db``) per step call, schema re-created idempotently, engine always
disposed — so an earlier step's writes persist to the file and are visible to a
later step's fresh engine. Steps stay **synchronous** (pytest-bdd silently
no-ops ``async def`` steps); the ``with_db`` callable runs on its own
``asyncio.run`` loop and may ``await`` the repositories directly. The
snapshot identity/no-op-dedupe scenarios drive the real :class:`PipelineService`
(constructed inside the callable over the per-call :class:`Storage`) — the
no-op dedupe is core behavior, not a repository concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from jobfucker.app.pipeline_service import PipelineService
from jobfucker.clients.base import ClientDeps, ServiceVacancyId
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline
from jobfucker.storage.testing import with_db
from test.pipeline_helpers import build_pipeline_config, make_factory
from test.storage.builders import build_vacancy, make_pipeline, make_snapshot

scenarios("bdd/storage.feature")


@dataclass(frozen=True, slots=True)
class AppendState:
    """Snapshot-append observation used by the id-stability scenario."""

    id: int
    snapshot_nos: list[int]


@dataclass(frozen=True, slots=True)
class SvcState:
    """Service-created pipeline observation (id + snapshot count)."""

    id: int
    snapshot_count: int


@dataclass(frozen=True, slots=True)
class NoopState:
    """No-op update observation (unchanged, snapshot count)."""

    id: int
    snapshot_count: int
    unchanged: bool


@pytest.fixture
def scenario_db(tmp_path: Path) -> Path:
    """The per-scenario file DB path (stable across the scenario's steps).

    Each scenario is one generated pytest item, so ``tmp_path`` is fresh per
    scenario; all steps of that scenario open fresh engines on this same path
    and see each other's committed writes (the D2/§6 file-DB persistence path).
    """
    return tmp_path / "scenario.db"


@given("a pipeline exists", target_fixture="stored_pipeline")
def pipeline_step(scenario_db: Path) -> Pipeline:
    """Create a pipeline identity + its first (current) snapshot."""

    async def seed(storage: Storage) -> Pipeline:
        pipeline = await storage.pipelines.create(make_pipeline(name="bdd-pipeline"))
        snap = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1, note="base"))
        await storage.pipelines.set_current_snapshot(pipeline.id, snap.id)
        return pipeline

    return with_db(scenario_db, seed)


@given(
    parsers.parse('it has an applied vacancy "{applied}" and a pending vacancy "{pending}"'),
    target_fixture="seeded_result_ids",
)
def seed_result_vacancies_step(
    scenario_db: Path,
    stored_pipeline: Pipeline,
    applied: str,
    pending: str,
) -> frozenset[ServiceVacancyId]:
    """Seed an applied and a pending vacancy for the skip-already-processed check."""

    async def seed(storage: Storage) -> frozenset[ServiceVacancyId]:
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=stored_pipeline.id,
                external_id=applied,
                apply_status="applied",
            )
        )
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=stored_pipeline.id,
                external_id=pending,
                apply_status="pending",
            )
        )
        return frozenset({ServiceVacancyId(applied)})

    return with_db(scenario_db, seed)


@when("the decided external ids are queried", target_fixture="processed_ids")
def query_processed_step(scenario_db: Path, stored_pipeline: Pipeline) -> frozenset[ServiceVacancyId]:
    """Query which external ids already reached an apply decision (skip-already-processed on apply)."""
    return frozenset(with_db(scenario_db, lambda s: s.vacancies.external_ids_decided(stored_pipeline.id)))


@then(parsers.parse('the processed set contains "{done}" but not "{pending}"'))
def assert_processed_set_step(
    processed_ids: frozenset[ServiceVacancyId],
    seeded_result_ids: frozenset[ServiceVacancyId],
    done: str,
    pending: str,
) -> None:
    """Assert 'done' is present and 'pending' is absent from the processed set."""
    del seeded_result_ids
    assert ServiceVacancyId(done) in processed_ids
    assert ServiceVacancyId(pending) not in processed_ids


@when(
    parsers.parse('the counter for service "{service}", login "{login}" on "{date}" is incremented three times'),
    target_fixture="incremented_limit",
)
def increment_three_times_step(scenario_db: Path, service: str, login: str, date: str) -> int:
    """Increment the auth-keyed daily counter three times and return the count."""

    async def increment(storage: Storage) -> int:
        for _ in range(3):
            await storage.daily_limits.increment(service, login, date)
        limit = await storage.daily_limits.get(service, login, date)
        assert limit is not None
        return limit.count

    return with_db(scenario_db, increment)


@then(parsers.parse("its count is {expected:d} and only one row exists for that service, login and date"))
def assert_counter_step(scenario_db: Path, incremented_limit: int, expected: int) -> None:
    """Assert the counter equals the expected value (idempotent increments)."""
    assert incremented_limit == expected

    async def read_count(storage: Storage) -> int:
        limit = await storage.daily_limits.get("mock", "a@ex.com", "2026-08-05")
        assert limit is not None
        return limit.count

    assert with_db(scenario_db, read_count) == expected


@when("a second snapshot is appended and made current", target_fixture="append_state")
def append_snapshot_step(scenario_db: Path, stored_pipeline: Pipeline) -> AppendState:
    """Append snapshot #2 and repoint the head; the identity id must not change."""

    async def append(storage: Storage) -> AppendState:
        snap = await storage.snapshots.create(
            make_snapshot(pipeline_id=stored_pipeline.id, snapshot_no=2, note="changed")
        )
        await storage.pipelines.set_current_snapshot(stored_pipeline.id, snap.id)
        identity = await storage.pipelines.get(stored_pipeline.id)
        assert identity is not None
        return AppendState(
            id=identity.id,
            snapshot_nos=[s.snapshot_no for s in await storage.snapshots.list(stored_pipeline.id)],
        )

    return with_db(scenario_db, append)


@then("the pipeline id is unchanged and there are two snapshots")
def assert_id_stable_step(stored_pipeline: Pipeline, append_state: AppendState) -> None:
    """Assert the identity id survived the append and the chain grew to two."""
    assert append_state.id == stored_pipeline.id
    assert append_state.snapshot_nos == [1, 2]


@given("a pipeline is created with a config", target_fixture="svc_state")
def svc_create_step(scenario_db: Path, client_deps: ClientDeps) -> SvcState:
    """Create a pipeline through the service (identity + snapshot #1)."""

    async def create(storage: Storage) -> SvcState:
        svc = PipelineService(storage, make_factory(client_deps))
        result = await svc.create(build_pipeline_config())
        assert result.is_ok
        res = result.unwrap()
        return SvcState(
            id=res.pipeline.id,
            snapshot_count=len((await svc.snapshots(res.pipeline.id)).unwrap()),
        )

    return with_db(scenario_db, create)


@when("it is updated with the identical config", target_fixture="noop_state")
def svc_noop_step(scenario_db: Path, client_deps: ClientDeps, svc_state: SvcState) -> NoopState:
    """Update with the identical config — a no-op that must append zero rows."""

    async def noop(storage: Storage) -> NoopState:
        svc = PipelineService(storage, make_factory(client_deps))
        result = await svc.new_snapshot(svc_state.id, build_pipeline_config())
        assert result.is_ok
        res = result.unwrap()
        return NoopState(
            id=res.pipeline.id,
            snapshot_count=len((await svc.snapshots(svc_state.id)).unwrap()),
            unchanged=res.unchanged,
        )

    return with_db(scenario_db, noop)


@then("the snapshot history still has exactly one row")
def assert_one_row_step(svc_state: SvcState, noop_state: NoopState) -> None:
    """Assert the no-op was dropped: id stable, one snapshot, unchanged flagged."""
    assert noop_state.id == svc_state.id
    assert noop_state.snapshot_count == 1
    assert noop_state.unchanged is True
