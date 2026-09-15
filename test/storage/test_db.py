"""Phase 3 (task 3.2): repository integration tests against an in-memory DB.

These are DB-backed **integration** tests (``integration`` marker): they drive the
real repositories in :mod:`jobfucker.storage.db` over a fresh
``sqlite:///:memory:`` engine (the ``storage`` fixture builds the schema and a
:class:`Storage` facade per test). They prove identity + snapshot CRUD and the
``current`` head pointer, snapshot-provenance preservation on vacancy upsert,
audit-with-snapshot, auth-keyed daily-limit increments — and that every
repository returns the **frozen, board-neutral DTOs** from ``dto.py``.

Behavioural acceptance scenarios (re-run freshness, soft-delete, limits, id
stability, no-op dedupe) are also expressed as Gherkin in
``test/storage/bdd/storage.feature`` collected by ``test_storage_bdd.py``; this
module covers the lower-level CRUD/query surface with plain pytest integration
calls, per the harness convention.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql_text

from jobfucker.clients.base import ServiceVacancyId
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import PipelineSnapshot, VacancyRecord
from jobfucker.storage.models import PipelineSnapshot as PipelineSnapshotRow
from test.storage.builders import build_vacancy, make_pipeline, make_snapshot


# --- Pipeline identity -------------------------------------------------------
@pytest.mark.integration
async def test_create_pipeline_assigns_id_and_returns_identity_dto(storage: Storage) -> None:
    created = await storage.pipelines.create(make_pipeline(name="alpha"))
    assert created.id > 0
    assert created.name == "alpha"
    assert created.current_snapshot_id is None
    # identity-only: no config columns on the DTO
    assert not hasattr(created, "query")
    # default from DDL applied on write
    stored = await storage.pipelines.get(created.id)
    assert stored is not None
    assert stored.name == "alpha"


@pytest.mark.integration
async def test_get_pipeline_missing_returns_none(storage: Storage) -> None:
    assert await storage.pipelines.get(99999) is None


@pytest.mark.integration
async def test_list_pipelines_excludes_soft_deleted(storage: Storage) -> None:
    keep = await storage.pipelines.create(make_pipeline(name="keep"))
    removed = await storage.pipelines.create(make_pipeline(name="remove"))
    assert await storage.pipelines.soft_delete(removed.id) is True

    listed = await storage.pipelines.list()
    assert [p.id for p in listed] == [keep.id]


@pytest.mark.integration
async def test_soft_delete_missing_returns_false(storage: Storage) -> None:
    assert await storage.pipelines.soft_delete(424242) is False


@pytest.mark.integration
async def test_find_by_name_returns_active_identity(storage: Storage) -> None:
    await storage.pipelines.create(make_pipeline(name="demo"))
    found = await storage.pipelines.find_by_name("demo")
    assert found is not None
    assert found.name == "demo"
    assert await storage.pipelines.find_by_name("nope") is None


@pytest.mark.integration
async def test_set_current_snapshot_repoints_head(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline(name="p"))
    s1 = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    s2 = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=2))

    assert await storage.pipelines.set_current_snapshot(pipeline.id, s1.id) is True
    assert (await storage.pipelines.get(pipeline.id)).current_snapshot_id == s1.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
    assert (await storage.snapshots.current(pipeline.id)).id == s1.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call

    assert await storage.pipelines.set_current_snapshot(pipeline.id, s2.id) is True
    assert (await storage.pipelines.get(pipeline.id)).current_snapshot_id == s2.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
    assert (await storage.snapshots.current(pipeline.id)).id == s2.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call

    assert await storage.pipelines.set_current_snapshot(99999, s1.id) is False


# --- PipelineSnapshot repo ---------------------------------------------------
@pytest.mark.integration
async def test_snapshot_create_assigns_id(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    snap = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    assert snap.id > 0
    assert snap.pipeline_id == pipeline.id
    assert snap.snapshot_no == 1


@pytest.mark.integration
async def test_snapshot_current_none_when_no_head(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    assert await storage.snapshots.current(pipeline.id) is None


@pytest.mark.integration
async def test_snapshot_list_and_next_snapshot_no(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    assert await storage.snapshots.next_snapshot_no(pipeline.id) == 1
    await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=2))
    assert await storage.snapshots.next_snapshot_no(pipeline.id) == 3

    listed = await storage.snapshots.list(pipeline.id)
    assert [s.snapshot_no for s in listed] == [1, 2]
    # distinct-identity chains: another pipeline starts its own numbering at 1
    other = await storage.pipelines.create(make_pipeline(name="other"))
    assert await storage.snapshots.next_snapshot_no(other.id) == 1


# --- Atomic identity+snapshot create / append (F2) ---------------------------
@pytest.mark.integration
async def test_create_identity_with_snapshot_is_atomic_and_sets_current(storage: Storage) -> None:
    """One transaction inserts the identity + snapshot #1 and points the head."""
    result = await storage.snapshots.create_identity_with_snapshot(
        make_pipeline(name="atomic"), make_snapshot(snapshot_no=1)
    )
    assert result.is_ok
    identity = result.unwrap()
    assert identity.id > 0
    assert identity.name == "atomic"
    assert identity.current_snapshot_id is not None
    head = await storage.snapshots.current(identity.id)
    assert head is not None
    assert head.id == identity.current_snapshot_id
    assert head.pipeline_id == identity.id
    assert head.snapshot_no == 1
    # exactly one identity + one snapshot persisted
    assert [p.name for p in await storage.pipelines.list()] == ["atomic"]
    assert len(await storage.snapshots.list(identity.id)) == 1


