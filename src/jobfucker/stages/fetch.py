"""Fetch stage — windowed/sliced listing fetch (client-side paging).

Resolves a fetch plan (:class:`FetchPlan`) for one search-pool entry (its
``search_index`` + ``query``), then makes **one slice-level client call**: the
client (through the shared driver ``jobfucker.clients.paging``) walks only the
native pages overlapping the slice and enriches only the items inside it — no
wasted API calls, no wasted loading. The engine walks the pipeline's pool by
calling this stage once per entry.

Persistence follows the mirror/derived model:

- **insert-only by default** — a new ``external_id`` is inserted; an existing
  row (any state, including soft-deleted) is **never touched**. There is no
  freshness wipe: fetched rows keep their scoring / cover-letter / apply
  artifacts exactly as they were. Already-stored ids are additionally excluded
  **on the client** (per-call ``exclude``), so a default re-fetch does no
  enrichment work for saved vacancies (HH: zero detail requests on an empty
  diff).
- **``--refresh`` opt-in** — overwrites **listing fields only** (title, url,
  company, salary, description) plus ``fetched_at``. A dirty-check skips the
  write when no listing field changed, so a same-snapshot re-fetch neither
  writes nor bumps ``fetched_at`` (no false staleness). Derived/user fields are
  never touched by fetch under any flag.
- **no absence tracking** — a stored vacancy missing from a scan keeps its row
  untouched: listings reorder and filter volatile, so absence proves nothing
  about the vacancy itself. ``soft_deleted_at`` is the only lifecycle signal
  and is set manually, never by fetch.
- **partial persistence** — a mid-slice page failure still returns the items
  loaded before it (``SearchSlice.failure``): everything loaded is persisted
  and the stage returns an ``Err`` with the failed page, the persisted pages
  and a precise resume hint.

One ``audit_log`` entry (``action='fetch'``) is written on success. This is a
callable, typer-free stage the engine controller (``jobfucker.engine``)
invokes; it is directly unit/BDD-tested through :class:`Storage` + a real
registered client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    Client,
    ClientError,
    Salary,
    SearchWindow,
    ServiceVacancyId,
    Vacancy,
)
from jobfucker.reporting import NullReporter, Reporter, RunEvent
from jobfucker.stages.fetch_plan import FetchPlan, plan_fetch
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord

__all__ = ["FetchInputs", "FetchReport", "FetchedVacancy", "run_fetch"]


@dataclass(frozen=True, slots=True)
class FetchInputs:
    """Everything the fetch stage needs beyond storage + the pipeline.

    ``client`` is resolved by the engine controller from the pipeline config +
    client factory (never hardcoded in core); ``search_index``/``query``
    identify the search-pool entry being walked (the client resolves the index
    to its prebuilt board filter); ``params`` carries the resolved window/slice
    intent (0-based listing positions). ``refresh`` opts into overwriting
    listing fields of already-stored rows (insert-only mirror sync is the
    default; soft-deleted rows are never touched even under refresh).
    """

    client: Client
    search_index: int
    query: str
    params: SearchWindow = field(default_factory=SearchWindow)
    refresh: bool = False


@dataclass(frozen=True, slots=True)
class FetchedVacancy:
    """Persisted identity of one fetched vacancy (for the fetch report)."""

    external_id: ServiceVacancyId
    title: str


@dataclass(frozen=True, slots=True)
class FetchReport:
    """Aggregate outcome of one ``run_fetch`` invocation (one pool entry)."""

    pipeline_id: int
    fetched: int  # vacancies persisted (inserts + refreshed) inside the slice
    requested: int  # slice size (what the user asked to persist)
    pages: int  # native pages the client actually scanned
    exhausted: bool  # listing ended before the slice was filled
    vacancies: tuple[FetchedVacancy, ...]
    already_stored: int = 0  # stored ids excluded from the client call (insert-only default)
    search_index: int = 0  # pool position of the walked entry
    query: str = ""  # the entry's query (for per-entry reporting)


def _client_error_message(error: ClientError) -> str:
    """A single human-readable string for any ``ClientError`` member."""
    return error.message


def _now() -> str:
    """A SQLite-style UTC timestamp for ``fetched_at`` (DDL stores TEXT)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _salary_to_str(salary: Salary | None) -> str | None:
    """Render a contract :class:`Salary` into the DTO's plaintext ``salary``."""
    if salary is None:
        return None
    lower = salary.from_ if salary.from_ is not None else ""
    upper = salary.to if salary.to is not None else ""
    text = f"{lower}-{upper}" if lower != "" and upper != "" else f"{lower}{upper}"
    return f"{text} {salary.currency}".strip() if salary.currency else text


