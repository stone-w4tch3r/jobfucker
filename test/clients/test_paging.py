"""Unit coverage for the shared client-side paging driver (``clients/paging.py``).

Pure math (``plan_pages``) plus the generic walk (``scan_slice``): exhaustion
detection, mid-walk failure → partial slice, reporter events, and the
invalid-argument guard. These mechanics are board-neutral and shared by every
client, so they are tested here once — the client tests only prove each
board plugs the driver in correctly.
"""

from __future__ import annotations

import pytest
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    BadRequestError,
    ClientError,
    ServiceVacancyId,
    TransportError,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.paging import (
    FetchedListingPage,
    FetchedPage,
    FetchPageFn,
    ListPageFn,
    plan_pages,
    scan_listing,
    scan_slice,
)
from jobfucker.reporting import Reporter, RunEvent


def _vacancy(index: int) -> Vacancy:
    """A minimal distinct vacancy for position/order assertions."""
    return Vacancy(
        external_id=ServiceVacancyId(f"v-{index}"),
        title=f"Vacancy {index}",
        url=f"https://example.test/v/{index}",
        company=None,
        description="canned",
        key_skills=(),
        salary=None,
    )


def _fake_fetch_page(
    listing: list[Vacancy],
    calls: list[tuple[int, int, int]],
) -> FetchPageFn:
    """A driver callback over an in-memory listing, recording (page, keep) calls."""

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        calls.append((page, keep_start, keep_end))
        return Ok(FetchedPage(kept=tuple(listing), listed=len(listing)))

    return fetch_page


