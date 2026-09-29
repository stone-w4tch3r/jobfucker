"""BDD acceptance for the Habr vacancy search slice (test/AGENTS.md §3b).

The routed ``httpx.MockTransport`` serves page-aware listing and id-aware detail
responses; the ``When`` returns a frozen outcome carrying the ``Result`` and the
recorded request history, and ``Then`` steps assert on geometry, filter wires,
authorization, and the mapped contract fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import (
    ClientError,
    ConfigurationError,
    ProtocolError,
    SearchSlice,
    ServiceVacancyId,
    TransportError,
)
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import (
    HabrEmployment,
    HabrFilterConfig,
    HabrQualification,
    HabrSalaryCurrency,
    HabrSearchEntry,
    HabrSearchType,
    HabrServiceConfig,
    HabrSort,
)
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver
from test.clients.habr.helpers import (
    DEFAULT_LOGIN,
    DEFAULT_PASSWORD,
    JsonBlob,
    RecordingSolver,
    authorized_identity,
    habr_deps,
    html_response,
    listing_item,
    listing_page,
    profile_id_for,
    ssr_detail_page,
    ssr_page_with_mistyped_script,
)

scenarios("bdd/vacancy_search.feature")

_LISTING_PATH: Final = "/api/frontend/vacancies"
_IDENTITY_PATH: Final = "/api/frontend_v1/users/me"
_DETAIL_PREFIX: Final = "/vacancies/"

CatalogResponder = Callable[[int, int], httpx.Response]
DetailResponder = Callable[[str], httpx.Response]


class HabrSearchRouter:
    """Dumb router over the Habr listing/detail endpoints plus the identity probe."""

    def __init__(
        self,
        *,
        catalog: CatalogResponder,
        detail: DetailResponder,
        identity: httpx.Response | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._catalog = catalog
        self._detail = detail
        self._identity = identity

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == _IDENTITY_PATH:
            if self._identity is None:
                raise AssertionError("unexpected Habr identity request")
            return self._identity
        if path == _LISTING_PATH:
            return self._catalog(int(request.url.params["page"]), int(request.url.params["per_page"]))
        if path.startswith(_DETAIL_PREFIX):
            return self._detail(path.removeprefix(_DETAIL_PREFIX))
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


@dataclass(frozen=True, slots=True)
class SearchScenario:
    """Frozen setup: the routed mock and a client factory over it."""

    router: HabrSearchRouter
    make_client: Callable[[], HabrClient]


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Frozen outcome: the slice result plus the recorded request history."""

    result: Result[SearchSlice, ClientError]
    requests: tuple[httpx.Request, ...]


# --- Responders ---------------------------------------------------------------


def _rich_detail(vacancy_id: str) -> httpx.Response:
    """A detail page for id 123 with escaped HTML and a separate predicted salary."""
    if vacancy_id == "123":
        return html_response(
            ssr_detail_page(
                123,
                title="Python разработчик",
                description="<p>Python &amp; APIs</p><ul><li>Первый</li><li>Второй</li></ul>",
                skills=("Python", "AsyncIO"),
                salary={"from": 200000, "to": 300000, "currency": "usd"},
                predicted_salary={"from": 1, "to": 2, "currency": "rur"},
            )
        )
    return html_response(ssr_detail_page(int(vacancy_id), title=f"Vacancy {vacancy_id}"))


def _reordered_rich_detail(vacancy_id: str) -> httpx.Response:
    """The same rich detail, but with the SSR script's attributes reshuffled."""
    if vacancy_id == "123":
        return html_response(
            ssr_detail_page(
                123,
                title="Python разработчик",
                description="<p>Python &amp; APIs</p><ul><li>Первый</li><li>Второй</li></ul>",
                skills=("Python", "AsyncIO"),
                salary={"from": 200000, "to": 300000, "currency": "usd"},
                predicted_salary={"from": 1, "to": 2, "currency": "rur"},
                reordered_script=True,
            )
        )
    return html_response(ssr_detail_page(int(vacancy_id), title=f"Vacancy {vacancy_id}", reordered_script=True))


def _single_item_catalog(page: int, per_page: int) -> httpx.Response:
    if page != 1:
        return listing_page([], total_results=1, current_page=page, per_page=per_page)
    return listing_page([listing_item(123)], total_results=1, current_page=1, per_page=per_page)


def _null_lists_catalog(page: int, per_page: int) -> httpx.Response:
    """A single listing item whose ``locations``/``skills`` are JSON ``null``."""
    del per_page
    if page != 1:
        return listing_page([], total_results=1, current_page=page)
    return listing_page([listing_item(123, locations=None, skills=None)], total_results=1, current_page=1, per_page=5)


