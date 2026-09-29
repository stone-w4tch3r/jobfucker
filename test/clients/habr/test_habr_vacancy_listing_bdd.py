"""BDD acceptance for the Habr listing-only preview path (test/AGENTS.md §3b).

``list_vacancies`` walks the same native pages as the enriched search but never
issues a detail ``GET``; the ``When`` returns a frozen outcome carrying the
``Result`` and the recorded request history.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import ClientError, SearchListing, TransportError
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import HabrSearchEntry, HabrSearchType, HabrServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver
from test.clients.habr.helpers import (
    JsonBlob,
    RecordingSolver,
    habr_deps,
    listing_item,
    listing_page,
    profile_id_for,
)

scenarios("bdd/vacancy_listing.feature")

_PROFILE_ID: Final = "habr-listing"
_LISTING_PATH: Final = "/api/frontend/vacancies"
_DETAIL_PREFIX: Final = "/vacancies/"
_EXPECTED_UI_URL: Final = "https://career.habr.com/vacancies?q=python&type=all&sort=relevance"

CatalogResponder = Callable[[int, int], httpx.Response]


class HabrListingRouter:
    """Dumb router over the Habr listing endpoint; a detail GET is a test failure."""

    def __init__(self, catalog: CatalogResponder) -> None:
        self.requests: list[httpx.Request] = []
        self._catalog = catalog

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == _LISTING_PATH:
            return self._catalog(int(request.url.params["page"]), int(request.url.params["per_page"]))
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


@dataclass(frozen=True, slots=True)
class ListingScenario:
    """Frozen setup: the routed mock and a client factory over it."""

    router: HabrListingRouter
    make_client: Callable[[], HabrClient]


@dataclass(frozen=True, slots=True)
class ListingOutcome:
    """Frozen outcome: the listing result plus the recorded request history."""

    result: Result[SearchListing, ClientError]
    requests: tuple[httpx.Request, ...]


def _rich_item() -> JsonBlob:
    return listing_item(123, salary={"from": 100000, "to": 200000, "currency": "usd"}, skills=("Python",))


def _rich_page(page: int, per_page: int) -> httpx.Response:
    del per_page
    if page != 1:
        return listing_page([], total_results=42, current_page=page)
    return listing_page([_rich_item()], total_results=42, current_page=1, per_page=5)


def _dense_page(page: int, per_page: int) -> httpx.Response:
    base = (page - 1) * per_page
    items = [listing_item(base + index, title=f"Vacancy {base + index}") for index in range(per_page)]
    return listing_page(items, total_results=100, current_page=page, per_page=per_page)


def _null_lists_page(page: int, per_page: int) -> httpx.Response:
    """One listing item whose ``locations``/``skills`` are JSON ``null``."""
    del per_page
    if page != 1:
        return listing_page([], total_results=1, current_page=page)
    return listing_page([listing_item(123, locations=None, skills=None)], total_results=1, current_page=1, per_page=5)


def _failing_second_page(page: int, per_page: int) -> httpx.Response:
    if page == 2:
        return httpx.Response(500, json={"error": "boom"})
    return _dense_page(page, per_page)


def _scenario(tmp_path: Path, catalog: CatalogResponder) -> ListingScenario:
    router = HabrListingRouter(catalog)
    data_dir = tmp_path / "data"
    entry = HabrSearchEntry(query="python", search_type=HabrSearchType.ALL)

    def make_client() -> HabrClient:
        return HabrClient(
            habr_deps(data_dir, profile_id_for(), RecordingSolver()),
            HabrServiceConfig(searches=(entry,)),
            http_transport=httpx.MockTransport(router),
            browser_driver=FakeHabrBrowserDriver(),
        )

    return ListingScenario(router=router, make_client=make_client)


@given("a Habr client configured for listing a rich page", target_fixture="listing_scenario")
def rich_listing_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, _rich_page)


@given("a Habr client configured for listing a dense catalog", target_fixture="listing_scenario")
def dense_listing_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, _dense_page)


@given("a Habr client configured for listing a page with null locations", target_fixture="listing_scenario")
def null_locations_listing_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, _null_lists_page)


@given("a Habr client configured for listing whose second page fails", target_fixture="listing_scenario")
def failing_listing_step(tmp_path: Path) -> ListingScenario:
    return _scenario(tmp_path, _failing_second_page)


async def _list(client: HabrClient, *, offset: int, limit: int, page_size: int) -> Result[SearchListing, ClientError]:
    try:
        return await client.list_vacancies(0, offset=offset, limit=limit, page_size=page_size)
    finally:
        await client.aclose()


@when(
    parsers.parse("positions {start:d} to {end:d} are listed with {page_size:d} items per page on entry 0"),
    target_fixture="listing_outcome",
)
def list_step(listing_scenario: ListingScenario, start: int, end: int, page_size: int) -> ListingOutcome:
    result = async_run(_list(listing_scenario.make_client(), offset=start, limit=end - start + 1, page_size=page_size))
    return ListingOutcome(result=result, requests=tuple(listing_scenario.router.requests))


def _listing(outcome: ListingOutcome) -> SearchListing:
    assert outcome.result.is_ok
    return outcome.result.unwrap()


def _listing_pages(outcome: ListingOutcome) -> list[str]:
    return [request.url.params["page"] for request in outcome.requests if request.url.path == _LISTING_PATH]


@then("the listing contains one short vacancy with decoded listing fields")
def assert_short_item_step(listing_outcome: ListingOutcome) -> None:
    items = _listing(listing_outcome).items
    assert len(items) == 1
    item = items[0]
    assert item.external_id == "123"
    assert item.title == "Python разработчик"
    assert item.url == "https://career.habr.com/vacancies/123"
    assert item.company == "Компания"
    assert item.area == "Москва"
    assert item.published_at == "2026-09-15T10:23:50+03:00"
    salary = item.salary
    assert salary is not None
    assert (salary.from_, salary.to) == (100000, 200000)
    assert salary.currency == "USD"
    assert salary.gross is False
    assert item.snippet_requirement is None
    assert item.snippet_responsibility is None


@then("the listing carries the board-reported total and a client-built search URL")
def assert_listing_metadata_step(listing_outcome: ListingOutcome) -> None:
    listing = _listing(listing_outcome)
    assert listing.found == 42
    assert listing.ui_url == _EXPECTED_UI_URL


@then("no vacancy detail is requested")
def assert_no_detail_step(listing_outcome: ListingOutcome) -> None:
    assert not any(request.url.path.startswith(_DETAIL_PREFIX) for request in listing_outcome.requests)


@then("the listing walks native pages 2 and 3 only")
def assert_listing_pages_step(listing_outcome: ListingOutcome) -> None:
    assert _listing_pages(listing_outcome) == ["2", "3"]


@then("the listing spans exactly the requested positions")
def assert_listing_span_step(listing_outcome: ListingOutcome) -> None:
    items = _listing(listing_outcome).items
    assert [item.external_id for item in items] == ["8", "9", "10", "11", "12"]


@then("the listing fails with the page error and no partial result")
def assert_listing_failure_step(listing_outcome: ListingOutcome) -> None:
    assert listing_outcome.result.is_err
    assert isinstance(listing_outcome.result.unwrap_err(), TransportError)


@then("the listing maps the null locations to a null area")
def assert_null_area_step(listing_outcome: ListingOutcome) -> None:
    items = _listing(listing_outcome).items
    assert len(items) == 1
    assert items[0].area is None