def _to_record(
    pipeline: Pipeline,
    vacancy: Vacancy,
    *,
    fetched_snapshot_id: int,
    fetched_at: str,
) -> VacancyRecord:
    """Map a contract :class:`Vacancy` onto a fresh (insert-only) record.

    Only the listing data is populated; all AI/apply result fields and the
    staleness timestamps (except ``fetched_at``) start null. A new
    ``external_id`` is the only path that reaches this — existing rows are
    skipped by default or merged via :func:`_refreshed_record` under
    ``--refresh``, so processed state is never wiped by a fetch.
    """
    return VacancyRecord(
        id=0,
        pipeline_id=pipeline.id,
        fetched_snapshot_id=fetched_snapshot_id,
        scored_snapshot_id=None,
        generated_snapshot_id=None,
        applied_snapshot_id=None,
        external_id=vacancy.external_id,
        title=vacancy.title,
        url=vacancy.url,
        company=vacancy.company,
        description=vacancy.description,
        salary=_salary_to_str(vacancy.salary),
        score=None,
        score_reasoning=None,
        score_error=None,
        cover_letter=None,
        cover_letter_error=None,
        apply_status=None,
        apply_error=None,
        skip_reason=None,
        applied_at=None,
        fetched_at=fetched_at,
        scored_at=None,
        generated_at=None,
        user_edited_at=None,
        manual_skip=False,
        manual_skip_reason=None,
        notes=None,
        created_at="",
        updated_at="",
        soft_deleted_at=None,
        has_hh_test=vacancy.has_hh_test,
    )


def _refreshed_record(
    existing: VacancyRecord,
    vacancy: Vacancy,
    *,
    fetched_snapshot_id: int,
    fetched_at: str,
) -> VacancyRecord:
    """Overlay the new listing fields onto the existing row (``--refresh``).

    Every derived/user field (score, cover letter, apply state, manual flags,
    notes, timestamps) is threaded through from ``existing`` untouched; only the
    listing data, ``fetched_snapshot_id`` and ``fetched_at`` change. Bumping
    ``fetched_at`` past ``scored_at``/``generated_at``/``user_edited_at`` is
    exactly what makes the row read as stale until its stage re-runs.
    """
    return replace(
        existing,
        fetched_snapshot_id=fetched_snapshot_id,
        external_id=vacancy.external_id,
        title=vacancy.title,
        url=vacancy.url,
        company=vacancy.company,
        description=vacancy.description,
        salary=_salary_to_str(vacancy.salary),
        fetched_at=fetched_at,
        has_hh_test=vacancy.has_hh_test,
    )


def _listing_changed(current: VacancyRecord, refreshed: VacancyRecord) -> bool:
    """Whether a refresh actually changes any listing field (dirty-check).

    Identical listing data means no write and no ``fetched_at`` bump: the row
    is not marked stale by a same-snapshot re-fetch.
    """
    return (
        current.title != refreshed.title
        or current.url != refreshed.url
        or current.company != refreshed.company
        or current.description != refreshed.description
        or current.salary != refreshed.salary
        or current.has_hh_test != refreshed.has_hh_test
    )