def _null_lists_detail(vacancy_id: str) -> httpx.Response:
    """A detail page whose ``locations``/``skills`` are JSON ``null``."""
    return html_response(ssr_detail_page(int(vacancy_id), locations=None, skills=None))


def _mistyped_ssr_detail(vacancy_id: str) -> httpx.Response:
    """A detail page with a ``data-ssr-state`` block that is not an application/json script."""
    return html_response(ssr_page_with_mistyped_script(int(vacancy_id)))


def _item_on_page(target_page: int, item_id: int) -> CatalogResponder:
    def catalog(page: int, per_page: int) -> httpx.Response:
        if page != target_page:
            return listing_page([], total_results=1, current_page=page)
        return listing_page([listing_item(item_id)], total_results=1, current_page=page, per_page=per_page)

    return catalog


def _dense_catalog(page: int, per_page: int) -> httpx.Response:
    base = (page - 1) * per_page
    items = [listing_item(base + index, title=f"Vacancy {base + index}") for index in range(per_page)]
    return listing_page(items, total_results=100, current_page=page, per_page=per_page)


# --- Scenario factory ---------------------------------------------------------


def _entry(
    *,
    search_type: HabrSearchType = HabrSearchType.ALL,
    filter_config: HabrFilterConfig | None = None,
) -> HabrSearchEntry:
    return HabrSearchEntry(
        query="python",
        search_type=search_type,
        filter=filter_config if filter_config is not None else HabrFilterConfig(),
    )


def _scenario(
    tmp_path: Path,
    *,
    catalog: CatalogResponder,
    detail: DetailResponder,
    entry: HabrSearchEntry,
    identity: httpx.Response | None = None,
    login: str = DEFAULT_LOGIN,
    password: str = DEFAULT_PASSWORD,
) -> SearchScenario:
    router = HabrSearchRouter(catalog=catalog, detail=detail, identity=identity)
    data_dir = tmp_path / "data"

    def make_client() -> HabrClient:
        return HabrClient(
            habr_deps(data_dir, profile_id_for(login), RecordingSolver(), login=login, password=password),
            HabrServiceConfig(searches=(entry,)),
            http_transport=httpx.MockTransport(router),
            browser_driver=FakeHabrBrowserDriver(),
        )

    return SearchScenario(router=router, make_client=make_client)


# --- Given --------------------------------------------------------------------


@given("a Habr client configured with an all-type search", target_fixture="search_scenario")
def all_search_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_item_on_page(3, 123), detail=_rich_detail, entry=_entry())


@given("a Habr client configured with an all-type search and a reordered SSR script", target_fixture="search_scenario")
def reordered_ssr_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_single_item_catalog, detail=_reordered_rich_detail, entry=_entry())


@given("a Habr client configured with an all-type search and a single page item", target_fixture="search_scenario")
def single_item_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_single_item_catalog, detail=_rich_detail, entry=_entry())


@given("a Habr client with a healthy session configured with a suitable search", target_fixture="search_scenario")
def suitable_search_step(tmp_path: Path) -> SearchScenario:
    return _scenario(
        tmp_path,
        catalog=_single_item_catalog,
        detail=_rich_detail,
        entry=_entry(search_type=HabrSearchType.SUITABLE),
        identity=authorized_identity(),
    )


@given("a Habr client without credentials configured with a suitable search", target_fixture="search_scenario")
def suitable_without_credentials_step(tmp_path: Path) -> SearchScenario:
    return _scenario(
        tmp_path,
        catalog=_single_item_catalog,
        detail=_rich_detail,
        entry=_entry(search_type=HabrSearchType.SUITABLE),
        login="",
        password="",
    )


@given("a Habr client configured with an all-type search and a dense catalog", target_fixture="search_scenario")
def dense_catalog_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_dense_catalog, detail=_rich_detail, entry=_entry())


@given(
    "a Habr client with a healthy session configured with a suitable search and a dense catalog",
    target_fixture="search_scenario",
)
def suitable_dense_catalog_step(tmp_path: Path) -> SearchScenario:
    return _scenario(
        tmp_path,
        catalog=_dense_catalog,
        detail=_rich_detail,
        entry=_entry(search_type=HabrSearchType.SUITABLE),
        identity=authorized_identity(),
    )


@given(
    "a Habr client with a healthy session configured with a suitable search whose pages report 20 items",
    target_fixture="search_scenario",
)
def suitable_mismatched_page_size_step(tmp_path: Path) -> SearchScenario:
    def catalog(page: int, per_page: int) -> httpx.Response:
        del per_page
        return listing_page([listing_item(1)], total_results=1, current_page=page, per_page=20)

    return _scenario(
        tmp_path,
        catalog=catalog,
        detail=_rich_detail,
        entry=_entry(search_type=HabrSearchType.SUITABLE),
        identity=authorized_identity(),
    )


