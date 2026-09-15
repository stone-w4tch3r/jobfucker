"""Shared native-page walk for slice-level ``search_vacancies`` clients.

Pagination and slice trimming are **client-level** mechanics: the core asks an
client for a position range (``[offset, offset + limit)``) and the client
walks only the native pages overlapping that range, enriching only the items
inside it. Every client (HH, mock, the test fake) shares that walk through
:func:`scan_slice` so no board re-implements it and the "no unnecessary API
calls" invariant is enforced in one place.

Split of responsibilities:

- :func:`plan_pages` — pure math: a position range → native pages with in-page
  keep bounds. No I/O, deterministic.
- :func:`scan_slice` — the generic async walk: fetch pages sequentially through
  the client's callback, stop at a short page (listing exhausted) or at the
  first page failure, publish per-page reporter events, and assemble the final
  (possibly partial) :class:`~jobfucker.clients.base.SearchSlice`.
- :func:`scan_listing` — the listing-only twin used by ``list_vacancies``:
  identical walk geometry, but the callback returns short
  (:class:`~jobfucker.clients.base.VacancyShort`) items, no exclusion, no keep
  asymmetry of enriched vs listed items, and a failed page is a plain ``Err``
  (a listing is never persisted, so there is nothing to partially save — the
  contrasting ``scan_slice`` partial-Ok exists purely for fetch persistence).

The client supplies one callback — ``fetch_page(page, keep_start, keep_end)`` —
which fetches that native listing page and returns **only** the items whose
global listing position lies inside ``[keep_start, keep_end)``. Reporting
``listed`` (how many items the native page contained) separately from ``kept``
lets the driver detect exhaustion even when the kept subset is smaller than the
page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    BadRequestError,
    ClientError,
    SearchListing,
    SearchPageFailure,
    SearchSlice,
    ServiceVacancyId,
    Vacancy,
    VacancyShort,
)
from jobfucker.reporting import NullReporter, Reporter, RunEvent

__all__ = [
    "FetchPageFn",
    "FetchedListingPage",
    "FetchedPage",
    "ListPageFn",
    "PlannedPage",
    "plan_pages",
    "scan_listing",
    "scan_slice",
]


@dataclass(frozen=True, slots=True)
class PlannedPage:
    """One native page to fetch, with the in-page positions to keep.

    ``page`` is the zero-based native page number; ``keep_start``/``keep_end``
    are **global** listing positions (inclusive/exclusive) the client must
    enrich — the intersection of the requested slice with the page's
    ``[page * page_size, (page + 1) * page_size)`` span.
    """

    page: int
    keep_start: int
    keep_end: int


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """One fetched native page, as returned by a client's ``fetch_page``.

    ``kept`` are the fully enriched vacancies inside the page's keep range, in
    native listing order (the driver concatenates them across pages and trusts
    this invariant); ``listed`` is how many items the native page contained —
    the exhaustion signal (``listed < page_size`` means the listing ended),
    independent of how many were kept.
    """

    kept: tuple[Vacancy, ...]
    listed: int


# (page, keep_start, keep_end) -> one fetched native page (see FetchedPage).
FetchPageFn = Callable[[int, int, int], Awaitable[Result[FetchedPage, ClientError]]]


@dataclass(frozen=True, slots=True)
class FetchedListingPage:
    """One fetched native listing page, as returned by a ``scan_listing`` callback.

    ``kept`` are the short listing items inside the page's keep range, in native
    listing order (decoded straight off the listing page — no enrichment);
    ``listed`` is how many items the native page contained (the exhaustion
    signal). ``found``/``ui_url`` are the board's envelope metadata for the
    executed query; both are optional (a board may not report them) and the
    driver takes the first page that reports each.
    """

    kept: tuple[VacancyShort, ...]
    listed: int
    found: int | None = None
    ui_url: str | None = None


# (page, keep_start, keep_end) -> one fetched native listing page
# (see FetchedListingPage). Same geometry as FetchPageFn; short items only.
ListPageFn = Callable[[int, int, int], Awaitable[Result[FetchedListingPage, ClientError]]]


def plan_pages(offset: int, limit: int, page_size: int) -> Result[tuple[PlannedPage, ...], str]:
    """Map the position range ``[offset, offset + limit)`` onto native pages.

    Pure math: returns the minimal consecutive native pages covering the range,
    each trimmed to the positions the caller wants. Examples (``page_size``
    100): ``[0, 97)`` → page 0 (keep 0..97); ``[101, 111)`` → page 1 (keep
    101..111); ``[150, 250)`` → pages 1-2 (keep 150..200, 200..250).
    """
    if offset < 0:
        return Err("search offset must be >= 0")
    if limit < 1:
        return Err("search limit must be >= 1")
    if page_size < 1:
        return Err("search page_size must be >= 1")
    first_page = offset // page_size
    last_page = (offset + limit - 1) // page_size
    slice_end = offset + limit
    return Ok(
        tuple(
            PlannedPage(
                page=page,
                keep_start=max(offset, page * page_size),
                keep_end=min(slice_end, (page + 1) * page_size),
            )
            for page in range(first_page, last_page + 1)
        )
    )


async def scan_slice(  # noqa: PLR0913 - shared driver: walk geometry + optional exclusion + reporter
    fetch_page: FetchPageFn,
    *,
    offset: int,
    limit: int,
    page_size: int,
    exclude: frozenset[ServiceVacancyId] = frozenset(),
    reporter: Reporter | None = None,
) -> Result[SearchSlice, ClientError]:
    """Walk the native pages covering ``[offset, offset + limit)`` and build the slice.

    Sequentially fetches each planned page through ``fetch_page``; a page that
    lists fewer than ``page_size`` items ends the listing early (``exhausted``)
    and a failed page stops the walk with a partial result carrying
    :class:`SearchPageFailure` (never a bare ``Err`` — the pages before the
    failure are already loaded and the caller can persist them). Invalid paging
    arguments return ``Err(BadRequestError)`` — the contract surface never
    raises. Publishes one ``client``-stage reporter event per fetched page so
    progress is visible during long walks.

    ``exclude`` is an optional set of ``external_id``\\ s the caller already
    stores: those items are dropped from ``items`` (a guarantee for every
    client) and the client is expected to skip their enrichment (e.g. the HH
    detail ``GET``) inside its ``fetch_page`` callback. Exclusion filters
    enriched items only — walk geometry, ``listed`` and exhaustion detection are
    unchanged.
    """
    sink = reporter if reporter is not None else NullReporter()
    planned_result = plan_pages(offset, limit, page_size)
    if planned_result.is_err:
        return Err(BadRequestError(message=planned_result.unwrap_err()))
    planned = planned_result.unwrap()

    scanned_start = planned[0].page * page_size
    scanned_end = scanned_start
    kept: list[Vacancy] = []
    pages_scanned = 0
    exhausted = False
    failure: SearchPageFailure | None = None

    for planned_page in planned:
        page_result = await fetch_page(planned_page.page, planned_page.keep_start, planned_page.keep_end)
        if page_result.is_err:
            error = page_result.unwrap_err()
            failure = SearchPageFailure(page=planned_page.page, error=error)
            await sink.publish(
                RunEvent(
                    stage="client",
                    message=f"search page {planned_page.page + 1}/{len(planned)} failed: {error.message}",
                    level="error",
                )
            )
            break
        fetched = page_result.unwrap()
        kept.extend(vacancy for vacancy in fetched.kept if vacancy.external_id not in exclude)
        pages_scanned += 1
        scanned_end = (planned_page.page + 1) * page_size
        await sink.publish(
            RunEvent(
                stage="client",
                message=f"search page {planned_page.page + 1}/{len(planned)}: {fetched.listed} items",
                level="info",
            )
        )
        # A short page means the listing ended: no further pages exist.
        if fetched.listed < page_size:
            exhausted = True
            break

    return Ok(
        SearchSlice(
            items=tuple(kept),
            offset=offset,
            scanned_start=scanned_start,
            scanned_end=scanned_end,
            pages_scanned=pages_scanned,
            pages_planned=len(planned),
            exhausted=exhausted,
            failure=failure,
        )
    )


async def scan_listing(
    fetch_page: ListPageFn,
    *,
    offset: int,
    limit: int,
    page_size: int,
    reporter: Reporter | None = None,
) -> Result[SearchListing, ClientError]:
    """Walk the native listing pages covering ``[offset, offset + limit)`` — short items only.

    The listing-only twin of :func:`scan_slice`: identical ``plan_pages``
    geometry and per-page reporter events, but the callback decodes short
    :class:`~jobfucker.clients.base.VacancyShort` items (no detail enrichment),
    there is no ``exclude`` (a preview wants the whole listing), and a failed
    page is a plain ``Err`` — a listing is never persisted, so unlike
    :func:`scan_slice` there is no partial ``Ok`` to preserve. Invalid paging
    arguments return ``Err(BadRequestError)`` — the contract surface never
    raises. ``found``/``ui_url`` come from the first page that reports them.
    """
    sink = reporter if reporter is not None else NullReporter()
    planned_result = plan_pages(offset, limit, page_size)
    if planned_result.is_err:
        return Err(BadRequestError(message=planned_result.unwrap_err()))
    planned = planned_result.unwrap()

    kept: list[VacancyShort] = []
    found: int | None = None
    ui_url: str | None = None
    pages_scanned = 0
    exhausted = False

    for planned_page in planned:
        page_result = await fetch_page(planned_page.page, planned_page.keep_start, planned_page.keep_end)
        if page_result.is_err:
            error = page_result.unwrap_err()
            await sink.publish(
                RunEvent(
                    stage="client",
                    message=f"listing page {planned_page.page + 1}/{len(planned)} failed: {error.message}",
                    level="error",
                )
            )
            return Err(error)
        fetched = page_result.unwrap()
        kept.extend(fetched.kept)
        pages_scanned += 1
        # Envelope metadata describes the whole query, not one page: the first
        # page that reports each value wins (all pages of one walk agree).
        found = fetched.found if found is None else found
        ui_url = fetched.ui_url if ui_url is None else ui_url
        await sink.publish(
            RunEvent(
                stage="client",
                message=f"listing page {planned_page.page + 1}/{len(planned)}: {fetched.listed} items",
                level="info",
            )
        )
        # A short page means the listing ended: no further pages exist.
        if fetched.listed < page_size:
            exhausted = True
            break

    return Ok(
        SearchListing(
            items=tuple(kept),
            offset=offset,
            pages_scanned=pages_scanned,
            pages_planned=len(planned),
            exhausted=exhausted,
            found=found,
            ui_url=ui_url,
        )
    )
