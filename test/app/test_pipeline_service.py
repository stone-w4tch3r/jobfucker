"""Integration tests for :class:`PipelineService` (``app/pipeline_service.py``).

Uses the shared ``storage`` and ``client_deps`` fixtures; a real :class:`Factory`
built by :func:`test.pipeline_helpers.make_factory` registers the mock client,
so ``validate_cap`` / resolution exercise the real code paths.

Covers the pipeline-snapshots lifecycle: create → snapshot #1 + current head,
append on change (id stable, no soft-delete), no-op dedupe on identical update,
distinct identities on the same service+login, and resolve/snapshots/list.
"""

from __future__ import annotations

import pytest

from jobfucker.app.pipeline_service import PipelineService
from jobfucker.clients.base import ClientDeps
from jobfucker.config import HhTestSolvingConfig, LimitsConfig, PipelineConfig
from jobfucker.storage.db import Storage
from test.pipeline_helpers import build_pipeline_config, make_factory


@pytest.fixture
def config() -> PipelineConfig:
    """A validated mock pipeline config."""
    return build_pipeline_config()


@pytest.fixture
def svc(storage: Storage, client_deps: ClientDeps) -> PipelineService:
    """A :class:`PipelineService` over the shared storage + mock factory."""
    return PipelineService(storage, make_factory(client_deps))