@given("a Habr client configured with a rich all-type filter", target_fixture="search_scenario")
def rich_filter_step(tmp_path: Path) -> SearchScenario:
    filter_config = HabrFilterConfig(
        sort=HabrSort.DATE,
        qualification=HabrQualification.MIDDLE,
        remote=True,
        with_salary=True,
        salary_from=200000,
        salary_currency=HabrSalaryCurrency.EUR,
        skills=(446, 1241),
        locations=("c_678", "r_14068"),
        employment=HabrEmployment.FULL_TIME,
    )
    return _scenario(
        tmp_path,
        catalog=_single_item_catalog,
        detail=_rich_detail,
        entry=_entry(filter_config=filter_config),
    )


@given("a Habr client configured with an all-type search and a concealed vacancy", target_fixture="search_scenario")
def concealed_vacancy_step(tmp_path: Path) -> SearchScenario:
    predicted: JsonBlob = {"from": 1, "to": 2, "currency": "rur"}

    def catalog(page: int, per_page: int) -> httpx.Response:
        del per_page
        if page != 1:
            return listing_page([], total_results=1, current_page=page)
        return listing_page(
            [listing_item(456, company_title=None, salary=None, predicted_salary=predicted)],
            total_results=1,
            current_page=1,
            per_page=5,
        )

    def detail(vacancy_id: str) -> httpx.Response:
        return html_response(
            ssr_detail_page(int(vacancy_id), company_title=None, salary=None, predicted_salary=predicted)
        )

    return _scenario(tmp_path, catalog=catalog, detail=detail, entry=_entry())


@given("a Habr client configured with an all-type search and a short page", target_fixture="search_scenario")
def short_page_step(tmp_path: Path) -> SearchScenario:
    def catalog(page: int, per_page: int) -> httpx.Response:
        del per_page
        return listing_page([listing_item(1), listing_item(2)], total_results=2, current_page=max(page, 1), per_page=5)

    return _scenario(tmp_path, catalog=catalog, detail=_rich_detail, entry=_entry())


@given("a Habr client configured with an all-type search and an empty catalog", target_fixture="search_scenario")
def empty_catalog_step(tmp_path: Path) -> SearchScenario:
    def catalog(page: int, per_page: int) -> httpx.Response:
        return listing_page([], total_results=0, current_page=max(page, 1), per_page=per_page)

    return _scenario(tmp_path, catalog=catalog, detail=_rich_detail, entry=_entry())


def _not_found_past_first_catalog(page: int, per_page: int) -> httpx.Response:
    """Wire page 1 is fine; every page past it answers the past-total ``404``."""
    del per_page
    if page > 1:
        return httpx.Response(404, json={"error": "Not found"})
    return listing_page([listing_item(1)], total_results=1, current_page=1, per_page=5)


def _over_cap_catalog(page: int, per_page: int) -> httpx.Response:
    """Dense up to the accessible cap; wire pages at/after offset 1000 are empty ``200``."""
    if page >= 21:
        return listing_page([], total_results=1118, current_page=page, per_page=per_page, total_pages=19)
    base = (page - 1) * per_page
    items = [listing_item(base + index, title=f"Vacancy {base + index}") for index in range(per_page)]
    return listing_page(items, total_results=1118, current_page=page, per_page=per_page, total_pages=19)


@given(
    "a Habr client configured with an all-type search whose past-total page is not found",
    target_fixture="search_scenario",
)
def not_found_past_total_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_not_found_past_first_catalog, detail=_rich_detail, entry=_entry())


@given(
    "a Habr client configured with an all-type search and a catalog capped below the requested offset",
    target_fixture="search_scenario",
)
def over_cap_catalog_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_over_cap_catalog, detail=_rich_detail, entry=_entry())


@given("a Habr client configured with an all-type search whose second page fails", target_fixture="search_scenario")
def failing_second_page_step(tmp_path: Path) -> SearchScenario:
    def catalog(page: int, per_page: int) -> httpx.Response:
        if page == 2:
            return httpx.Response(500, json={"error": "boom"})
        return _dense_catalog(page, per_page)

    return _scenario(tmp_path, catalog=catalog, detail=_rich_detail, entry=_entry())


