"""BDD acceptance coverage for the HH listing-only search (the preview path).

Same transport style as the enriched-search feature, but the payloads are
realistic *listing* pages (short fields + envelope ``alternate_url``) and the
assertions prove the contract of ``Client.list_vacancies``: short items in
native order, envelope metadata, windowed walks, zero detail requests, and a
plain ``Err`` on a failed page (no partial listing).

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given``
seeds a frozen ``ListingScenario`` (data-configured transport), the ``When``
returns a frozen ``ListingOutcome``, and ``Then`` steps only assert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import ClientError, ProtocolError, SearchListing
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHSearchEntry, HHServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.hh.helpers import (
    RecordingSolver,
    applicant_healthcheck,
    hh_client,
    json_response,
    seed_auth_state,
)

scenarios("bdd/vacancy_listing.feature")

_PROFILE_ID = "vacancy-listing-profile"


class ListingTransport:
    """External-boundary fake: a dumb router over the scripted catalog pages."""

    def __init__(self, *, page_response: Callable[[int, int], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._page_response = page_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/me":
            return applicant_healthcheck()
        if request.url.path == "/vacancies":
            return self._page_response(int(request.url.params["page"]), int(request.url.params["per_page"]))
        raise AssertionError(f"Listing transport must never serve details, got: {request.method} {request.url}")


def _listing_page(count: int, page: int, per_page: int) -> httpx.Response:
    """A realistic listing page of ``count`` short items (ids ``L-<position>``)."""
    items = [
        {
            "id": f"L-{page * per_page + index}",
            "name": f"Listing Vacancy {page * per_page + index}",
            "alternate_url": f"https://hh.ru/vacancy/L-{page * per_page + index}",
            "employer": {"name": f"Работодатель {index}"},
            "salary": {"from": 200000 + index, "to": None, "currency": "RUR", "gross": False},
            "area": {"name": "Москва"},
            "published_at": "2026-08-30T12:00:00+0300",
            "snippet": {"requirement": "Опыт с <highlighttext>Python</highlighttext>"},
        }
        for index in range(count)
    ]
    return json_response(
        {
            "items": items,
            "found": 3552,
            "page": page,
            "pages": 4,
            "per_page": per_page,
            "alternate_url": "https://hh.ru/search/vacancy?text=python&area=1",
        }
    )


def _rich_catalog(page: int, per_page: int) -> httpx.Response:
    assert page == 0, "the rich catalog has exactly one page"
    return _listing_page(1, page, per_page)


def _dense_catalog(page: int, per_page: int) -> httpx.Response:
    return _listing_page(per_page, page, per_page)


def _second_page_fails(page: int, per_page: int) -> httpx.Response:
    if page == 1:
        return httpx.Response(500, headers={"content-type": "text/plain"}, text="boom")
    return _listing_page(per_page, page, per_page)


@dataclass(frozen=True, slots=True)
class ListingScenario:
    """Frozen setup: the client factory over the scripted transport."""

    transport: ListingTransport
    make_client: Callable[[], HHClient]


@dataclass(frozen=True, slots=True)
class ListingOutcome:
    """Frozen outcome: the listing result and the request history it produced."""

    listing: Result[SearchListing, ClientError]
    requests: tuple[httpx.Request, ...]


def _scenario(tmp_path: Path, *, page_response: Callable[[int, int], httpx.Response]) -> ListingScenario:
    transport = ListingTransport(page_response=page_response)
    data_dir = tmp_path / "data"
    seed_auth_state(data_dir, _PROFILE_ID, access_token="USER-listing", refresh_token="refresh-listing")

    def make_client() -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(resume_id="resume-1", searches=(HHSearchEntry(query="python"),)),
            solver=RecordingSolver(),
        )

    return ListingScenario(transport=transport, make_client=make_client)


async def _list_once(
    client: HHClient, *, offset: int, limit: int, page_size: int
) -> Result[SearchListing, ClientError]:
    try:
        return await client.list_vacancies(0, offset=offset, limit=limit, page_size=page_size)
    finally:
        await client.aclose()


def _list(scenario: ListingScenario, *, offset: int, limit: int, page_size: int) -> ListingOutcome:
    listing = async_run(_list_once(scenario.make_client(), offset=offset, limit=limit, page_size=page_size))
    return ListingOutcome(listing=listing, requests=tuple(scenario.transport.requests))


@given("a healthy HH session and a rich catalog page with one vacancy", target_fixture="listing_scenario")
def rich_catalog_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, page_response=_rich_catalog)


@given("a healthy HH session and a dense catalog", target_fixture="listing_scenario")
def dense_catalog_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, page_response=_dense_catalog)


@given("a healthy HH session and a catalog whose second page fails", target_fixture="listing_scenario")
def page_error_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, page_response=_second_page_fails)


@when(
    parsers.parse("the whole first page is listed with {page_size:d} items per page"),
    target_fixture="listing_outcome",
)
def list_first_page_step(listing_scenario: ListingScenario, page_size: int) -> ListingOutcome:
    return _list(listing_scenario, offset=0, limit=page_size, page_size=page_size)


@when(
    parsers.parse("positions {start:d} to {end:d} are listed with {page_size:d} items per page"),
    target_fixture="listing_outcome",
)
def list_positions_step(listing_scenario: ListingScenario, start: int, end: int, page_size: int) -> ListingOutcome:
    return _list(listing_scenario, offset=start, limit=end - start + 1, page_size=page_size)


@when("the listing page fails mid-window", target_fixture="listing_outcome")
def list_page_error_step(listing_scenario: ListingScenario) -> ListingOutcome:
    return _list(listing_scenario, offset=0, limit=200, page_size=100)


@then("the listing contains one short vacancy with decoded listing fields")
def listing_fields_step(listing_outcome: ListingOutcome) -> None:
    assert listing_outcome.listing.is_ok
    items = listing_outcome.listing.unwrap().items
    assert len(items) == 1
    item = items[0]
    assert item.external_id == "L-0"
    assert item.title == "Listing Vacancy 0"
    assert item.url == "https://hh.ru/vacancy/L-0"
    assert item.company == "Работодатель 0"
    assert item.salary is not None
    assert item.salary.from_ == 200000
    assert item.salary.to is None
    assert item.salary.currency == "RUR"
    assert item.area == "Москва"
    assert item.published_at == "2026-08-30T12:00:00+0300"
    assert item.snippet_requirement == "Опыт с <highlighttext>Python</highlighttext>"


@then("the listing carries the board-reported total and web search URL")
def listing_metadata_step(listing_outcome: ListingOutcome) -> None:
    assert listing_outcome.listing.is_ok
    listing = listing_outcome.listing.unwrap()
    assert listing.found == 3552
    assert listing.ui_url == "https://hh.ru/search/vacancy?text=python&area=1"


@then("no vacancy detail is requested")
def no_detail_step(listing_outcome: ListingOutcome) -> None:
    assert [request.url.path for request in listing_outcome.requests] == ["/me", "/vacancies"]


@then("HH walks native pages 1 and 2 only")
def listing_pages_step(listing_outcome: ListingOutcome) -> None:
    assert [request.url.params["page"] for request in listing_outcome.requests if request.url.path == "/vacancies"] == [
        "1",
        "2",
    ]


@then("the listing spans exactly positions 8 to 12")
def listing_span_step(listing_outcome: ListingOutcome) -> None:
    assert listing_outcome.listing.is_ok
    listing = listing_outcome.listing.unwrap()
    assert listing.offset == 8
    assert [v.external_id for v in listing.items] == [f"L-{position}" for position in range(8, 13)]
    assert listing.pages_scanned == 2


@then("the listing fails with the page error and no partial result")
def listing_error_step(listing_outcome: ListingOutcome) -> None:
    assert listing_outcome.listing.is_err
    error = listing_outcome.listing.unwrap_err()
    assert isinstance(error, ProtocolError)  # a 500 page maps to the protocol error class
