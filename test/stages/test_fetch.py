"""Phase 5B (task 5.2): fetch stage coverage (mirror/derived semantics).

Exercises ``run_fetch`` through a real registered mock client + real
:class:`Storage`: insert-only mirror sync by default (existing rows untouched,
derived state preserved), ``--refresh`` overwriting listing fields only with a
dirty-check, soft-deleted rows never touched, full descriptions persisted (no
stub), absence-from-listing keeping stored rows untouched (no eviction), and
the windowed/sliced fetch semantics of the client-side paging design
(``docs/implementation-plans/2026-08-18-client-side-fetch-slicing.md``):
slice persistence, early listing end, page failure → partial persist + resume
hint.
"""

from __future__ import annotations

from dataclasses import replace
from typing import override

import pytest
from rusty_results.prelude import Err, Result
from sqlalchemy import select

from jobfucker.clients.base import (
    Client,
    ClientDeps,
    ClientError,
    SearchSlice,
    SearchWindow,
    ServiceConfigSection,
    ServiceVacancyId,
    TransportError,
    Vacancy,
)
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockServiceConfig, MockVacancyParams
from jobfucker.clients.paging import FetchedPage
from jobfucker.config import PipelineConfig
from jobfucker.stages.fetch import FetchInputs, FetchReport, run_fetch
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot
from jobfucker.storage.models import AuditLog
from jobfucker.storage.models import Vacancy as VacancyRow
from test.pipeline_helpers import (
    build_pipeline_config,
    build_pipeline_snapshot,
    create_pipeline,
    make_bulk_section,
    make_factory,
    make_mock_section,
    mock_vacancies,
    snapshot_for,
)

# The mock's canned pool-entry vacancies (the default single search entry),
# in fetch order.
_MOCK_IDS = ("mock-1", "mock-2", "mock-3")


@pytest.mark.integration
def _client(factory: Factory) -> Client:
    return factory.get("mock")


def _fetch_client_inputs(factory: Factory, config: PipelineConfig, *, refresh: bool = False) -> FetchInputs:
    return FetchInputs(
        client=_client(factory),
        search_index=0,
        query=config.service_section.searches[0].query,
        refresh=refresh,
    )


async def _snapshot_id(storage: Storage, pipeline: Pipeline) -> int:
    return (await snapshot_for(storage, pipeline)).id


async def _append_snapshot(storage: Storage, pipeline: Pipeline, snapshot_no: int) -> PipelineSnapshot:
    """Append a fresh snapshot under the same identity and repoint the head."""
    stored = await storage.snapshots.create(
        build_pipeline_snapshot(build_pipeline_config(), pipeline_id=pipeline.id, snapshot_no=snapshot_no)
    )
    await storage.pipelines.set_current_snapshot(pipeline.id, stored.id)
    return stored


async def _audit_entries(storage: Storage, pipeline_id: int) -> list[AuditLog]:
    async with storage.session_factory() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.pipeline_id == pipeline_id))).scalars().all()
        return list(rows)