@given("a Habr client configured with an all-type search and a malformed catalog", target_fixture="search_scenario")
def malformed_catalog_step(tmp_path: Path) -> SearchScenario:
    def catalog(page: int, per_page: int) -> httpx.Response:
        del page, per_page
        return html_response("<html>nope</html>")

    return _scenario(tmp_path, catalog=catalog, detail=_rich_detail, entry=_entry())


@given(
    "a Habr client configured with an all-type search whose listing and detail lists are null",
    target_fixture="search_scenario",
)
def null_lists_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_null_lists_catalog, detail=_null_lists_detail, entry=_entry())


@given(
    "a Habr client configured with an all-type search whose detail script is not JSON",
    target_fixture="search_scenario",
)
def mistyped_ssr_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_single_item_catalog, detail=_mistyped_ssr_detail, entry=_entry())


# --- When ---------------------------------------------------------------------


async def _search(
    client: HabrClient,
    *,
    offset: int,
    limit: int,
    page_size: int,
    exclude: frozenset[ServiceVacancyId],
) -> Result[SearchSlice, ClientError]:
    try:
        return await client.search_vacancies(0, offset=offset, limit=limit, page_size=page_size, exclude=exclude)
    finally:
        await client.aclose()


@when(
    parsers.parse("positions {start:d} to {end:d} are searched with {page_size:d} items per page on entry 0"),
    target_fixture="search_outcome",
)
def search_step(search_scenario: SearchScenario, start: int, end: int, page_size: int) -> SearchOutcome:
    result = async_run(
        _search(
            search_scenario.make_client(),
            offset=start,
            limit=end - start + 1,
            page_size=page_size,
            exclude=frozenset(),
        )
    )
    return SearchOutcome(result=result, requests=tuple(search_scenario.router.requests))


@when(
    parsers.parse(
        "positions {start:d} to {end:d} are searched with {page_size:d} items per page "
        "on entry 0 excluding {first} and {second}"
    ),
    target_fixture="search_outcome",
)
def search_excluding_step(
    search_scenario: SearchScenario, start: int, end: int, page_size: int, first: str, second: str
) -> SearchOutcome:
    exclude = frozenset({ServiceVacancyId(first), ServiceVacancyId(second)})
    result = async_run(
        _search(
            search_scenario.make_client(),
            offset=start,
            limit=end - start + 1,
            page_size=page_size,
            exclude=exclude,
        )
    )
    return SearchOutcome(result=result, requests=tuple(search_scenario.router.requests))


# --- Then ---------------------------------------------------------------------


def _slice(outcome: SearchOutcome) -> SearchSlice:
    assert outcome.result.is_ok
    return outcome.result.unwrap()


def _listing_requests(outcome: SearchOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.url.path == _LISTING_PATH]


def _detail_ids(outcome: SearchOutcome) -> list[str]:
    return [
        request.url.path.removeprefix(_DETAIL_PREFIX)
        for request in outcome.requests
        if request.url.path.startswith(_DETAIL_PREFIX)
    ]


def _identity_requests(outcome: SearchOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.url.path == _IDENTITY_PATH]


@then("Habr receives native page 3 and the all search without a login")
def assert_page3_all_step(search_outcome: SearchOutcome) -> None:
    listing = _listing_requests(search_outcome)
    assert [request.url.params["page"] for request in listing] == ["3"]
    assert listing[0].url.params["type"] == "all"
    assert listing[0].url.params["q"] == "python"
    assert listing[0].url.params["per_page"] == "5"
    assert _identity_requests(search_outcome) == []


@then("the vacancy is returned with normalized full details")
def assert_rich_vacancy_step(search_outcome: SearchOutcome) -> None:
    items = _slice(search_outcome).items
    assert len(items) == 1
    vacancy = items[0]
    assert vacancy.external_id == "123"
    assert vacancy.title == "Python разработчик"
    assert vacancy.url == "https://career.habr.com/vacancies/123"
    assert vacancy.company == "Компания"
    assert vacancy.description == "Python & APIs\nПервый\nВторой"
    assert vacancy.key_skills == ("Python", "AsyncIO")
    salary = vacancy.salary
    assert salary is not None
    assert (salary.from_, salary.to) == (200000, 300000)
    assert salary.currency == "USD"  # uppercased, never the predicted 1..2 RUR
    assert salary.gross is False
    assert vacancy.has_hh_test is None


@then("Habr receives native page 1 and the suitable search")
def assert_suitable_step(search_outcome: SearchOutcome) -> None:
    listing = _listing_requests(search_outcome)
    assert [request.url.params["page"] for request in listing] == ["1"]
    assert listing[0].url.params["type"] == "suitable"


@then("the identity endpoint was checked once")
def assert_identity_once_step(search_outcome: SearchOutcome) -> None:
    assert len(_identity_requests(search_outcome)) == 1