@pytest.mark.integration
async def test_create_makes_snapshot_one_and_sets_current(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    result = await svc.create(config)
    assert result.is_ok
    m = result.unwrap()
    assert m.created is True and m.appended is False and m.unchanged is False
    assert m.pipeline.id >= 1
    assert m.pipeline.name == config.name
    assert m.snapshot.snapshot_no == 1
    assert m.pipeline.current_snapshot_id == m.snapshot.id
    assert (await storage.snapshots.current(m.pipeline.id)).id == m.snapshot.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
    assert len(await storage.pipelines.list()) == 1
    assert m.pipeline.soft_deleted_at is None


@pytest.mark.integration
async def test_new_snapshot_changed_appends_exactly_one_row_id_stable_no_soft_delete(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    created = (await svc.create(config)).unwrap()
    first_id = created.pipeline.id
    assert (await storage.pipelines.get(first_id)).soft_deleted_at is None  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call

    changed = config.model_copy(update={"limits": LimitsConfig(daily_apply_limit=99)})
    result = await svc.new_snapshot(first_id, changed)
    assert result.is_ok
    m = result.unwrap()
    assert m.created is False and m.appended is True and m.unchanged is False
    # id NEVER changes on update
    assert m.pipeline.id == first_id
    # exactly one new snapshot row, numbered 2, current repointed
    assert m.snapshot.snapshot_no == 2
    snaps = await storage.snapshots.list(first_id)
    assert [s.snapshot_no for s in snaps] == [1, 2]
    assert (await storage.snapshots.current(first_id)).id == m.snapshot.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
    # no soft-delete of the head
    stored = await storage.pipelines.get(first_id)
    assert stored is not None and stored.soft_deleted_at is None


@pytest.mark.integration
async def test_new_snapshot_identical_appends_zero_rows_unchanged(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    created = (await svc.create(config)).unwrap()
    first_id = created.pipeline.id

    result = await svc.new_snapshot(first_id, config)  # identical config → no-op
    assert result.is_ok
    m = result.unwrap()
    assert m.created is False and m.appended is False
    assert m.unchanged is True
    # zero new rows
    assert len(await storage.snapshots.list(first_id)) == 1
    assert (await storage.snapshots.current(first_id)).id == created.snapshot.id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call


@pytest.mark.integration
async def test_new_snapshot_changed_hh_test_solving_appends(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    """An edit that only adds/changes ``hh_test_solving`` is a real change, not a no-op.

    Regression: the dedupe field list must include the hh-test-solving content
    column, or ``jobfucker update`` silently drops the section.
    """
    created = (await svc.create(config)).unwrap()
    changed = config.model_copy(
        update={"hh_test_solving": HhTestSolvingConfig(enabled=True, test_prompt="Solve {{ test_formatted }}")}
    )

    m = (await svc.new_snapshot(created.pipeline.id, changed)).unwrap()

    assert m.created is False and m.appended is True and m.unchanged is False
    assert len(await storage.snapshots.list(created.pipeline.id)) == 2


@pytest.mark.integration
async def test_two_distinct_pipelines_share_service_and_login_independently(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    """SC2: same service+login, distinct names -> two identities, independent chains."""
    ai = config.model_copy(update={"name": "ai-engineer"})
    qa = config.model_copy(update={"name": "qa-search"})
    ra = (await svc.create(ai)).unwrap()
    rb = (await svc.create(qa)).unwrap()

    assert ra.pipeline.id != rb.pipeline.id
    # both target the same service client
    assert ra.snapshot.service == "mock"
    assert rb.snapshot.service == "mock"
    # independent chains, each at its own snapshot #1
    assert [s.snapshot_no for s in await storage.snapshots.list(ra.pipeline.id)] == [1]
    assert [s.snapshot_no for s in await storage.snapshots.list(rb.pipeline.id)] == [1]
    assert len(await storage.pipelines.list()) == 2


@pytest.mark.integration
async def test_resolve_returns_identity_and_current_snapshot(svc: PipelineService, config: PipelineConfig) -> None:
    created = (await svc.create(config)).unwrap()
    resolved = await svc.resolve(created.pipeline.id)
    assert resolved.is_ok
    identity, snapshot = resolved.unwrap()
    assert identity.id == created.pipeline.id
    assert identity.name == config.name
    assert snapshot.snapshot_no == 1


@pytest.mark.integration
async def test_snapshots_returns_ordered_history(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    created = (await svc.create(config)).unwrap()
    changed = config.model_copy(update={"limits": LimitsConfig(daily_apply_limit=99)})
    await svc.new_snapshot(created.pipeline.id, changed)

    result = await svc.snapshots(created.pipeline.id)
    assert result.is_ok
    assert [s.snapshot_no for s in result.unwrap()] == [1, 2]


@pytest.mark.integration
async def test_list_returns_identities_oldest_first(svc: PipelineService, config: PipelineConfig) -> None:
    a = (await svc.create(config.model_copy(update={"name": "a"}))).unwrap()
    b = (await svc.create(config.model_copy(update={"name": "b"}))).unwrap()
    rows = await svc.list()
    assert [row.id for row in rows] == [a.pipeline.id, b.pipeline.id]
    assert rows[0].name == "a"
    assert rows[1].name == "b"


async def test_resolve_unknown_id_returns_exact_cli_message(svc: PipelineService, config: PipelineConfig) -> None:
    await svc.create(config)
    result = await svc.resolve(999)
    assert result.is_err
    assert result.unwrap_err() == "Pipeline id 999 not found."


@pytest.mark.integration
async def test_resolve_empty_store_returns_exact_cli_message(svc: PipelineService) -> None:
    result = await svc.resolve(1)
    assert result.is_err
    assert result.unwrap_err() == "No stored pipelines. Run `jobfucker init` first."


@pytest.mark.integration
async def test_create_cap_violation_returns_err_and_persists_nothing(svc: PipelineService, storage: Storage) -> None:
    bad = build_pipeline_config(daily_apply_limit=201)  # mock per-auth cap is 200
    result = await svc.create(bad)
    assert result.is_err
    assert result.unwrap_err() == "daily_apply_limit (201) exceeds the 'mock' per-auth daily cap (200)"
    assert await storage.pipelines.list() == []


@pytest.mark.integration
async def test_new_snapshot_on_soft_deleted_pipeline_returns_err_and_persists_nothing(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    """F9: appending to a soft-deleted identity fails cleanly (Err), no rows."""
    created = (await svc.create(config)).unwrap()
    pipeline_id = created.pipeline.id
    assert await storage.pipelines.soft_delete(pipeline_id) is True

    changed = config.model_copy(update={"limits": LimitsConfig(daily_apply_limit=99)})
    result = await svc.new_snapshot(pipeline_id, changed)
    assert result.is_err
    assert result.unwrap_err() == f"Pipeline id {pipeline_id} not found."
    # nothing was appended for the deleted identity
    assert len(await storage.snapshots.list(pipeline_id)) == 1


@pytest.mark.integration
async def test_snapshots_on_soft_deleted_pipeline_returns_err(
    svc: PipelineService, storage: Storage, config: PipelineConfig
) -> None:
    """F9: the snapshot-history view also rejects a soft-deleted identity."""
    created = (await svc.create(config)).unwrap()
    pipeline_id = created.pipeline.id
    assert await storage.pipelines.soft_delete(pipeline_id) is True

    result = await svc.snapshots(pipeline_id)
    assert result.is_err
    assert result.unwrap_err() == f"Pipeline id {pipeline_id} not found."