@pytest.mark.integration
async def test_create_identity_with_snapshot_duplicate_name_returns_err_atomically(storage: Storage) -> None:
    """A UNIQUE violation surfaces as Err with nothing partially persisted."""
    # The migration-only partial unique index on active names is absent from
    # metadata.create_all; add it so the real IntegrityError path is exercised.
    async with storage.engine.begin() as conn:
        await conn.execute(
            sql_text("CREATE UNIQUE INDEX uq_pipelines_name_active ON pipelines(name) WHERE soft_deleted_at IS NULL")
        )
    first = await storage.snapshots.create_identity_with_snapshot(
        make_pipeline(name="dup"), make_snapshot(snapshot_no=1)
    )
    assert first.is_ok
    second = await storage.snapshots.create_identity_with_snapshot(
        make_pipeline(name="dup"), make_snapshot(snapshot_no=1)
    )
    assert second.is_err
    # nothing partially persisted: one identity, one snapshot
    assert [p.name for p in await storage.pipelines.list()] == ["dup"]
    async with storage.session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(PipelineSnapshotRow))).scalar_one()
    assert total == 1


@pytest.mark.integration
async def test_create_and_set_current_appends_next_no_and_repoints(storage: Storage) -> None:
    """Append computes next_no in the same transaction and repoints the head."""
    created = await storage.snapshots.create_identity_with_snapshot(
        make_pipeline(name="append"), make_snapshot(snapshot_no=1)
    )
    identity = created.unwrap()
    appended = await storage.snapshots.create_and_set_current(identity.id, make_snapshot(snapshot_no=999))
    assert appended.is_ok
    snap = appended.unwrap()
    # next_no = MAX(snapshot_no) + 1 inside the append transaction, not the
    # caller's placeholder
    assert snap.snapshot_no == 2
    assert snap.pipeline_id == identity.id
    assert (await storage.snapshots.current(identity.id)).id == snap.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
    assert [s.snapshot_no for s in await storage.snapshots.list(identity.id)] == [1, 2]


@pytest.mark.integration
async def test_create_and_set_current_unknown_pipeline_returns_err_persists_nothing(storage: Storage) -> None:
    """An invalid pipeline_id fails cleanly before anything is written."""
    result = await storage.snapshots.create_and_set_current(99999, make_snapshot(snapshot_no=1))
    assert result.is_err
    async with storage.session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(PipelineSnapshotRow))).scalar_one()
    assert total == 0


# --- Vacancies ---------------------------------------------------------------
@pytest.mark.integration
async def test_upsert_inserts_then_updates_by_external_id(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    first = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="First"))
    assert first.id > 0

    updated = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="Second"))
    # same row was updated, not a duplicate
    assert updated.id == first.id

    rows = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert len(rows) == 1
    assert rows[0].title == "Second"


@pytest.mark.integration
async def test_upsert_allows_distinct_external_ids(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1"))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v2"))
    assert len(await storage.vacancies.list_by_pipeline(pipeline.id)) == 2


