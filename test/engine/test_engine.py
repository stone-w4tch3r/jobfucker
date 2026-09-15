"""Phase 5B (task 5.1): engine controller coverage (post-decomposition).

Plain-pytest coverage of the callable core: batch-selector validation, position
windows (from/to/take), the decomposed per-stage methods (``fetch`` takes no
selector; ``score``/``generate_cv``/``apply`` take one), and the ``run``
composition (per-item failures carried in reports, not aborts).
"""

from __future__ import annotations

import pytest
from rusty_results.prelude import Err, Ok

from jobfucker.ai import ScoreError, ScoreResult
from jobfucker.clients.base import ClientDeps, SearchWindow
from jobfucker.clients.factory import Factory
from jobfucker.engine import BatchSelector, Engine
from jobfucker.stages.apply import ApplyFilters, ApplyReport
from jobfucker.stages.fetch import FetchReport
from jobfucker.stages.generate_cv import GenerateCvReport
from jobfucker.stages.score import ScoreReport
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline
from test.pipeline_helpers import (
    ScriptedAi,
    build_pipeline_config,
    create_pipeline,
    make_bulk_section,
    make_factory,
    snapshot_for,
)


def _engine(storage: Storage, factory: Factory, ai: ScriptedAi, snapshot_id: int) -> Engine:
    return Engine(
        storage=storage,
        factory=factory,
        config=build_pipeline_config(),
        ai=ai,
        snapshot_id=snapshot_id,
    )


async def _run_engine(storage: Storage, factory: Factory, ai: ScriptedAi, pipeline: Pipeline) -> Engine:
    """Build an engine stamped with the pipeline's head snapshot id."""
    return _engine(storage, factory, ai, (await snapshot_for(storage, pipeline)).id)


@pytest.mark.unit
def test_batch_selector_rejects_to_plus_take() -> None:
    """--to combined with --take is a validation error."""
    selector = BatchSelector(from_=0, to=5, take=3)
    result = selector.validate()
    assert result.is_err
    assert "Cannot combine --to with --take" in result.unwrap_err()


@pytest.mark.unit
def test_batch_selector_rejects_negative_and_reversed() -> None:
    """Negative --from / reversed window are validation errors."""
    assert BatchSelector(from_=-1).validate().is_err
    assert BatchSelector(from_=5, to=2).validate().is_err
    assert BatchSelector(take=0).validate().is_err


@pytest.mark.unit
def test_batch_selector_position_windows() -> None:
    """from/to/take map to the expected 0-based position sets."""
    assert BatchSelector().positions(5) == {0, 1, 2, 3, 4}
    assert BatchSelector(from_=1, to=3).positions(5) == {1, 2, 3}  # to inclusive
    assert BatchSelector(take=2).positions(5) == {0, 1}  # take starts at 0
    assert BatchSelector(from_=2, take=2).positions(5) == {2, 3}
    assert BatchSelector(from_=4).positions(5) == {4}  # clipped to total


@pytest.mark.integration
async def test_fetch_ignores_selector_and_refetches_all(storage: Storage, client_deps: ClientDeps) -> None:
    """fetch takes no batch selector and re-fetches every vacancy."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)
    result = await engine.fetch(pipeline)
    assert result.is_ok
    reports = result.unwrap()
    assert len(reports) == 1
    report = reports[0]
    assert isinstance(report, FetchReport)
    assert report.fetched == 3


@pytest.mark.integration
async def test_position_stage_surfaces_batch_validation_error(storage: Storage, client_deps: ClientDeps) -> None:
    """Position stages fail fast on a bad selector, before touching the client."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)

    score = await engine.score(pipeline, selector=BatchSelector(to=1, take=2))
    assert score.is_err
    assert "Cannot combine --to with --take" in score.unwrap_err()
    # No writes happened on the fail-fast path.
    assert await storage.vacancies.list_by_pipeline(pipeline.id) == []

    assert (await engine.generate_cv(pipeline, selector=BatchSelector(from_=-1))).is_err
    assert (await engine.apply(pipeline, selector=BatchSelector(take=0))).is_err