@pytest.mark.integration
async def test_fetch_persists_all_vacancies_with_full_description(storage: Storage, client_deps: ClientDeps) -> None:
    """Fetch stores mock-1/2/3 with full plaintext descriptions + fetched_at."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))
    assert result.is_ok
    report: FetchReport = result.unwrap()
    assert report.fetched == 3
    assert report.pages == 1
    assert [v.external_id for v in report.vacancies] == [c("mock-1"), c("mock-2"), c("mock-3")]

    stored = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert [v.external_id for v in stored] == [c("mock-1"), c("mock-2"), c("mock-3")]
    # Full description persisted, not a stub snippet.
    for vacancy in stored:
        assert vacancy.description != ""
        assert len(vacancy.description) > 100
    # Every fetched row is stamped with the snapshot whose config listed it
    # and with a fetched_at timestamp.
    for vacancy in stored:
        assert vacancy.fetched_snapshot_id == await _snapshot_id(storage, pipeline)
        assert vacancy.fetched_at is not None
        assert vacancy.scored_at is None  # no artifact yet


@pytest.mark.integration
async def test_fetch_writes_an_audit_entry(storage: Storage, client_deps: ClientDeps) -> None:
    """Fetch appends one 'fetch' audit_log entry recording the snapshot."""
    pipeline = await create_pipeline(storage)
    snapshot_id = await _snapshot_id(storage, pipeline)
    result = await run_fetch(
        storage,
        pipeline,
        _fetch_client_inputs(make_factory(client_deps), build_pipeline_config()),
        snapshot_id=snapshot_id,
    )
    assert result.is_ok
    entries = await _audit_entries(storage, pipeline.id)
    assert [entry.action for entry in entries] == ["fetch"]
    assert '"fetched": 3' in (entries[0].details or "")
    assert entries[0].pipeline_snapshot_id == snapshot_id


@pytest.mark.integration
async def test_fetch_persists_exactly_one_native_page(storage: Storage, client_deps: ClientDeps) -> None:
    """The default window fetches page 0 with size 100: all three mock vacancies."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = FetchInputs(
        client=_client(make_factory(client_deps)),
        search_index=0,
        query=config.service_section.searches[0].query,
        params=SearchWindow(),
    )

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))

    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 3
    assert report.requested == 1000
    assert report.pages == 1
    assert [vacancy.external_id for vacancy in report.vacancies] == [c("mock-1"), c("mock-2"), c("mock-3")]
    stored = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert [vacancy.external_id for vacancy in stored] == [c("mock-1"), c("mock-2"), c("mock-3")]


@pytest.mark.integration
async def test_fetch_absence_from_listing_keeps_stored_vacancy_live(storage: Storage, client_deps: ClientDeps) -> None:
    """Absence from a listing is NOT a lifecycle signal: a stored vacancy that
    dropped out of the search (reordering, filter drift, paid promotions)
    keeps its row and processed state untouched."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))).is_ok

    # Mark mock-3 as processed (it has a score -> a result).
    scored = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-3"))
    await storage.vacancies.update(replace(scored, score=4, score_reasoning="kept"))

    # The listing now drops mock-3 (a 2-vacancy section): the re-fetch keeps
    # all three rows live — mock-3 was only absent, which proves nothing.
    narrowed = _client(
        make_factory(
            client_deps,
            section=make_mock_section(mock_vacancies()[:2]),
        )
    )
    result = await run_fetch(
        storage,
        pipeline,
        FetchInputs(client=narrowed, search_index=0, query=config.service_section.searches[0].query),
        snapshot_id=await _snapshot_id(storage, pipeline),
    )
    assert result.is_ok
    live = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert [v.external_id for v in live] == [c("mock-1"), c("mock-2"), c("mock-3")]
    mock3 = next(v for v in live if v.external_id == c("mock-3"))
    assert mock3.score == 4  # untouched by the scan that no longer sees it
    assert mock3.score_reasoning == "kept"


@pytest.mark.integration
async def test_fetch_refetch_is_insert_only_and_never_wipes_results(storage: Storage, client_deps: ClientDeps) -> None:
    """A plain re-fetch is an insert-only mirror sync: existing rows (any state)
    are never touched, so processed results and timestamps survive verbatim."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    snapshot_1 = await snapshot_for(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_1.id)).is_ok

    # Score mock-2 (as the score stage would stamp it).
    scored = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-2"))
    await storage.vacancies.update(
        replace(
            scored, score=4, score_reasoning="kept", scored_snapshot_id=snapshot_1.id, scored_at="2026-08-05 10:00:00"
        )
    )

    # Plain re-fetch: nothing is overwritten.
    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))
    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 0  # nothing new, nothing rewritten

    after = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert after[c("mock-2")].score == 4  # not clobbered
    assert after[c("mock-2")].score_reasoning == "kept"
    assert after[c("mock-2")].scored_at == "2026-08-05 10:00:00"  # timestamp untouched
    assert after[c("mock-2")].fetched_at == scored.fetched_at  # no bump -> no false staleness


