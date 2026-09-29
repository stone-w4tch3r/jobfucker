"""Habr vacancy search: listing JSON walk plus SSR detail enrichment.

The listing surface is ``GET /api/frontend/vacancies`` (JSON, anonymous works);
the full vacancy text lives only on the HTML detail page, whose server-rendered
state is one ``<script type="application/json" data-ssr-state="true">`` block
whose ``vacancy`` member is the detail object (docs/habr; live-pinned in
``.kilo/habr-recon-findings.md`` §1, §5).

Paging and slice trimming are the shared driver's job
(:mod:`jobfucker.clients.paging`): Habr pages are **1-based**, so the native
(driver) page is sent as ``page + 1``; the client clamps ``page_size`` to Habr's
effective cap of 50 before walking. Exhaustion is a short/empty ``200`` page or
(defensively) a ``404`` listing page — never a hard failure; mid-slice page
failures stay partial ``Ok`` results owned by the shared driver.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ClientError,
    NotFoundError,
    ProtocolError,
    Salary,
    SearchListing,
    SearchSlice,
    ServiceVacancyId,
    TransportError,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.habr.config import HabrSearchFilters, HabrSearchType, encode_search_type
from jobfucker.clients.habr.models import (
    HabrListingItem,
    HabrListingResponse,
    HabrSalary,
    HabrSsrState,
    HabrVacancyDetail,
)
from jobfucker.clients.habr.transport import CAREER_ORIGIN, FormFields, HabrTransport, decode_json
from jobfucker.clients.paging import FetchedListingPage, FetchedPage, scan_listing, scan_slice
from jobfucker.clients.shared.html_text import DescriptionParser

__all__ = ["SearchService", "clamp_page_size", "effective_page_size", "fetch_vacancy_detail", "parse_vacancy_detail"]

# Habr echoes any requested `per_page` but never returns more than 50 items, and
# computes `totalPages` from 50: the client's geometry must match the board.
_MAX_PER_PAGE: Final = 50

# Live-pinned 2026-09-29: `type=suitable` ignores `per_page` entirely — any
# requested size (20/25/50) returns 25 items with `meta.perPage=25`, so the walk
# geometry must use a fixed 25-item stride for that search type.
_SUITABLE_PER_PAGE: Final = 25

_LISTING_PATH: Final = "/api/frontend/vacancies"
_PAGING_PARAMS: Final = frozenset({"page", "per_page"})

# The detail page carries exactly one inline SSR state script. Anchor on the
# stable ``data-ssr-state="true"`` attribute (tolerant of attribute order and
# extra attributes) and still confirm the tag is a ``type="application/json"``
# script; a fixed attribute order breaks on a template reshuffle (observed risk).
_SCRIPT_TAG_PATTERN: Final = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.IGNORECASE)
_SSR_STATE_ATTR_PATTERN: Final = re.compile(r"\bdata-ssr-state\s*=\s*[\"']true[\"']", re.IGNORECASE)
_JSON_TYPE_ATTR_PATTERN: Final = re.compile(r"\btype\s*=\s*[\"']application/json[\"']", re.IGNORECASE)

_HTTP_OK: Final = 200
_HTTP_NOT_FOUND: Final = 404
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR_MIN: Final = 500


def clamp_page_size(page_size: int) -> int:
    """Clamp a requested page size to Habr's effective listing cap of 50."""
    return min(page_size, _MAX_PER_PAGE)


def effective_page_size(search_type: HabrSearchType, page_size: int) -> int:
    """The per-page geometry the board actually honors for a search type.

    ``type=suitable`` ignores ``per_page`` and always returns 25 items, so the
    walk stride must be 25 regardless of the requested size; every other type
    honors ``per_page`` up to the 50-item cap.
    """
    if search_type is HabrSearchType.SUITABLE:
        return _SUITABLE_PER_PAGE
    return clamp_page_size(page_size)