def _resume_hint(params: SearchWindow, plan: FetchPlan, resume_from: int, pages_remaining: int) -> str:
    """Precise re-run flags that continue exactly where a failed fetch stopped.

    ``resume_from`` is the exclusive end of the persisted positions (the failed
    walk always ends page-aligned). The hint repeats the flag family the user
    chose: ``--from/--take`` and ``--from/--to`` slices re-open the remainder
    of the same slice; window modes continue from the next native page.
    """
    if params.take is not None:
        return f"--from {resume_from} --take {plan.slice_end - resume_from}"
    if params.to is not None:
        return f"--from {resume_from} --to {params.to}"
    if params.first_page != 0:
        return f"--first-page {resume_from // plan.page_size} --take-pages {pages_remaining}"
    return f"--from {resume_from}"


async def run_fetch(  # noqa: PLR0913 - explicit (storage, pipeline, inputs, snapshot, progress) stage seams + error origin
    storage: Storage,
    pipeline: Pipeline,
    inputs: FetchInputs,
    *,
    snapshot_id: int,
    progress: Reporter | None = None,
    error_origin: str = "",
) -> Result[FetchReport, str]:
    """Fetch and persist one search entry's vacancies from the client.

    Args:
        storage: the storage facade (four repositories).
        pipeline: the pipeline DTO (id + ownership for the vacancies).
        inputs: the resolved client, pool entry identity + window/slice params
            (+ ``refresh``).
        snapshot_id: the :class:`PipelineSnapshot` id whose search pool produced
            this listing — stamped ``fetched_snapshot_id`` on every row (and
            recorded on the ``fetch`` audit entry).
        progress: the live output sink (:class:`NullReporter` when omitted) —
            one ``stage='fetch'`` event is published per persisted vacancy;
            per-page walk progress comes from the client's shared driver at
            ``info`` level on the same sink.
        error_origin: prefix for every error message (``"search N: "`` from the
            engine's pool walk, ``""`` otherwise).

    Returns:
        ``Ok(FetchReport)`` on success; ``Err`` for an invalid plan, a pre-scan
        client failure (auth/transport round-trip), or a mid-slice page
        failure. A page failure persists everything loaded so far and the
        error carries a resume hint.
    """
    reporter = progress if progress is not None else NullReporter()
    plan_result = plan_fetch(
        inputs.params,
        max_search_items=inputs.client.service_info.max_search_items,
        origin=error_origin,
    )
    if plan_result.is_err:
        return Err(plan_result.unwrap_err())
    plan = plan_result.unwrap()

    # Pre-pass: stored external ids (any state, including soft-deleted) become
    # the per-call exclusion so the default insert-only fetch skips them on the
    # client — no listing enrichments for already-saved vacancies, no wasted
    # detail requests. Pool overlap between entries costs a skip, not a
    # re-fetch. ``--refresh`` passes an empty set: everything is wanted.
    stored_ids = await storage.vacancies.list_external_ids(pipeline.id)
    exclude: frozenset[ServiceVacancyId] = frozenset() if inputs.refresh else frozenset(stored_ids)

    # One slice-level call: the client walks only the native pages overlapping
    # the slice and enriches only the items inside it (shared paging driver).
    slice_result = await inputs.client.search_vacancies(
        inputs.search_index,
        offset=plan.slice_start,
        limit=plan.slice_end - plan.slice_start,
        page_size=plan.page_size,
        exclude=exclude,
    )
    if slice_result.is_err:
        return Err(f"{error_origin}fetch failed: {_client_error_message(slice_result.unwrap_err())}")
    listing = slice_result.unwrap()

    stored: list[FetchedVacancy] = []
    for vacancy in listing.items:
        # Insert-only mirror sync by default; ``--refresh`` overwrites listing
        # fields only and never touches soft-deleted rows.
        existing = await storage.vacancies.get_by_external_id(pipeline.id, vacancy.external_id)
        if existing is None:
            record = _to_record(
                pipeline,
                vacancy,
                fetched_snapshot_id=snapshot_id,
                fetched_at=_now(),
            )
            await storage.vacancies.upsert(record)
            stored.append(FetchedVacancy(external_id=vacancy.external_id, title=vacancy.title))
            await reporter.publish(
                RunEvent(
                    stage="fetch",
                    message=f"fetched {vacancy.title} {vacancy.url}",
                    level="info",
                    item=vacancy.title,
                    url=vacancy.url,
                )
            )
            continue
        if existing.soft_deleted_at is not None:
            await reporter.publish(
                RunEvent(
                    stage="fetch",
                    message=f"{vacancy.title} is soft-deleted, skipped (fetch never touches it)",
                    level="debug",
                    item=vacancy.title,
                    url=vacancy.url,
                )
            )
            continue
        if not inputs.refresh:
            await reporter.publish(
                RunEvent(
                    stage="fetch",
                    message=f"already stored, skipped {vacancy.title} {vacancy.url} (use --refresh to overwrite)",
                    level="debug",
                    item=vacancy.title,
                    url=vacancy.url,
                )
            )
            continue
        refreshed = _refreshed_record(
            existing,
            vacancy,
            fetched_snapshot_id=snapshot_id,
            fetched_at=_now(),
        )
        if not _listing_changed(existing, refreshed):
            await reporter.publish(
                RunEvent(
                    stage="fetch",
                    message=f"listing unchanged, skipped {vacancy.title} {vacancy.url}",
                    level="debug",
                    item=vacancy.title,
                    url=vacancy.url,
                )
            )
            continue
        await storage.vacancies.upsert(refreshed)
        stored.append(FetchedVacancy(external_id=vacancy.external_id, title=vacancy.title))
        await reporter.publish(
            RunEvent(
                stage="fetch",
                message=f"refreshed {vacancy.title} {vacancy.url}",
                level="info",
                item=vacancy.title,
                url=vacancy.url,
            )
        )

    # NOTE: absence from the listing is deliberately NOT a lifecycle signal —
    # listings reorder and filter volatile, so a stored vacancy missing from
    # this scan simply keeps its row and data untouched. The only lifecycle
    # signal is ``soft_deleted_at``, set manually, never by fetch.

    if listing.failure is not None:
        failed_page = listing.failure.page
        message = _client_error_message(listing.failure.error)
        if not stored:
            return Err(
                f"{error_origin}fetch failed on page {failed_page}/{listing.pages_planned}: {message}. "
                "Nothing was persisted; re-run the same command"
            )
        first_loaded = listing.scanned_start // plan.page_size
        last_loaded = first_loaded + listing.pages_scanned - 1
        hint = _resume_hint(
            inputs.params,
            plan,
            resume_from=listing.offset + len(listing.items),
            pages_remaining=listing.pages_planned - listing.pages_scanned,
        )
        return Err(
            f"{error_origin}fetch failed on page {failed_page}/{listing.pages_planned}: {message}. "
            f"Pages {first_loaded}-{last_loaded} ({len(stored)} vacancies) are "
            f"already persisted. Re-run with {hint} to continue"
        )

    details = json.dumps(
        {
            "search_index": inputs.search_index,
            "query": inputs.query,
            "window_start": plan.window_start,
            "window_end": plan.window_end,
            "slice_start": plan.slice_start,
            "slice_end": plan.slice_end,
            "pages_requested": listing.pages_planned,
            "pages": listing.pages_scanned,
            "fetched": len(stored),
            "already_stored": len(exclude),
            "exhausted": listing.exhausted,
        },
        sort_keys=True,
    )
    await storage.audit_log.append(pipeline.id, "fetch", details, pipeline_snapshot_id=snapshot_id)

    return Ok(
        FetchReport(
            pipeline_id=pipeline.id,
            fetched=len(stored),
            requested=plan.slice_end - plan.slice_start,
            pages=listing.pages_scanned,
            exhausted=listing.exhausted,
            vacancies=tuple(stored),
            already_stored=len(exclude),
            search_index=inputs.search_index,
            query=inputs.query,
        )
    )