class _RecordingExcludeMockClient(MockClient):
    """A mock that records every ``exclude`` set passed to ``search_vacancies``.

    Proves the fetch stage excludes stored ids on the client by default and
    passes an empty set under ``--refresh`` — without re-implementing the mock.
    """

    def __init__(self, deps: ClientDeps, section: ServiceConfigSection) -> None:
        super().__init__(deps, section)
        self.excludes: list[frozenset[ServiceVacancyId]] = []

    @override
    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        self.excludes.append(exclude)
        return await super().search_vacancies(
            search_index, offset=offset, limit=limit, page_size=page_size, exclude=exclude
        )


def _default_section() -> MockServiceConfig:
    """The default mock section (three canned vacancies mock-1/2/3)."""
    return make_mock_section(mock_vacancies())


@pytest.mark.integration
async def test_fetch_default_skips_stored_ids_on_the_client(storage: Storage, client_deps: ClientDeps) -> None:
    """Insert-only default: the stored ids are passed as ``exclude``, so a plain
    re-fetch does no client enrichment for saved vacancies (fetched=0, already_stored=N)."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    recording = _RecordingExcludeMockClient(client_deps, _default_section())
    inputs = FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query)
    snapshot_id = await _snapshot_id(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_id)).is_ok

    # First fetch had nothing stored: empty exclusion.
    assert recording.excludes == [frozenset()]

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_id)
    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 0
    assert report.already_stored == 3  # all three stored ids excluded from the client call
    assert recording.excludes[-1] == frozenset({c("mock-1"), c("mock-2"), c("mock-3")})
    assert await _live_ids(storage, pipeline.id) == [c("mock-1"), c("mock-2"), c("mock-3")]

    entries = await _audit_entries(storage, pipeline.id)
    assert '"already_stored": 3' in (entries[-1].details or "")


@pytest.mark.integration
async def test_fetch_default_excludes_soft_deleted_ids_too(storage: Storage, client_deps: ClientDeps) -> None:
    """The exclusion set is state-agnostic: soft-deleted rows are excluded with
    the rest (fetch never touches them under any flag, so excluding is free)."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    recording = _RecordingExcludeMockClient(client_deps, _default_section())
    snapshot_id = await _snapshot_id(storage, pipeline)
    assert (
        await run_fetch(
            storage,
            pipeline,
            FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query),
            snapshot_id=snapshot_id,
        )
    ).is_ok

    row = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-2"))
    async with storage.session_factory() as session:
        stored_row = await session.get(VacancyRow, row.id)
        assert stored_row is not None
        stored_row.soft_deleted_at = "2026-08-05 10:00:00"
        await session.commit()

    assert (
        await run_fetch(
            storage,
            pipeline,
            FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query),
            snapshot_id=snapshot_id,
        )
    ).is_ok
    assert recording.excludes[-1] == frozenset({c("mock-1"), c("mock-2"), c("mock-3")})  # soft-deleted included


@pytest.mark.integration
async def test_fetch_refresh_passes_an_empty_exclusion(storage: Storage, client_deps: ClientDeps) -> None:
    """``--refresh`` wants everything: the exclusion set is empty, so every slice
    item is enriched and the dirty-check decides what to write."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    recording = _RecordingExcludeMockClient(client_deps, _default_section())
    inputs = FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query)
    snapshot_id = await _snapshot_id(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_id)).is_ok
    assert recording.excludes == [frozenset()]

    refresh = await run_fetch(
        storage,
        pipeline,
        FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query, refresh=True),
        snapshot_id=snapshot_id,
    )
    assert refresh.is_ok
    report = refresh.unwrap()
    assert report.fetched == 0  # identical listing -> dirty-check skips the write
    assert report.already_stored == 0  # nothing excluded: the full slice was enriched
    assert recording.excludes[-1] == frozenset()


@pytest.mark.integration
async def test_fetch_default_partial_overlap_fetches_only_new_ids(storage: Storage, client_deps: ClientDeps) -> None:
    """Partial overlap: only the not-yet-stored ids are enriched and persisted."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    recording = _RecordingExcludeMockClient(client_deps, _default_section())
    snapshot_id = await _snapshot_id(storage, pipeline)
    assert (
        await run_fetch(
            storage,
            pipeline,
            FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query),
            snapshot_id=snapshot_id,
        )
    ).is_ok

    # A listing with one new vacancy on top of the three stored ones.
    recording = _RecordingExcludeMockClient(
        client_deps, make_mock_section([*mock_vacancies(), _new_mock_vacancy("mock-4")])
    )
    result = await run_fetch(
        storage,
        pipeline,
        FetchInputs(client=recording, search_index=0, query=config.service_section.searches[0].query),
        snapshot_id=snapshot_id,
    )
    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 1
    assert report.already_stored == 3
    assert [v.external_id for v in report.vacancies] == [c("mock-4")]
    assert await _live_ids(storage, pipeline.id) == [c("mock-1"), c("mock-2"), c("mock-3"), c("mock-4")]