@pytest.mark.integration
async def test_run_composes_four_stages(storage: Storage, client_deps: ClientDeps) -> None:
    """The full run composition fetches, scores, generates, and applies."""
    pipeline = await create_pipeline(storage)
    snapshot = await snapshot_for(storage, pipeline)
    engine = _engine(storage, make_factory(client_deps), ScriptedAi(), snapshot.id)
    result = await engine.run(pipeline)
    assert result.is_ok
    reports = result.unwrap()
    assert isinstance(reports.score, ScoreReport)
    assert isinstance(reports.generate_cv, GenerateCvReport)
    assert isinstance(reports.apply, ApplyReport)
    assert reports.fetch[0].fetched == 3
    assert reports.score.scored == 3
    assert reports.generate_cv.generated == 3
    assert reports.apply.applied == 3

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert all(v.apply_status == "applied" for v in stored.values())
    # Every result row carries the identity + the snapshot whose config produced
    # each artifact (spec success criterion 3).
    for vacancy in stored.values():
        assert vacancy.fetched_snapshot_id == snapshot.id
        assert vacancy.scored_snapshot_id == snapshot.id
        assert vacancy.generated_snapshot_id == snapshot.id
        assert vacancy.applied_snapshot_id == snapshot.id

    # Each audit entry records the snapshot it ran against.
    audit_rows = await storage.audit_log.list()
    assert {entry.pipeline_snapshot_id for entry in audit_rows} == {snapshot.id}