class SearchService:
    """Fetch a position slice of Habr listings, enriching only the kept items."""

    def __init__(self, transport: HabrTransport) -> None:
        self._transport = transport

    async def search(  # noqa: PLR0913 - contract slice params + the resolved entry wires
        self,
        query: str,
        filters: HabrSearchFilters,
        search_type: HabrSearchType,
        *,
        offset: int,
        limit: int,
        page_size: int,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Return one slice of fully enriched contract vacancies.

        ``query``/``filters``/``search_type`` are one pool entry's resolved wires
        (the client resolves the entry by index before calling). The shared
        driver walks only the native pages overlapping ``[offset, offset + limit)``;
        this service's page callback fetches one native listing page and enriches
        **only** the items inside its keep range. Items whose ``external_id`` is
        in ``exclude`` skip the detail ``GET`` entirely, so an insert-only
        re-fetch does zero detail work.
        """
        stride = effective_page_size(search_type, page_size)

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
            page_result = await _fetch_listing_page(
                self._transport, query, filters, search_type, page=page, page_size=stride
            )
            if page_result.is_err:
                return Err(page_result.unwrap_err())
            listing = page_result.unwrap()
            if listing is None:
                # A 404 listing page is exhaustion, not a failure (defensive:
                # live Habr answers 200 empty, but the wiki documented a 404).
                return Ok(FetchedPage(kept=(), listed=0))
            kept: list[Vacancy] = []
            # Clamp to the page window (as the listing path does) so a page that
            # returns extra items cannot shift a later page's positions.
            for index, item in enumerate(listing.items[:stride]):
                position = page * stride + index
                if not keep_start <= position < keep_end:
                    continue  # outside the requested slice: never enrich it
                if ServiceVacancyId(str(item.id)) in exclude:
                    continue  # already stored: skip the detail GET entirely
                detail_result = await fetch_vacancy_detail(self._transport, str(item.id))
                if detail_result.is_err:
                    return Err(detail_result.unwrap_err())
                kept.append(_to_vacancy(detail_result.unwrap()))
            return Ok(FetchedPage(kept=tuple(kept), listed=len(listing.items)))

        return await scan_slice(fetch_page, offset=offset, limit=limit, page_size=stride, exclude=exclude)

    async def list_vacancies(  # noqa: PLR0913 - contract window params + the resolved entry wires
        self,
        query: str,
        filters: HabrSearchFilters,
        search_type: HabrSearchType,
        *,
        offset: int,
        limit: int,
        page_size: int,
    ) -> Result[SearchListing, ClientError]:
        """Return one window of the Habr listing as short items — no enrichment.

        The preview twin of :meth:`search`: the same native-page walk and filter
        encoding, but the page callback decodes the listing's short fields
        straight off the JSON (no detail ``GET`` at all). ``found`` comes from the
        envelope's ``meta.totalResults``;         ``ui_url`` is built by the client from
        the executed params (Habr returns no UI URL).
        """
        stride = effective_page_size(search_type, page_size)

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
            params = _search_params(query, filters, search_type, page=page, page_size=stride)
            page_result = await _fetch_listing_page(
                self._transport, query, filters, search_type, page=page, page_size=stride
            )
            if page_result.is_err:
                return Err(page_result.unwrap_err())
            listing = page_result.unwrap()
            if listing is None:
                return Ok(FetchedListingPage(kept=(), listed=0, found=None, ui_url=_ui_url(params)))
            # Cap at the page boundary so a misbehaving page with extra items
            # cannot double-count positions overlapping the next keep range.
            window_start = page * stride
            window_end = min(window_start + stride, window_start + len(listing.items))
            kept_start = max(keep_start, window_start) - window_start
            kept_end = min(keep_end, window_end) - window_start
            return Ok(
                FetchedListingPage(
                    kept=tuple(
                        _to_vacancy_short(item) for item in listing.items[max(kept_start, 0) : max(kept_end, 0)]
                    ),
                    listed=len(listing.items),
                    found=listing.meta.total_results,
                    ui_url=_ui_url(params),
                )
            )

        return await scan_listing(fetch_page, offset=offset, limit=limit, page_size=stride)


async def _fetch_listing_page(  # noqa: PLR0913 - one call site: transport + resolved entry wires + page
    transport: HabrTransport,
    query: str,
    filters: HabrSearchFilters,
    search_type: HabrSearchType,
    *,
    page: int,
    page_size: int,
) -> Result[HabrListingResponse | None, ClientError]:
    """Fetch and decode one native listing page; ``None`` means past the end (``404``).

    ``page_size`` is the effective stride the client computed for this search
    type; the decoded ``meta.perPage`` must agree with it or the page's item
    positions cannot be trusted, so a mismatch fails loudly instead of silently
    mis-slicing the walk.
    """
    params = _search_params(query, filters, search_type, page=page, page_size=page_size)
    response_result = await transport.get_json(_LISTING_PATH, params=params)
    if response_result.is_err:
        return Err(response_result.unwrap_err())
    response = response_result.unwrap()
    if response.status_code == _HTTP_NOT_FOUND:
        return Ok(None)
    failure = _listing_status_error(response)
    if failure is not None:
        return Err(failure)
    decoded = decode_json(response, HabrListingResponse, operation="vacancy search")
    if decoded.is_err:
        return Err(decoded.unwrap_err())
    listing = decoded.unwrap()
    if listing.meta.per_page != page_size:
        return Err(
            ProtocolError(
                message=(
                    "Habr listing page size mismatch: "
                    f"requested per_page={page_size}, board reported {listing.meta.per_page}"
                )
            )
        )
    return Ok(listing)


async def fetch_vacancy_detail(transport: HabrTransport, vacancy_id: str) -> Result[HabrVacancyDetail, ClientError]:
    """Fetch one vacancy detail page and decode its SSR ``vacancy`` object."""
    response_result = await transport.get_website(f"/vacancies/{vacancy_id}")
    if response_result.is_err:
        return Err(response_result.unwrap_err())
    response = response_result.unwrap()
    failure = _detail_status_error(response)
    if failure is not None:
        return Err(failure)
    return parse_vacancy_detail(response.text)


def parse_vacancy_detail(html: str) -> Result[HabrVacancyDetail, ClientError]:
    """Extract the inline SSR ``vacancy`` object from a detail page's HTML."""
    blob = _find_ssr_state_blob(html)
    if blob is None:
        return Err(ProtocolError(message="Habr vacancy page carried no inline SSR state"))
    try:
        state = HabrSsrState.model_validate_json(blob)
    except ValidationError:
        return Err(ProtocolError(message="Malformed Habr vacancy detail state"))
    return Ok(state.vacancy)


def _find_ssr_state_blob(html: str) -> str | None:
    """Return the JSON body of the ``data-ssr-state`` application/json script tag."""
    for match in _SCRIPT_TAG_PATTERN.finditer(html):
        attributes = match.group(1)
        if _SSR_STATE_ATTR_PATTERN.search(attributes) and _JSON_TYPE_ATTR_PATTERN.search(attributes):
            return match.group(2)
    return None


def _search_params(
    query: str,
    filters: HabrSearchFilters,
    search_type: HabrSearchType,
    *,
    page: int,
    page_size: int,
) -> FormFields:
    """Encode one entry's listing params (1-based ``page``, clamped ``per_page``).

    Boolean filters are emitted only when true and ``salary``/``currency`` only
    when a floor is set; list filters repeat one query param per value. ``sort``
    is always sent (it has a board default but is part of the executed contract).
    """
    params: FormFields = (
        ("q", query),
        ("type", encode_search_type(search_type)),
        ("page", str(page + 1)),
        ("per_page", str(page_size)),
        ("sort", filters.sort),
    )
    if filters.qid is not None:
        params += (("qid", str(filters.qid)),)
    if filters.remote:
        params += (("remote", "true"),)
    if filters.with_salary:
        params += (("with_salary", "true"),)
    if filters.salary is not None:
        params += (("salary", str(filters.salary)), ("currency", filters.currency))
    params += tuple(("skills[]", str(skill_id)) for skill_id in filters.skills)
    params += tuple(("locations[]", location) for location in filters.locations)
    if filters.employment_type is not None:
        params += (("employment_type", filters.employment_type),)
    return params


def _ui_url(params: FormFields) -> str:
    """Build the human-facing search URL from the executed params (no paging)."""
    display = tuple((name, value) for name, value in params if name not in _PAGING_PARAMS)
    return f"{CAREER_ORIGIN}/vacancies?{urlencode(display)}"


def _listing_status_error(response: httpx.Response) -> ClientError | None:
    """Map a non-200 listing status; ``404`` is handled by the caller as exhaustion."""
    status = response.status_code
    if status == _HTTP_OK:
        return None
    if status >= _HTTP_SERVER_ERROR_MIN or status == _HTTP_TOO_MANY_REQUESTS:
        return TransportError(message="Habr vacancy listing is unavailable", status=status)
    return ProtocolError(message="Unexpected Habr vacancy listing status", status=status)


def _detail_status_error(response: httpx.Response) -> ClientError | None:
    """Map a non-200 detail status."""
    status = response.status_code
    if status == _HTTP_OK:
        return None
    if status == _HTTP_NOT_FOUND:
        return NotFoundError(message="Habr vacancy detail was not found")
    if status >= _HTTP_SERVER_ERROR_MIN or status == _HTTP_TOO_MANY_REQUESTS:
        return TransportError(message="Habr vacancy detail is unavailable", status=status)
    return ProtocolError(message="Unexpected Habr vacancy detail status", status=status)


def _salary(salary: HabrSalary | None) -> Salary | None:
    """Map the board salary (lowercased currency, no gross flag) to the contract.

    Habr's ``predictedSalary`` is a *separate* field and is never merged here.
    """
    if salary is None:
        return None
    currency = salary.currency.upper() if salary.currency is not None else None
    return Salary(from_=salary.from_, to=salary.to, currency=currency, gross=False)


def _to_vacancy_short(item: HabrListingItem) -> VacancyShort:
    """Map a validated listing item into the board-neutral short contract."""
    return VacancyShort(
        external_id=ServiceVacancyId(str(item.id)),
        title=item.title,
        url=f"{CAREER_ORIGIN}{item.href}",
        company=item.company.title if item.company is not None else None,
        salary=_salary(item.salary),
        area=item.locations[0].title if item.locations else None,
        published_at=item.published_date.date if item.published_date is not None else None,
        snippet_requirement=None,
        snippet_responsibility=None,
    )


def _to_vacancy(detail: HabrVacancyDetail) -> Vacancy:
    """Map a validated detail object into the board-neutral enriched contract."""
    parser = DescriptionParser()
    parser.feed(detail.description)
    parser.close()
    return Vacancy(
        external_id=ServiceVacancyId(str(detail.id)),
        title=detail.title,
        url=f"{CAREER_ORIGIN}{detail.href}",
        company=detail.company.title if detail.company is not None else None,
        description=parser.text(),
        key_skills=tuple(skill.title for skill in detail.skills),
        salary=_salary(detail.salary),
        has_hh_test=None,
    )