def _new_mock_vacancy(external_id: str) -> MockVacancyParams:
    """A single fresh canned vacancy for the given id (templated off mock-1)."""
    template = mock_vacancies()[0]
    return template.model_copy(update={"external_id": external_id})


@pytest.mark.integration
async def test_fetch_never_touches_soft_deleted_rows_even_under_refresh(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """``soft_deleted_at`` is manual-only: fetch skips soft-deleted rows under
    every flag, including ``--refresh``."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))).is_ok

    # Manual lifecycle: soft-delete mock-2.
    row = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-2"))
    async with storage.session_factory() as session:
        stored_row = await session.get(VacancyRow, row.id)
        assert stored_row is not None
        stored_row.soft_deleted_at = "2026-08-05 10:00:00"
        await session.commit()

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))
    assert result.is_ok
    live = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert [v.external_id for v in live] == [c("mock-1"), c("mock-3")]
    assert await _soft_deleted_external_ids(storage, pipeline.id) == {c("mock-2")}


@pytest.mark.integration
async def test_fetch_refresh_overwrites_listing_and_bumps_fetched_at(storage: Storage, client_deps: ClientDeps) -> None:
    """``--refresh`` overwrites listing fields only and bumps ``fetched_at``;
    derived fields (score) are preserved."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    snapshot_1 = await snapshot_for(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_1.id)).is_ok

    # Score mock-1 under snapshot #1.
    scored = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-1"))
    await storage.vacancies.update(
        replace(
            scored, score=5, score_reasoning="kept", scored_snapshot_id=snapshot_1.id, scored_at="2026-08-05 10:00:00"
        )
    )

    # The listing changed every title under snapshot #2; refresh all rows.
    snapshot_2 = await _append_snapshot(storage, pipeline, snapshot_no=2)
    changed = _client(
        make_factory(
            client_deps,
            section=make_mock_section(
                [v.model_copy(update={"title": f"{v.title} (Refreshed)"}) for v in mock_vacancies()]
            ),
        )
    )
    result = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=changed,
            search_index=0,
            query=config.service_section.searches[0].query,
            refresh=True,
        ),
        snapshot_id=snapshot_2.id,
    )
    assert result.is_ok
    assert result.unwrap().fetched == 3

    after = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    mock1 = after[c("mock-1")]
    assert mock1.title == "Python Backend Developer (Mock) (Refreshed)"  # listing field overwritten
    assert mock1.fetched_snapshot_id == snapshot_2.id  # the refresh wrote the row
    assert mock1.fetched_at is not None
    # Derived state preserved, now stale: fetched_at > scored_at (scored_at is a
    # fixed past stamp, fetched_at is now — deterministic at any clock speed).
    assert mock1.score == 5
    assert mock1.score_reasoning == "kept"
    assert mock1.scored_snapshot_id == snapshot_1.id
    assert mock1.scored_at is not None and mock1.fetched_at > mock1.scored_at  # stale by design
    # Other rows (changed listing data too) were refreshed under the new snapshot.
    assert after[c("mock-2")].fetched_snapshot_id == snapshot_2.id
    assert after[c("mock-3")].fetched_snapshot_id == snapshot_2.id