@pytest.mark.integration
async def test_upsert_never_touches_the_soft_delete_lifecycle(storage: Storage) -> None:
    """``soft_deleted_at`` is a manual-only lifecycle signal: upsert preserves it.

    The board's archived marker is gone; presence/absence in a listing never
    enters the lifecycle. A re-upsert of a soft-deleted row keeps it
    soft-deleted (the fetch stage decides insert-only vs refresh, and never
    touches soft-deleted rows anyway).
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    row = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1"))
    # Manual lifecycle: the user (or a future GUI action) soft-deletes the row.
    async with storage.engine.begin() as conn:
        await conn.execute(
            sql_text("UPDATE vacancies SET soft_deleted_at = '2026-08-05 10:00:00' WHERE id = :id"),
            {"id": row.id},
        )

    again = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="new"))
    assert again.id == row.id
    assert again.soft_deleted_at == "2026-08-05 10:00:00"  # never resurrected by upsert
    assert await storage.vacancies.list_by_pipeline(pipeline.id) == []


@pytest.mark.integration
async def test_get_by_external_id_finds_soft_deleted_row(storage: Storage) -> None:
    """The provenance lookup is state-agnostic: a soft-deleted row is found.

    The fetch stage looks a vacancy up to decide insert-only skip vs
    ``--refresh`` overwrite, and the row may well be soft-deleted at that point
    (fetch never touches soft-deleted rows, so the stage reads the state off
    this lookup).
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    row = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1"))
    async with storage.engine.begin() as conn:
        await conn.execute(
            sql_text("UPDATE vacancies SET soft_deleted_at = '2026-08-05 10:00:00' WHERE id = :id"),
            {"id": row.id},
        )

    row = await storage.vacancies.get_by_external_id(pipeline.id, ServiceVacancyId("v1"))
    assert row is not None
    assert row.soft_deleted_at is not None


@pytest.mark.integration
async def test_get_by_external_id(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1"))
    row = await storage.vacancies.get_by_external_id(pipeline.id, ServiceVacancyId("v1"))
    assert row is not None
    assert row.external_id == ServiceVacancyId("v1")
    assert await storage.vacancies.get_by_external_id(pipeline.id, ServiceVacancyId("nope")) is None


@pytest.mark.integration
async def test_manual_skip_maps_to_bool(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", manual_skip=True))
    row = await storage.vacancies.get_by_external_id(pipeline.id, ServiceVacancyId("v1"))
    assert row is not None
    assert row.manual_skip is True


@pytest.mark.integration
async def test_apply_status_round_trips(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", apply_status="applied"))
    row = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert row.apply_status == "applied"


@pytest.mark.integration
async def test_external_ids_scored_returns_only_scored(storage: Storage) -> None:
    """skip-already-processed on score: only rows carrying a score count as done."""
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="done", apply_status="applied"))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="scored", score=4))
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="pending", apply_status="pending")
    )

    processed = await storage.vacancies.external_ids_scored(pipeline.id)
    assert processed == {ServiceVacancyId("scored")}


@pytest.mark.integration
async def test_external_ids_with_cover_letter_stage_relative(storage: Storage) -> None:
    """skip-already-processed on generate: a score alone must NOT hide a row.

    The stage-relative predicate is exactly the audit's fix: scored-but-
    letterless vacancies remain generate candidates even with the flag on.
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="lettered", cover_letter="cl"))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="scored", score=4))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="done", apply_status="applied"))

    processed = await storage.vacancies.external_ids_with_cover_letter(pipeline.id)
    assert processed == {ServiceVacancyId("lettered")}


@pytest.mark.integration
async def test_external_ids_decided_apply_only(storage: Storage) -> None:
    """skip-already-processed on apply: only rows with an apply decision count.

    A scored-but-never-applied row is still an apply candidate; a sub-threshold
    row without an apply decision (the former score-stage 'skipped' stamp) is
    not 'decided'.
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="done", apply_status="applied"))
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="declined", apply_status="skipped")
    )
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="scored", score=2))

    processed = await storage.vacancies.external_ids_decided(pipeline.id)
    assert processed == {ServiceVacancyId("done"), ServiceVacancyId("declined")}


@pytest.mark.integration
async def test_list_by_pipeline_orders_by_id(storage: Storage) -> None:
    """The id-ordered list is the stable order used by the batch selectors."""
    pipeline = await storage.pipelines.create(make_pipeline())
    first = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="a"))
    second = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="b"))
    third = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="c"))

    rows = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert [r.id for r in rows] == [first.id, second.id, third.id]