class _RecordingReporter(Reporter):
    """Captures every published event."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        self.events.append(event)


# --- plan_pages ----------------------------------------------------------------
@pytest.mark.unit
def test_plan_pages_maps_position_ranges_onto_trimmed_native_pages() -> None:
    """The slice→pages table: minimal pages with exact in-page keep bounds."""
    cases = (
        # (offset, limit, page_size) -> ((page, keep_start, keep_end), ...)
        (0, 97, 100, ((0, 0, 97),)),
        (0, 202, 100, ((0, 0, 100), (1, 100, 200), (2, 200, 202))),
        (101, 10, 100, ((1, 101, 111),)),
        (150, 100, 100, ((1, 150, 200), (2, 200, 250))),
        (0, 250, 100, ((0, 0, 100), (1, 100, 200), (2, 200, 250))),
        (200, 100, 100, ((2, 200, 300),)),
        (0, 3, 2, ((0, 0, 2), (1, 2, 3))),
    )
    for offset, limit, page_size, expected in cases:
        result = plan_pages(offset, limit, page_size)
        assert result.is_ok
        pages = result.unwrap()
        assert [(p.page, p.keep_start, p.keep_end) for p in pages] == list(expected)


@pytest.mark.unit
def test_plan_pages_rejects_invalid_arguments() -> None:
    """Basic sanity bounds: offset >= 0, limit >= 1, page_size >= 1."""
    cases = (
        (-1, 10, 100, "search offset must be >= 0"),
        (0, 0, 100, "search limit must be >= 1"),
        (0, 10, 0, "search page_size must be >= 1"),
    )
    for offset, limit, page_size, message in cases:
        result = plan_pages(offset, limit, page_size)
        assert result.is_err
        assert result.unwrap_err() == message


# --- scan_slice ----------------------------------------------------------------
@pytest.mark.unit
async def test_scan_slice_walks_all_planned_pages_and_assembles_the_slice() -> None:
    """A full walk: concatenated items, page-aligned scanned range, no exhaustion."""
    calls: list[tuple[int, int, int]] = []
    pages: list[list[Vacancy]] = [[_vacancy(i) for i in range(100)], [_vacancy(i) for i in range(100, 150)]]

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        calls.append((page, keep_start, keep_end))
        items = pages[page]
        kept = items[keep_start - page * 100 : keep_end - page * 100]
        return Ok(FetchedPage(kept=tuple(kept), listed=len(items)))

    result = await scan_slice(fetch_page, offset=50, limit=100, page_size=100)

    assert result.is_ok
    listing = result.unwrap()
    assert [v.external_id for v in listing.items] == [f"v-{i}" for i in range(50, 150)]
    assert listing.offset == 50
    assert (listing.scanned_start, listing.scanned_end) == (0, 200)  # page-aligned
    assert listing.pages_scanned == 2
    assert listing.pages_planned == 2
    assert listing.exhausted is True  # page 1 listed 50 < 100: listing ended
    assert listing.failure is None
    # The keep bounds trim exactly the requested positions out of each page.
    assert calls == [(0, 50, 100), (1, 100, 150)]


@pytest.mark.unit
async def test_scan_slice_stops_at_short_page_and_skips_later_pages() -> None:
    """A short page means the listing ended: later planned pages are never fetched."""
    calls: list[tuple[int, int, int]] = []
    fetch_page = _fake_fetch_page([_vacancy(0)], calls)

    result = await scan_slice(fetch_page, offset=0, limit=250, page_size=100)

    assert result.is_ok
    listing = result.unwrap()
    assert listing.exhausted is True
    assert listing.pages_scanned == 1
    assert listing.pages_planned == 3
    assert [c[0] for c in calls] == [0]  # pages 1-2 never fetched


@pytest.mark.unit
async def test_scan_slice_returns_partial_slice_on_page_failure() -> None:
    """A failing page stops the walk: items before it survive, failure is carried."""
    calls: list[tuple[int, int, int]] = []
    pages: list[list[Vacancy]] = [[_vacancy(i) for i in range(100)]]

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        calls.append((page, keep_start, keep_end))
        if page == 1:
            return Err(TransportError(message="boom"))
        items = pages[page]
        kept = items[keep_start - page * 100 : keep_end - page * 100]
        return Ok(FetchedPage(kept=tuple(kept), listed=len(items)))

    result = await scan_slice(fetch_page, offset=0, limit=202, page_size=100)

    assert result.is_ok  # partial result, never a bare Err for a page failure
    listing = result.unwrap()
    assert len(listing.items) == 100
    assert listing.failure is not None
    assert listing.failure.page == 1
    assert isinstance(listing.failure.error, TransportError)
    assert listing.scanned_end == 100  # the failed page is not part of the scan
    assert listing.pages_scanned == 1
    assert listing.pages_planned == 3
    assert [c[0] for c in calls] == [0, 1]  # the walk stopped right after the failure


@pytest.mark.unit
async def test_scan_slice_first_page_failure_yields_empty_partial_slice() -> None:
    """Failing on the very first page: an empty slice with the failure descriptor."""
    calls: list[tuple[int, int, int]] = []

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        calls.append((page, keep_start, keep_end))
        return Err(TransportError(message="unreachable board"))

    result = await scan_slice(fetch_page, offset=0, limit=100, page_size=100)

    assert result.is_ok
    listing = result.unwrap()
    assert listing.items == ()
    assert listing.pages_scanned == 0
    assert listing.scanned_start == listing.scanned_end == 0
    assert listing.failure is not None
    assert listing.failure.page == 0
    assert [c[0] for c in calls] == [0]


@pytest.mark.unit
async def test_scan_slice_rejects_invalid_paging_arguments_without_raising() -> None:
    """The contract surface never raises: bad arguments map to BadRequestError."""
    fetch_page = _fake_fetch_page([_vacancy(0)], [])
    for result in (
        await scan_slice(fetch_page, offset=-1, limit=10, page_size=100),
        await scan_slice(fetch_page, offset=0, limit=0, page_size=100),
        await scan_slice(fetch_page, offset=0, limit=10, page_size=0),
    ):
        assert result.is_err
        assert isinstance(result.unwrap_err(), BadRequestError)


@pytest.mark.unit
async def test_scan_slice_publishes_one_reporter_event_per_page() -> None:
    """Each walked page publishes an info-tier progress event; failures an error one."""
    reporter = _RecordingReporter()

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        del keep_start, keep_end
        if page == 1:
            return Err(TransportError(message="boom"))
        return Ok(FetchedPage(kept=(), listed=100))

    result = await scan_slice(fetch_page, offset=0, limit=250, page_size=100, reporter=reporter)

    assert result.is_ok
    messages = [event.message for event in reporter.events]
    assert messages == [
        "search page 1/3: 100 items",
        "search page 2/3 failed: boom",
    ]
    assert [event.level for event in reporter.events] == ["info", "error"]


@pytest.mark.unit
async def test_scan_slice_drops_excluded_ids_without_breaking_walk_geometry() -> None:
    """``exclude`` filters enriched items only; pages/listed/exhaustion are unchanged."""
    pages: list[list[Vacancy]] = [[_vacancy(i) for i in range(100)], [_vacancy(i) for i in range(100, 150)]]
    calls: list[tuple[int, int, int]] = []

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        calls.append((page, keep_start, keep_end))
        items = pages[page]
        kept = items[keep_start - page * 100 : keep_end - page * 100]
        return Ok(FetchedPage(kept=tuple(kept), listed=len(items)))

    excluded = frozenset({ServiceVacancyId("v-50"), ServiceVacancyId("v-120")})
    result = await scan_slice(fetch_page, offset=50, limit=100, page_size=100, exclude=excluded)

    assert result.is_ok
    listing = result.unwrap()
    ids = [v.external_id for v in listing.items]
    assert ServiceVacancyId("v-50") not in ids
    assert ServiceVacancyId("v-120") not in ids
    assert len(ids) == 98  # both pages still walked; only the two ids dropped
    assert (listing.scanned_start, listing.scanned_end) == (0, 200)  # walk geometry unchanged
    assert listing.pages_scanned == 2
    assert listing.pages_planned == 2
    assert listing.exhausted is True  # page 1 listed 50 < 100 regardless of exclusions
    assert calls == [(0, 50, 100), (1, 100, 150)]  # page walk unchanged
    assert listing.failure is None


@pytest.mark.unit
async def test_scan_slice_default_exclude_is_a_noop() -> None:
    """No ``exclude`` given: every enriched item is kept (existing callers unaffected)."""
    listing_vacancies = [_vacancy(i) for i in range(3)]
    fetch_page = _fake_fetch_page(listing_vacancies, [])

    result = await scan_slice(fetch_page, offset=0, limit=10, page_size=100)

    assert result.is_ok
    assert [v.external_id for v in result.unwrap().items] == [f"v-{i}" for i in range(3)]


@pytest.mark.unit
async def test_scan_slice_defaults_to_a_silent_reporter() -> None:
    """No reporter given: the walk still completes through the NullReporter."""

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
        del keep_start, keep_end, page
        return Ok(FetchedPage(kept=(_vacancy(0),), listed=1))

    result = await scan_slice(fetch_page, offset=0, limit=10, page_size=100, reporter=None)
    assert result.is_ok
    assert len(result.unwrap().items) == 1


# --- scan_listing ----------------------------------------------------------------
def _short_item(index: int) -> VacancyShort:
    """A minimal distinct short item for position/order assertions."""
    return VacancyShort(
        external_id=ServiceVacancyId(f"s-{index}"),
        title=f"Vacancy {index}",
        url=f"https://example.test/s/{index}",
        company=None,
        salary=None,
        area=None,
        published_at=None,
        snippet_requirement=None,
        snippet_responsibility=None,
    )


def _listing_fetch_page(
    pages: list[list[VacancyShort]],
    calls: list[int],
    *,
    found: int | None = 999,
    ui_url: str | None = "https://example.test/search?x",
) -> ListPageFn:
    """A listing driver callback over in-memory pages, recording fetched page numbers."""

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
        calls.append(page)
        items = pages[page]
        page_start = page * 100
        kept = items[max(0, keep_start - page_start) : max(0, keep_end - page_start)]
        return Ok(FetchedListingPage(kept=tuple(kept), listed=len(items), found=found, ui_url=ui_url))

    return fetch_page


@pytest.mark.unit
async def test_scan_listing_walks_pages_and_trims_to_the_window() -> None:
    """Items cover exactly [offset, offset+limit): the driver trims whole pages."""
    pages = [[_short_item(i) for i in range(100)], [_short_item(i) for i in range(100, 150)]]
    calls: list[int] = []

    result = await scan_listing(_listing_fetch_page(pages, calls), offset=50, limit=100, page_size=100)

    assert result.is_ok
    listing = result.unwrap()
    assert [v.external_id for v in listing.items] == [f"s-{i}" for i in range(50, 150)]
    assert listing.offset == 50
    assert listing.pages_scanned == 2
    assert listing.pages_planned == 2
    assert listing.exhausted is True  # page 1 listed 50 < 100
    assert listing.found == 999
    assert listing.ui_url == "https://example.test/search?x"
    assert calls == [0, 1]


@pytest.mark.unit
async def test_scan_listing_stops_at_short_page() -> None:
    """A short page ends the listing: later planned pages are never fetched."""
    calls: list[int] = []

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
        calls.append(page)
        del keep_start, keep_end
        return Ok(FetchedListingPage(kept=(_short_item(0),), listed=1))

    result = await scan_listing(fetch_page, offset=0, limit=250, page_size=100)

    assert result.is_ok
    listing = result.unwrap()
    assert listing.exhausted is True
    assert listing.pages_scanned == 1
    assert listing.pages_planned == 3
    assert calls == [0]


@pytest.mark.unit
async def test_scan_listing_page_failure_is_a_plain_error() -> None:
    """A failed page is a plain Err (no partial listing: nothing is persisted)."""
    calls: list[int] = []
    pages = [[_short_item(i) for i in range(100)], []]

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
        calls.append(page)
        if page == 1:
            return Err(TransportError(message="boom"))
        items = pages[page]
        return Ok(FetchedListingPage(kept=tuple(items), listed=len(items)))

    result = await scan_listing(fetch_page, offset=0, limit=200, page_size=100)

    assert result.is_err
    assert isinstance(result.unwrap_err(), TransportError)
    assert calls == [0, 1]  # the walk stopped at the failure


@pytest.mark.unit
async def test_scan_listing_takes_envelope_metadata_from_first_reporting_page() -> None:
    """``found``/``ui_url`` come from the first page; later pages cannot override."""
    calls: list[int] = []

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
        calls.append(page)
        del keep_start, keep_end
        found = 42 if page == 0 else 999
        ui_url = "https://first.example" if page == 0 else "https://second.example"
        return Ok(FetchedListingPage(kept=(_short_item(page),), listed=1, found=found, ui_url=ui_url))

    result = await scan_listing(fetch_page, offset=0, limit=2, page_size=1)

    assert result.is_ok
    listing = result.unwrap()
    assert listing.found == 42
    assert listing.ui_url == "https://first.example"


@pytest.mark.unit
async def test_scan_listing_tolerates_absent_envelope_metadata() -> None:
    """A board without ``found``/``ui_url`` (e.g. the mock) yields ``None`` metadata."""
    pages = [[_short_item(0)]]
    calls: list[int] = []

    result = await scan_listing(
        _listing_fetch_page(pages, calls, found=None, ui_url=None), offset=0, limit=1, page_size=100
    )

    assert result.is_ok
    listing = result.unwrap()
    assert listing.found is None
    assert listing.ui_url is None


@pytest.mark.unit
async def test_scan_listing_rejects_invalid_paging_arguments_without_raising() -> None:
    """The contract surface never raises: bad arguments map to BadRequestError."""
    calls: list[int] = []
    fetch_page = _listing_fetch_page([[_short_item(0)]], calls)
    for result in (
        await scan_listing(fetch_page, offset=-1, limit=10, page_size=100),
        await scan_listing(fetch_page, offset=0, limit=0, page_size=100),
        await scan_listing(fetch_page, offset=0, limit=10, page_size=0),
    ):
        assert result.is_err
        assert isinstance(result.unwrap_err(), BadRequestError)


@pytest.mark.unit
async def test_scan_listing_publishes_one_reporter_event_per_page() -> None:
    """Each walked page publishes an info-tier progress event; a failure an error one."""
    reporter = _RecordingReporter()
    calls: list[int] = []

    async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
        calls.append(page)
        del keep_start, keep_end
        if page == 1:
            return Err(TransportError(message="boom"))
        return Ok(FetchedListingPage(kept=tuple(_short_item(i) for i in range(100)), listed=100))

    result = await scan_listing(fetch_page, offset=0, limit=200, page_size=100, reporter=reporter)

    assert result.is_err
    messages = [event.message for event in reporter.events]
    assert messages == [
        "listing page 1/2: 100 items",
        "listing page 2/2 failed: boom",
    ]
    assert [event.level for event in reporter.events] == ["info", "error"]