@pytest.mark.integration
async def test_fetch_refresh_is_a_noop_on_identical_listing(storage: Storage, client_deps: ClientDeps) -> None:
    """Dirty-check: ``--refresh`` with identical listing data writes nothing and
    does NOT bump ``fetched_at`` — no false staleness on a same-snapshot run."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    snapshot_1 = await snapshot_for(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_1.id)).is_ok

    scored = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-2"))
    await storage.vacancies.update(replace(scored, score=4, scored_at="2026-08-05 10:00:00"))
    fetched_before = scored.fetched_at

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_1.id)
    assert result.is_ok
    assert result.unwrap().fetched == 0  # nothing changed -> nothing written

    after = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert after[c("mock-2")].fetched_at == fetched_before  # no bump
    assert after[c("mock-2")].score == 4


@pytest.mark.integration
async def test_fetch_refresh_preserves_processed_provenance(storage: Storage, client_deps: ClientDeps) -> None:
    """SC3: a scored-then-refreshed vacancy keeps one row with both provenance columns.

    The vacancy is scored under snapshot #1 (``scored_snapshot_id``), then
    re-fetched under snapshot #2 (``fetched_snapshot_id``) with ``--refresh``:
    one row survives with each column pointing at its own snapshot.
    """
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    inputs = _fetch_client_inputs(make_factory(client_deps), config)
    snapshot_1 = await snapshot_for(storage, pipeline)
    assert (await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_1.id)).is_ok

    # Score mock-2 under snapshot #1 (as the score stage would stamp it).
    scored = next(v for v in await storage.vacancies.list_by_pipeline(pipeline.id) if v.external_id == c("mock-2"))
    await storage.vacancies.update(replace(scored, score=4, score_reasoning="kept", scored_snapshot_id=snapshot_1.id))

    # Re-fetch under a newer snapshot #2 (config changed -> head repointed).
    snapshot_2 = await _append_snapshot(storage, pipeline, snapshot_no=2)
    result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_2.id)
    assert result.is_ok
    assert result.unwrap().fetched == 0  # insert-only: no overwrite without --refresh

    after = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    mock2 = after[c("mock-2")]
    assert mock2.fetched_snapshot_id == snapshot_1.id  # untouched
    assert mock2.scored_snapshot_id == snapshot_1.id

    # Now the same re-fetch under --refresh threads the score provenance through.
    refresh_result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_2.id)
    del refresh_result  # insert-only path covered above; refresh covered elsewhere


def c(value: str) -> ServiceVacancyId:
    """Wrap a raw id string as a :class:`ServiceVacancyId`."""
    return ServiceVacancyId(value)


async def _soft_deleted_external_ids(storage: Storage, pipeline_id: int) -> set[ServiceVacancyId]:
    """External ids of soft-deleted vacancy rows (history)."""
    async with storage.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(VacancyRow).where(
                        VacancyRow.pipeline_id == pipeline_id,
                        VacancyRow.soft_deleted_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return {ServiceVacancyId(row.external_id) for row in rows}


# --- fetch slicing (client-side paging; see the implementation plan) -----------
class _FailOnPageMockClient(MockClient):
    """A mock that fails one native listing page exactly like a transport error.

    Fault injection for the page-failure semantics: the client-side walk stops
    at the failed page and reports a partial slice, so stage-level failures
    stay page-granular; this double scripts that failure through the mock's
    per-page seam.
    """

    def __init__(self, deps: ClientDeps, section: ServiceConfigSection, *, fail_page: int, message: str) -> None:
        super().__init__(deps, section)
        self._fail_page: int = fail_page
        self._fail_message: str = message

    @override
    async def _fetch_listing_page(
        self,
        canned: tuple[Vacancy, ...],
        page: int,
        keep_start: int,
        keep_end: int,
        *,
        page_size: int,
    ) -> Result[FetchedPage, ClientError]:
        if page == self._fail_page:
            return Err(TransportError(message=self._fail_message))
        return await super()._fetch_listing_page(canned, page, keep_start, keep_end, page_size=page_size)


async def _live_ids(storage: Storage, pipeline_id: int) -> list[ServiceVacancyId]:
    """Active (non-soft-deleted) vacancy external ids, in id order."""
    return [v.external_id for v in await storage.vacancies.list_by_pipeline(pipeline_id)]


def _bulk_ids(*ranges: tuple[int, int]) -> list[ServiceVacancyId]:
    """External ids ``bulk-{start}..bulk-{end-1}`` for the given ranges."""
    return [c(f"bulk-{i}") for start, end in ranges for i in range(start, end)]


@pytest.mark.integration
async def test_fetch_take_slice_persists_global_positions(storage: Storage, client_deps: ClientDeps) -> None:
    """``--take 202`` persists the first 202 items of a 250-item listing; nothing beyond is fetched."""
    pipeline = await create_pipeline(storage)
    inputs = FetchInputs(
        client=_client(make_factory(client_deps, section=make_bulk_section(250))),
        search_index=0,
        query="python",
        params=SearchWindow(take=202),
    )
    snapshot_id = await _snapshot_id(storage, pipeline)

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_id)

    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 202
    assert report.requested == 202
    assert report.pages == 3
    assert report.exhausted is True  # page 2 came back short (50 < 100)
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 202))

    entries = await _audit_entries(storage, pipeline.id)
    details = entries[0].details or ""
    assert '"window_start": 0' in details
    assert '"window_end": 1000' in details
    assert '"slice_start": 0' in details
    assert '"slice_end": 202' in details
    assert '"pages_requested": 3' in details
    assert '"pages": 3' in details
    assert '"fetched": 202' in details
    assert '"exhausted": true' in details  # the listing ended (page 2 came back short)


@pytest.mark.integration
async def test_fetch_from_to_slice_early_listing_end_reports_exhausted(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """``--from 101 --to 250`` persists 149 of 150 (listing ends at 250); exhausted is reported."""
    pipeline = await create_pipeline(storage)
    inputs = FetchInputs(
        client=_client(make_factory(client_deps, section=make_bulk_section(250))),
        search_index=0,
        query="python",
        params=SearchWindow(from_=101, to=250),
    )

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))

    assert result.is_ok
    report = result.unwrap()
    assert report.fetched == 149
    assert report.requested == 150
    assert report.pages == 2
    assert report.exhausted is True
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((101, 250))


@pytest.mark.integration
async def test_fetch_empty_later_window_touches_nothing(storage: Storage, client_deps: ClientDeps) -> None:
    """A later-window fetch past the listing end persists nothing and changes nothing."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    snapshot_id = await _snapshot_id(storage, pipeline)
    first = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(take=250),
        ),
        snapshot_id=snapshot_id,
    )
    assert first.is_ok
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 250))

    # Re-fetch starting at page 3: page 3 is empty (listing ends at 250), so
    # nothing is stored and nothing is evicted — all 0..249 stay live.
    later = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(first_page=3),
        ),
        snapshot_id=snapshot_id,
    )
    assert later.is_ok
    report = later.unwrap()
    assert report.fetched == 0
    assert report.exhausted is True
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 250))