@pytest.mark.integration
async def test_staleness_timestamps_round_trip_through_upsert_and_update(storage: Storage) -> None:
    """The four staleness stamps persist through upsert and update writes.

    The caller threads them (mirror/derived contract); a bump on update (e.g. a
    stage writing ``scored_at``) must not be lost.
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    inserted = await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            fetched_at="2026-08-05 09:00:00",
            scored_at="2026-08-05 09:30:00",
            generated_at="2026-08-05 10:00:00",
            user_edited_at=None,
        )
    )
    assert inserted.fetched_at == "2026-08-05 09:00:00"
    assert inserted.scored_at == "2026-08-05 09:30:00"
    assert inserted.generated_at == "2026-08-05 10:00:00"

    bumped = await storage.vacancies.update(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            id=inserted.id,
            fetched_at="2026-08-05 09:00:00",
            scored_at="2026-08-05 12:00:00",
            generated_at="2026-08-05 10:00:00",
            user_edited_at="2026-08-05 11:00:00",
        )
    )
    assert bumped.scored_at == "2026-08-05 12:00:00"
    assert bumped.user_edited_at == "2026-08-05 11:00:00"
    assert bumped.fetched_at == "2026-08-05 09:00:00"  # threaded, not clobbered


@pytest.mark.integration
async def test_vacancy_snapshot_provenance_survives_upsert(storage: Storage) -> None:
    """A vacancy scored under S1 then re-fetched under S2 keeps ONE row with
    ``scored_snapshot_id=S1`` and ``fetched_snapshot_id=S2`` (Fork 2: provenance
    is carried forward in place, never duplicated across snapshots)."""
    pipeline = await storage.pipelines.create(make_pipeline())
    s1 = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    s2 = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=2))

    # Fetched + scored under S1.
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            fetched_snapshot_id=s1.id,
            scored_snapshot_id=s1.id,
        )
    )
    # Re-fetched under S2; the caller carries the S1 score provenance forward.
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            fetched_snapshot_id=s2.id,
            scored_snapshot_id=s1.id,
        )
    )

    rows = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert len(rows) == 1
    assert rows[0].fetched_snapshot_id == s2.id
    assert rows[0].scored_snapshot_id == s1.id


# --- Audit -------------------------------------------------------------------
@pytest.mark.integration
async def test_audit_log_append_and_read_with_snapshot(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    snapshot = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    entry = await storage.audit_log.append(pipeline.id, "fetch", details='{"n": 3}', pipeline_snapshot_id=snapshot.id)
    assert entry.id > 0
    assert entry.action == "fetch"
    assert entry.pipeline_id == pipeline.id
    assert entry.pipeline_snapshot_id == snapshot.id


@pytest.mark.integration
async def test_audit_log_snapshot_defaults_to_none(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    entry = await storage.audit_log.append(pipeline.id, "fetch")
    assert entry.pipeline_snapshot_id is None


# --- Daily limits (auth-keyed) ----------------------------------------------
@pytest.mark.integration
async def test_daily_limit_get_absent_is_none(storage: Storage) -> None:
    assert await storage.daily_limits.get("mock", "user@ex.com", "2026-08-05") is None


@pytest.mark.integration
async def test_daily_limit_increment_creates_then_increments_atomically(storage: Storage) -> None:
    limit = await storage.daily_limits.increment("mock", "user@ex.com", "2026-08-05")
    assert limit.count == 1

    limit = await storage.daily_limits.increment("mock", "user@ex.com", "2026-08-05")
    limit = await storage.daily_limits.increment("mock", "user@ex.com", "2026-08-05")
    assert limit.count == 3
    assert limit.service == "mock"
    assert limit.login == "user@ex.com"


@pytest.mark.integration
async def test_daily_limit_unique_per_auth_and_date(storage: Storage) -> None:
    await storage.daily_limits.increment("mock", "a@ex.com", "2026-08-05")
    await storage.daily_limits.increment("mock", "a@ex.com", "2026-08-05")
    # different login / different date / different service are independent rows
    await storage.daily_limits.increment("mock", "b@ex.com", "2026-08-05")
    await storage.daily_limits.increment("mock", "a@ex.com", "2026-08-06")
    await storage.daily_limits.increment("hh", "a@ex.com", "2026-08-05")

    assert await storage.daily_limits.get("mock", "a@ex.com", "2026-08-05") is not None
    assert await storage.daily_limits.get("mock", "b@ex.com", "2026-08-05") is not None
    assert await storage.daily_limits.get("mock", "a@ex.com", "2026-08-06") is not None
    assert await storage.daily_limits.get("hh", "a@ex.com", "2026-08-05") is not None


@pytest.mark.integration
async def test_daily_limit_increment_duplicate_key_is_updated_not_duplicated(storage: Storage) -> None:
    """The UNIQUE(service, login, date) invariant holds across increments."""
    for _ in range(5):
        await storage.daily_limits.increment("mock", "a@ex.com", "2026-08-05")
    limit = await storage.daily_limits.get("mock", "a@ex.com", "2026-08-05")
    assert limit is not None
    assert limit.count == 5
    # only one row for the (service, login, date) key
    assert len(await storage.daily_limits.list()) == 1


# --- Timestamps are real SQLite values, not literal text (F1) ----------------
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _assert_real_timestamp(value: str) -> None:
    """Assert ``value`` is a real ``datetime('now')`` value, not the literal
    ``"(datetime('now'))"`` text the buggy ``_NOW`` used to store."""
    assert _TIMESTAMP_RE.match(value), f"expected a real timestamp, got: {value!r}"


@pytest.mark.integration
async def test_lifecycle_timestamps_are_real_sqlite_values(storage: Storage) -> None:
    """F1 regression: every *_at column stores a real 'YYYY-MM-DD HH:MM:SS'
    timestamp across pipeline create + repoint, snapshot create, vacancy upsert
    (update path) and daily_limits increment (update path)."""
    pipeline = await storage.pipelines.create(make_pipeline(name="ts"))
    _assert_real_timestamp(pipeline.created_at)
    _assert_real_timestamp(pipeline.updated_at)

    snap = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    _assert_real_timestamp(snap.created_at)

    assert await storage.pipelines.set_current_snapshot(pipeline.id, snap.id) is True
    repointed = await storage.pipelines.get(pipeline.id)
    assert repointed is not None
    _assert_real_timestamp(repointed.updated_at)

    # The upsert *update* path is where the buggy literal used to be stored.
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="first"))
    vacancy = await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="second"))
    _assert_real_timestamp(vacancy.created_at)
    _assert_real_timestamp(vacancy.updated_at)

    # The increment *update* path (second call) is where the literal used to be.
    await storage.daily_limits.increment("mock", "user@ex.com", "2026-08-05")
    limit = await storage.daily_limits.increment("mock", "user@ex.com", "2026-08-05")
    _assert_real_timestamp(limit.created_at)
    _assert_real_timestamp(limit.updated_at)

    # soft_delete stamps a real timestamp too (not the literal).
    assert await storage.pipelines.soft_delete(pipeline.id) is True
    deleted = await storage.pipelines.get(pipeline.id)
    assert deleted is not None
    assert deleted.soft_deleted_at is not None
    _assert_real_timestamp(deleted.soft_deleted_at)


# --- DTO purity --------------------------------------------------------------
@pytest.mark.integration
async def test_dtos_are_frozen(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    with pytest.raises(FrozenInstanceError):
        pipeline.name = "nope"  # type: ignore[misc]  # rationale: intentional frozen-ness probe


@pytest.mark.integration
async def test_vacancy_external_id_is_typed_service_vacancy_id(storage: Storage) -> None:
    """The DTO field is a :class:`ServiceVacancyId` (a ``NewType`` over ``str``).

    ``NewType`` is a *static* typing aid — at runtime it is a plain ``str`` — so
    the guarantee is enforced by ``basedpyright`` strict (the DTO declares
    ``external_id: ServiceVacancyId`` and the repository constructs it via
    ``ServiceVacancyId(...)``). Here we assert the value round-trips through the
    repository; the static guarantee is verified by type checking, not runtime.
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1"))
    row = await storage.vacancies.get_by_external_id(pipeline.id, ServiceVacancyId("v1"))
    assert row is not None
    assert row.external_id == ServiceVacancyId("v1")
    assert row.external_id == "v1"  # NewType is str at runtime; equality holds
    # The annotation on the DTO is the ServiceVacancyId type (verified statically).
    assert get_type_hints(VacancyRecord)["external_id"] is ServiceVacancyId


@pytest.mark.integration
async def test_snapshot_dto_is_frozen_and_typed(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    snap = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
    assert isinstance(snap, PipelineSnapshot)
    with pytest.raises(FrozenInstanceError):
        snap.snapshot_no = 99  # type: ignore[misc]  # rationale: intentional frozen-ness probe