@then("the search fails with a configuration error")
def assert_configuration_error_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.result.is_err
    assert isinstance(search_outcome.result.unwrap_err(), ConfigurationError)


@then("no Habr request is sent")
def assert_no_request_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.requests == ()


@then("Habr receives per page 50 on native page 1")
def assert_clamped_page_size_step(search_outcome: SearchOutcome) -> None:
    listing = _listing_requests(search_outcome)
    assert listing[0].url.params["per_page"] == "50"
    assert listing[0].url.params["page"] == "1"


@then("Habr requests per page 25 on native pages 1 and 2")
def assert_suitable_stride_step(search_outcome: SearchOutcome) -> None:
    listing = _listing_requests(search_outcome)
    assert [request.url.params["per_page"] for request in listing] == ["25", "25"]
    assert [request.url.params["page"] for request in listing] == ["1", "2"]


@then("Habr walks native pages 2 and 3 only")
def assert_walked_pages_step(search_outcome: SearchOutcome) -> None:
    assert [request.url.params["page"] for request in _listing_requests(search_outcome)] == ["2", "3"]


@then("exactly 5 vacancy details are requested")
def assert_detail_count_step(search_outcome: SearchOutcome) -> None:
    assert _detail_ids(search_outcome) == ["8", "9", "10", "11", "12"]


@then("the slice spans the requested positions")
def assert_slice_span_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert [item.external_id for item in slice_.items] == ["8", "9", "10", "11", "12"]
    assert (slice_.scanned_start, slice_.scanned_end) == (5, 15)
    assert slice_.pages_scanned == 2
    assert slice_.failure is None


@then("the slice contains only the new vacancies")
def assert_excluded_slice_step(search_outcome: SearchOutcome) -> None:
    assert [item.external_id for item in _slice(search_outcome).items] == ["9", "11", "12"]


@then("no detail request is made for the stored vacancies")
def assert_no_stored_detail_step(search_outcome: SearchOutcome) -> None:
    assert _detail_ids(search_outcome) == ["9", "11", "12"]


@then("Habr receives the exact configured filter set")
def assert_exact_filters_step(search_outcome: SearchOutcome) -> None:
    request = _listing_requests(search_outcome)[0]
    assert request.url.params.multi_items() == [
        ("q", "python"),
        ("type", "all"),
        ("page", "1"),
        ("per_page", "5"),
        ("sort", "date"),
        ("qid", "4"),
        ("remote", "true"),
        ("with_salary", "true"),
        ("salary", "200000"),
        ("currency", "EUR"),
        ("skills[]", "446"),
        ("skills[]", "1241"),
        ("locations[]", "c_678"),
        ("locations[]", "r_14068"),
        ("employment_type", "full_time"),
    ]


@then("the concealed vacancy maps to a null company and a null salary")
def assert_concealed_step(search_outcome: SearchOutcome) -> None:
    items = _slice(search_outcome).items
    assert len(items) == 1
    assert items[0].company is None
    assert items[0].salary is None


@then("the short page exhausts the listing")
def assert_short_page_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert slice_.exhausted is True
    assert len(slice_.items) == 2


@then("the empty page exhausts the listing")
def assert_empty_page_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert slice_.exhausted is True
    assert slice_.items == ()
    assert slice_.pages_scanned == 1


@then("the not-found page exhausts the listing")
def assert_not_found_exhausts_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert slice_.exhausted is True
    assert slice_.items == ()
    assert slice_.failure is None


@then("the over-cap window exhausts the listing")
def assert_over_cap_exhausts_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert slice_.exhausted is True
    assert slice_.items == ()
    assert slice_.failure is None
    assert [request.url.params["page"] for request in _listing_requests(search_outcome)] == ["21"]


@then("the search page is a partial slice with a transport failure")
def assert_partial_transport_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert [item.external_id for item in slice_.items] == ["0", "1", "2", "3", "4"]
    failure = slice_.failure
    assert failure is not None
    assert failure.page == 1
    assert isinstance(failure.error, TransportError)


@then("the search page is a partial slice with a protocol failure")
def assert_partial_protocol_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert slice_.items == ()
    failure = slice_.failure
    assert failure is not None
    assert failure.page == 0
    assert isinstance(failure.error, ProtocolError)


@then("the vacancy maps the null lists to no key skills")
def assert_null_lists_step(search_outcome: SearchOutcome) -> None:
    slice_ = _slice(search_outcome)
    assert len(slice_.items) == 1
    assert slice_.items[0].key_skills == ()
    assert slice_.failure is None