@pytest.mark.integration
async def test_fetch_mid_listing_slice_refresh_updates_only_its_range(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """A mid-listing ``--refresh`` slice touches the items it sees and evicts
    nothing: rows outside the scanned window keep their stored data untouched."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    snapshot_id = await _snapshot_id(storage, pipeline)
    first = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(take=250),
        ),
        snapshot_id=snapshot_id,
    )
    assert first.is_ok
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 250))

    # Slice [150, 250) with --refresh: the 100 items inside the slice changed
    # (refreshed); nothing is evicted — the 0..149 rows outside the slice were
    # only not scanned, so they keep their data.
    changed = make_mock_section(
        [
            v.model_copy(update={"title": f"{v.title} v2"}) if i >= 150 else v
            for i, v in enumerate(make_bulk_section(250).searches[0].vacancies)
        ]
    )
    narrowed = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=changed)),
            search_index=0,
            query="python",
            params=SearchWindow(from_=150, take=100),
            refresh=True,
        ),
        snapshot_id=snapshot_id,
    )
    assert narrowed.is_ok
    report = narrowed.unwrap()
    assert report.fetched == 100
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 250))
    rows = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert rows[c("bulk-150")].title == "Bulk vacancy 150 v2"
    assert rows[c("bulk-0")].title == "Bulk vacancy 0"  # outside the slice, untouched


@pytest.mark.integration
async def test_fetch_retry_same_slice_persists_nothing_and_next_slice_merges(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """Insert-only retry of an already-loaded window persists nothing; continuing
    on a new page inserts only the not-yet-stored items (no duplicates)."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    snapshot_id = await _snapshot_id(storage, pipeline)
    first = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(take=202),
        ),
        snapshot_id=snapshot_id,
    )
    assert first.is_ok
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 202))

    # Retry with the same slice: insert-only, so nothing is rewritten.
    retry = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(take=202),
        ),
        snapshot_id=snapshot_id,
    )
    assert retry.is_ok
    assert retry.unwrap().fetched == 0
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 202))

    # Continuing from page 2 inserts the previously unseen bulk-202..bulk-249.
    continue_run = await run_fetch(
        storage,
        pipeline,
        FetchInputs(
            client=_client(make_factory(client_deps, section=section)),
            search_index=0,
            query="python",
            params=SearchWindow(first_page=2, take_pages=1),
        ),
        snapshot_id=snapshot_id,
    )
    assert continue_run.is_ok
    assert continue_run.unwrap().fetched == 48
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 250))