@pytest.mark.integration
async def test_repeat_run_fetch_is_insert_only_and_stages_reprocess(storage: Storage, client_deps: ClientDeps) -> None:
    """A repeat run's fetch persists nothing new (insert-only mirror sync).

    The position stages re-run over the same rows by default; with
    ``skip_already_processed`` they protect the prior results.
    """
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)
    assert (await engine.run(pipeline)).is_ok

    # Second full run: fetch persists nothing (all rows already stored), and
    # the stages re-process the same rows.
    result = await engine.run(pipeline)
    assert result.is_ok
    reports = result.unwrap()
    assert reports.fetch[0].fetched == 0
    assert reports.score.scored == 3  # reprocessed (not skipped)

    # With skip_already_processed, the position stages protect prior results.
    skipped = await engine.run(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert skipped.is_ok
    assert skipped.unwrap().fetch[0].fetched == 0
    assert skipped.unwrap().score.scored == 0


@pytest.mark.integration
async def test_single_position_stage_runs_without_composing(storage: Storage, client_deps: ClientDeps) -> None:
    """Each stage can be run directly without composing the others."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)

    fetch = await engine.fetch(pipeline)
    assert fetch.is_ok
    assert fetch.unwrap()[0].fetched == 3

    score = await engine.score(pipeline)
    assert score.is_ok
    assert score.unwrap().scored == 3

    generate = await engine.generate_cv(pipeline)
    assert generate.is_ok
    assert generate.unwrap().generated == 3

    apply = await engine.apply(pipeline)
    assert apply.is_ok
    assert apply.unwrap().applied == 3


@pytest.mark.integration
async def test_position_stage_skip_already_processed(storage: Storage, client_deps: ClientDeps) -> None:
    """skip_already_processed on a position stage protects already-computed rows.

    Idempotency now lives at the position stages (no re-fetch wipes results), so
    an apply with ``skip_already_processed`` after a previous apply re-runs the
    stage but answers nothing new.
    """
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)
    assert (await engine.run(pipeline)).is_ok  # fetch + all three position stages

    # Re-apply WITHOUT re-fetching and WITH skip_already_processed: all three
    # vacancies already carry an apply result, so none is answered again.
    apply = await engine.apply(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert apply.is_ok
    assert apply.unwrap().applied == 0

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert all(v.apply_status == "applied" for v in stored.values())  # untouched


@pytest.mark.integration
async def test_generate_skip_already_processed_works_after_score_only(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """The core regression: ``generate --skip-already-processed`` on a scored pipeline.

    Stage-relative idempotency: a score is the score stage's result, so it
    must not hide a scored-but-letterless vacancy from ``generate``. Before
    the decoupling this produced ``generated=0`` forever (the audit's bug).
    """
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)

    assert (await engine.fetch(pipeline)).is_ok
    assert (await engine.score(pipeline)).is_ok

    generated = await engine.generate_cv(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert generated.is_ok
    assert generated.unwrap().generated == 3  # scored rows are still candidates


@pytest.mark.integration
async def test_apply_skip_already_processed_applies_scored_lettered(storage: Storage, client_deps: ClientDeps) -> None:
    """``apply --skip-already-processed`` still attempts scored+lettered rows.

    Stage-relative idempotency: only an apply decision counts as done for the
    apply stage. Previously the union predicate excluded every scored row, so
    a never-applied vacancy was hidden.
    """
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)

    assert (await engine.fetch(pipeline)).is_ok
    assert (await engine.score(pipeline)).is_ok
    assert (await engine.generate_cv(pipeline)).is_ok

    applied = await engine.apply(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert applied.is_ok
    assert applied.unwrap().applied == 3


@pytest.mark.integration
async def test_generate_skip_excludes_only_lettered_rows(storage: Storage, client_deps: ClientDeps) -> None:
    """A second ``generate --skip-already-processed`` pass has nothing left."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)
    assert (await engine.fetch(pipeline)).is_ok
    assert (await engine.score(pipeline)).is_ok

    first = await engine.generate_cv(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert first.is_ok
    assert first.unwrap().generated == 3

    second = await engine.generate_cv(pipeline, selector=BatchSelector(skip_already_processed=True))
    assert second.is_ok
    assert second.unwrap().generated == 0  # all rows now hold a letter


@pytest.mark.integration
async def test_run_per_item_ai_error_is_a_field_not_an_abort(storage: Storage, client_deps: ClientDeps) -> None:
    """A scoring failure on one vacancy is recorded, not a batch abort."""
    pipeline = await create_pipeline(storage)
    # mock-1 scores fine; mock-2 fails; mock-3 below-threshold → skipped.
    ai = ScriptedAi(
        scores=[
            Ok(ScoreResult(fit_score=5, comment="great")),
            Err(ScoreError(message="AI returned invalid JSON", raw="{}")),
            Ok(ScoreResult(fit_score=1, comment="weak")),
        ]
    )
    engine = await _run_engine(storage, make_factory(client_deps), ai, pipeline)
    result = await engine.run(pipeline)
    assert result.is_ok
    score_report = result.unwrap().score
    assert score_report.failed == 1
    assert score_report.sub_threshold == 1

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    # mock-2 recorded an error field; the batch did not abort.
    failed = [v for v in stored.values() if v.score_error is not None]
    assert len(failed) == 1
    failed_error = failed[0].score_error
    assert failed_error is not None
    assert "invalid JSON" in failed_error


# --- fetch slicing through the engine (client-side paging) --------------------
@pytest.mark.integration
async def test_fetch_accepts_fetch_params_and_slices_the_listing(storage: Storage, client_deps: ClientDeps) -> None:
    """``engine.fetch(pipeline, params=...)`` slices like the stage, via the factory."""
    pipeline = await create_pipeline(storage)
    factory = make_factory(client_deps, section=make_bulk_section(250))
    engine = await _run_engine(storage, factory, ScriptedAi(), pipeline)

    result = await engine.fetch(pipeline, params=SearchWindow(take=202))

    assert result.is_ok
    reports = result.unwrap()
    assert len(reports) == 1
    report = reports[0]
    assert report.fetched == 202
    assert report.pages == 3
    rows = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert len(rows) == 202
    assert [v.id for v in rows] == sorted(v.id for v in rows)  # stable id order


@pytest.mark.integration
async def test_fetch_fails_fast_on_invalid_fetch_params(storage: Storage, client_deps: ClientDeps) -> None:
    """A bad plan returns the validation error before any listing is touched."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)

    result = await engine.fetch(pipeline, params=SearchWindow(take=0))

    assert result.is_err
    assert result.unwrap_err() == "search 0: --take must be >= 1"
    assert await storage.vacancies.list_by_pipeline(pipeline.id) == []


@pytest.mark.integration
async def test_run_accepts_fetch_params_for_its_fetch_stage(storage: Storage, client_deps: ClientDeps) -> None:
    """``engine.run(pipeline, fetch_params=...)`` feeds the run's fetch, then processes the slice."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(
        storage, make_factory(client_deps, section=make_bulk_section(250)), ScriptedAi(), pipeline
    )

    result = await engine.run(pipeline, fetch_params=SearchWindow(take=202))

    assert result.is_ok
    reports = result.unwrap()
    assert reports.fetch[0].fetched == 202
    assert reports.fetch[0].requested == 202
    assert reports.score.scored == 202
    rows = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert len(rows) == 202
    assert [v.id for v in rows] == sorted(v.id for v in rows)  # stable id order


# --- forced-ids apply through the engine --------------------------------------
@pytest.mark.integration
async def test_apply_forced_ids_fail_fast_on_unknown_id(storage: Storage, client_deps: ClientDeps) -> None:
    """A forced-ids apply names the missing ids in an Err before any apply."""
    pipeline = await create_pipeline(storage)
    engine = await _run_engine(storage, make_factory(client_deps), ScriptedAi(), pipeline)
    assert (await engine.fetch(pipeline)).is_ok  # vacancies exist, none eligible

    result = await engine.apply(pipeline, filters=ApplyFilters(only_external_ids=frozenset({"mock-1", "ghost-9"})))
    assert result.is_err
    assert "not found in pipeline" in result.unwrap_err()
    assert "ghost-9" in result.unwrap_err()
    # The fail-fast happened before any application attempt.
    stored = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert all(v.apply_status is None for v in stored)