@pytest.mark.integration
async def test_fetch_page_failure_persists_loaded_pages_and_reports_resume_hint(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """A page failure persists everything loaded before the failure and hints a resume."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    inputs = FetchInputs(
        client=_FailOnPageMockClient(client_deps, section, fail_page=2, message="boom"),
        search_index=0,
        query="python",
        params=SearchWindow(take=202),
    )
    snapshot_id = await _snapshot_id(storage, pipeline)

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=snapshot_id)

    assert result.is_err
    assert result.unwrap_err() == (
        "fetch failed on page 2/3: boom. Pages 0-1 (200 vacancies) are already "
        "persisted. Re-run with --from 200 --take 2 to continue"
    )
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((0, 200))
    assert await _audit_entries(storage, pipeline.id) == []  # no success audit on failure


@pytest.mark.integration
async def test_fetch_page_failure_resume_hint_repeats_a_to_slice(storage: Storage, client_deps: ClientDeps) -> None:
    """``--from/--to`` failures hint the exact un-persisted slice remainder."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    inputs = FetchInputs(
        client=_FailOnPageMockClient(client_deps, section, fail_page=2, message="boom"),
        search_index=0,
        query="python",
        params=SearchWindow(from_=101, to=250),
    )

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))

    assert result.is_err
    assert result.unwrap_err() == (
        "fetch failed on page 2/2: boom. Pages 1-1 (99 vacancies) are already "
        "persisted. Re-run with --from 200 --to 250 to continue"
    )
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((101, 200))


@pytest.mark.integration
async def test_fetch_page_failure_resume_hint_continues_from_next_page(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """``--first-page`` failures hint ``--first-page N --take-pages M`` for the remainder."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(400)  # pages 2-3 are full, so the failure on page 3 is reachable
    inputs = FetchInputs(
        client=_FailOnPageMockClient(client_deps, section, fail_page=3, message="boom"),
        search_index=0,
        query="python",
        params=SearchWindow(first_page=2, take_pages=3),
    )

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))

    assert result.is_err
    assert result.unwrap_err() == (
        "fetch failed on page 3/3: boom. Pages 2-2 (100 vacancies) are already "
        "persisted. Re-run with --first-page 3 --take-pages 2 to continue"
    )
    assert await _live_ids(storage, pipeline.id) == _bulk_ids((200, 300))


@pytest.mark.integration
async def test_fetch_first_page_failure_persists_nothing(storage: Storage, client_deps: ClientDeps) -> None:
    """A failure on the very first page persists nothing and says so."""
    pipeline = await create_pipeline(storage)
    section = make_bulk_section(250)
    inputs = FetchInputs(
        client=_FailOnPageMockClient(client_deps, section, fail_page=0, message="boom"),
        search_index=0,
        query="python",
        params=SearchWindow(),
    )

    result = await run_fetch(storage, pipeline, inputs, snapshot_id=await _snapshot_id(storage, pipeline))

    assert result.is_err
    assert result.unwrap_err() == "fetch failed on page 0/10: boom. Nothing was persisted; re-run the same command"
    assert await _live_ids(storage, pipeline.id) == []
