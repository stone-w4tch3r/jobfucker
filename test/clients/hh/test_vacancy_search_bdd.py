"""BDD acceptance coverage for slice-level HH vacancy search and enrichment.

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given`` seeds
a frozen ``SearchScenario`` (data-configured transport + filter set), the
``When`` returns a frozen ``SearchOutcome``, and ``Then`` steps only assert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import (
    BadRequestError,
    ClientError,
    ConfigurationError,
    SearchSlice,
    ServiceVacancyId,
)
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import (
    Currency,
    ExperienceLevel,
    HHFilterConfig,
    HHLegacyWorkParams,
    HHLocationFilters,
    HHOrderingFilters,
    HHPublishedFilters,
    HHQueryFilters,
    HHSalaryFilters,
    HHSearchEntry,
    HHServiceConfig,
    HHTargetingFilters,
    HHWorkArrangementFilters,
    SearchIn,
    SortBy,
    VacancyLabel,
    WorkFormat,
    WorkSchedule,
)
from jobfucker.testing.step_runner import async_run
from test.clients.hh.helpers import (
    CatalogItemPayload,
    JsonBlob,
    RecordingSolver,
    applicant_healthcheck,
    hh_client,
    json_response,
    seed_auth_state,
    vacancy_detail,
)

scenarios("bdd/vacancy_search.feature")

_PROFILE_ID = "vacancy-search-profile"

CatalogResponder = Callable[[int, int], httpx.Response]


class SearchTransport:
    """External-boundary fake: a dumb router over the scripted catalog pages.

    Detail payloads route by the requested id: ``123`` serves the rich canned
    vacancy, any other id serves the dense canned vacancy, so tests can assert
    exactly which items were enriched.
    """

    def __init__(self, *, catalog: CatalogResponder) -> None:
        self.requests: list[httpx.Request] = []
        self._catalog = catalog

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/me":
            return applicant_healthcheck()
        if request.url.path == "/vacancies":
            return self._catalog(int(request.url.params["page"]), int(request.url.params["per_page"]))
        if request.url.path.startswith("/vacancies/"):
            return self._detail(request.url.path.removeprefix("/vacancies/"))
        raise AssertionError(f"Unexpected HH request: {request.method} {request.url}")

    def _detail(self, vacancy_id: str) -> httpx.Response:
        if vacancy_id == "123":
            detail = vacancy_detail(
                vacancy_id,
                description="<p>Python &amp; APIs</p><ul><li>Первый</li><li>Второй</li></ul>",
                skills=("Python", "AsyncIO"),
                salary=JsonBlob({"from": 200000, "to": 300000, "currency": "RUR", "gross": False}),
            )
        else:
            detail = vacancy_detail(vacancy_id, name=f"Vacancy {vacancy_id}", description="<p>Dense canned vacancy</p>")
        return json_response(detail)


# --- Scripted catalog responders (one per setup, built from data) ------------


def _one_item(page: int, per_page: int) -> list[CatalogItemPayload]:
    del page, per_page
    return [CatalogItemPayload(id="123", name="Python разработчик", alternate_url="https://hh.ru/vacancy/123")]


def _no_items(page: int, per_page: int) -> list[CatalogItemPayload]:
    del page, per_page
    return []


def _dense_items(page: int, per_page: int) -> list[CatalogItemPayload]:
    return [
        CatalogItemPayload(
            id=f"d-{page * per_page + index}",
            name=f"Vacancy d-{page * per_page + index}",
            alternate_url=f"https://hh.ru/vacancy/d-{page * per_page + index}",
        )
        for index in range(per_page)
    ]


def _catalog(items_for: Callable[[int, int], list[CatalogItemPayload]]) -> CatalogResponder:
    def respond(page: int, per_page: int) -> httpx.Response:
        items = items_for(page, per_page)
        return json_response({"items": items, "found": len(items), "page": page, "pages": 4, "per_page": per_page})

    return respond


def _error_envelope(page: int, per_page: int) -> httpx.Response:
    del page, per_page
    return json_response({"errors": [{"type": "bad_argument", "value": "page"}]})


def _basic_filter() -> HHFilterConfig:
    return HHFilterConfig(
        location=HHLocationFilters(regions=(1, 40)),
        work_arrangement=HHWorkArrangementFilters(
            legacy_params=HHLegacyWorkParams(
                work_schedules=(WorkSchedule.FULL_DAY, WorkSchedule.REMOTE),
            ),
            required_experience=ExperienceLevel.BETWEEN_1_AND_3_YEARS,
        ),
        salary=HHSalaryFilters(only_with_salary=True),
    )


def _rich_filter() -> HHFilterConfig:
    return HHFilterConfig(
        query=HHQueryFilters(
            use_hh_query_language=False,
            fields_to_search_in=(SearchIn.NAME, SearchIn.COMPANY_NAME),
            exclude_words="стажер",
        ),
        ordering=HHOrderingFilters(sort_results_by=SortBy.PUBLICATION_TIME),
        work_arrangement=HHWorkArrangementFilters(
            work_formats=(WorkFormat.REMOTE, WorkFormat.HYBRID),
            legacy_params=HHLegacyWorkParams(work_schedules=(WorkSchedule.FULL_DAY, WorkSchedule.REMOTE)),
            required_experience=ExperienceLevel.BETWEEN_1_AND_3_YEARS,
        ),
        salary=HHSalaryFilters(from_=200_000, currency=Currency.RUR, only_with_salary=True),
        published=HHPublishedFilters(from_=date(2026, 8, 1), to=date(2026, 8, 20)),
        location=HHLocationFilters(regions=(1, 40)),
        targeting=HHTargetingFilters(
            professional_roles=(96,),
            vacancy_labels=(VacancyLabel.WITH_SALARY, VacancyLabel.ACCREDITED_IT),
        ),
    )


def _field_filter() -> HHFilterConfig:
    return HHFilterConfig(query=HHQueryFilters(fields_to_search_in=(SearchIn.NAME,)))


@dataclass(frozen=True, slots=True)
class SearchScenario:
    """Frozen setup: the client factory over the scripted transport and filters."""

    transport: SearchTransport
    make_client: Callable[[str], HHClient]


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Frozen outcome: the search result and the request history it produced."""

    listing: Result[SearchSlice, ClientError]
    requests: tuple[httpx.Request, ...]


def _scenario(
    tmp_path: Path, *, catalog: CatalogResponder, search_filter: HHFilterConfig | None = None
) -> SearchScenario:
    transport = SearchTransport(catalog=catalog)
    search_filter = search_filter if search_filter is not None else _basic_filter()
    data_dir = tmp_path / "data"
    seed_auth_state(data_dir, _PROFILE_ID, access_token="USER-search", refresh_token="refresh-search")

    def make_client(query: str) -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(resume_id="resume-1", searches=(HHSearchEntry(query=query, filter=search_filter),)),
            solver=RecordingSolver(),
        )

    return SearchScenario(transport=transport, make_client=make_client)


async def _search_once(
    client: HHClient,
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


def _search(
    scenario: SearchScenario,
    *,
    offset: int,
    limit: int,
    page_size: int,
    query: str = "python",
    exclude: frozenset[ServiceVacancyId] = frozenset(),
) -> SearchOutcome:
    listing = async_run(
        _search_once(scenario.make_client(query), offset=offset, limit=limit, page_size=page_size, exclude=exclude)
    )
    return SearchOutcome(listing=listing, requests=tuple(scenario.transport.requests))


@given("a healthy HH session and a catalog page with one vacancy", target_fixture="search_scenario")
def one_vacancy_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_catalog(_one_item))


@given("a healthy HH session and an empty catalog page", target_fixture="search_scenario")
def empty_page_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_catalog(_no_items))


@given("a healthy HH session with a rich advanced filter", target_fixture="search_scenario")
def rich_filter_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_catalog(_one_item), search_filter=_rich_filter())


@given("a healthy HH session with a search-field filter and no query", target_fixture="search_scenario")
def search_field_without_query_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_catalog(_one_item), search_filter=_field_filter())


@given("a healthy HH session and a catalog error envelope", target_fixture="search_scenario")
def error_envelope_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_error_envelope)


@given("a healthy HH session and a dense catalog", target_fixture="search_scenario")
def dense_catalog_step(tmp_path: Path) -> SearchScenario:
    return _scenario(tmp_path, catalog=_catalog(_dense_items))


@when(
    parsers.parse("positions {start:d} to {end:d} are searched with {page_size:d} items per page"),
    target_fixture="search_outcome",
)
def search_positions_step(search_scenario: SearchScenario, start: int, end: int, page_size: int) -> SearchOutcome:
    return _search(search_scenario, offset=start, limit=end - start + 1, page_size=page_size)


@when(
    parsers.parse(
        "positions {start:d} to {end:d} are searched with {page_size:d} items per page excluding {first} and {second}"
    ),
    target_fixture="search_outcome",
)
def search_excluding_step(
    search_scenario: SearchScenario, start: int, end: int, page_size: int, first: str, second: str
) -> SearchOutcome:
    excluded = frozenset({ServiceVacancyId(first), ServiceVacancyId(second)})
    return _search(search_scenario, offset=start, limit=end - start + 1, page_size=page_size, exclude=excluded)


@when(
    parsers.parse("positions {start:d} to {end:d} are searched with {page_size:d} items per page without a query"),
    target_fixture="search_outcome",
)
def search_without_query_step(search_scenario: SearchScenario, start: int, end: int, page_size: int) -> SearchOutcome:
    return _search(search_scenario, offset=start, limit=end - start + 1, page_size=page_size, query="")


def _catalog_requests(outcome: SearchOutcome) -> list[httpx.Request]:
    return [request for request in outcome.requests if request.url.path == "/vacancies"]


def _detail_ids(outcome: SearchOutcome) -> list[str]:
    """The vacancy ids detail-fetched, in order (the enrichment proof)."""
    return [
        request.url.path.removeprefix("/vacancies/")
        for request in outcome.requests
        if request.url.path.startswith("/vacancies/") and request.url.path != "/vacancies"
    ]


@then("HH receives native page 2 and the configured basic filters")
def native_page_and_filters_step(search_outcome: SearchOutcome) -> None:
    request = _catalog_requests(search_outcome)[0]
    assert request.url.params.get("text") == "python"
    assert request.url.params.get("page") == "2"
    assert request.url.params.get("per_page") == "5"
    assert request.url.params.get_list("area") == ["1", "40"]
    assert request.url.params.get_list("schedule") == ["fullDay", "remote"]
    assert request.url.params.get("experience") == "between1And3"
    assert request.url.params.get("only_with_salary") == "true"


@then("HH receives the exact configured filter set")
def exact_filter_set_step(search_outcome: SearchOutcome) -> None:
    # The full /vacancies query string is pinned exactly: nothing dropped, nothing extra.
    request = _catalog_requests(search_outcome)[0]
    assert request.url.params.multi_items() == [
        ("text", "python"),
        ("page", "0"),
        ("per_page", "5"),
        ("no_magic", "true"),
        ("search_field", "name"),
        ("search_field", "company_name"),
        ("order_by", "publication_time"),
        ("schedule", "fullDay"),
        ("schedule", "remote"),
        ("work_format", "REMOTE"),
        ("work_format", "HYBRID"),
        ("experience", "between1And3"),
        ("salary", "200000"),
        ("currency", "RUR"),
        ("only_with_salary", "true"),
        ("date_from", "2026-08-01"),
        ("date_to", "2026-08-20"),
        ("area", "1"),
        ("area", "40"),
        ("professional_role", "96"),
        ("excluded_text", "стажер"),
        ("label", "with_salary"),
        ("label", "accredited_it"),
    ]


@then("the vacancy is returned with normalized full details")
def enriched_vacancy_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.listing.is_ok
    vacancy = search_outcome.listing.unwrap().items[0]
    assert vacancy.external_id == "123"
    assert vacancy.title == "Python разработчик"
    assert vacancy.url == "https://hh.ru/vacancy/123"
    assert vacancy.company == "Компания"
    assert vacancy.description == "Python & APIs\nПервый\nВторой"
    assert vacancy.key_skills == ("Python", "AsyncIO")
    assert vacancy.salary is not None
    assert vacancy.salary.from_ == 200000
    assert vacancy.salary.to == 300000
    assert vacancy.salary.currency == "RUR"
    assert vacancy.salary.gross is False


@then("the search returns an empty vacancy list")
def empty_result_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.listing.is_ok
    listing = search_outcome.listing.unwrap()
    assert listing.items == ()
    assert listing.exhausted is True


@then("no vacancy detail is requested")
def no_detail_step(search_outcome: SearchOutcome) -> None:
    assert [request.url.path for request in search_outcome.requests] == ["/me", "/vacancies"]


@then("the search walk fails with a configuration error")
def configuration_error_step(search_outcome: SearchOutcome) -> None:
    # Page-fetch failures are partial slices (uniform contract: Err is reserved
    # for pre-scan failures); a deterministic config rejection fails on page 0.
    assert search_outcome.listing.is_ok
    listing = search_outcome.listing.unwrap()
    assert listing.items == ()
    assert listing.failure is not None
    assert isinstance(listing.failure.error, ConfigurationError)


@then("no vacancy catalog request is sent")
def no_catalog_step(search_outcome: SearchOutcome) -> None:
    assert [request.url.path for request in search_outcome.requests] == ["/me"]


@then("the search walk fails with a bad request error")
def bad_request_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.listing.is_ok
    listing = search_outcome.listing.unwrap()
    assert listing.items == ()
    assert listing.failure is not None
    assert isinstance(listing.failure.error, BadRequestError)


@then("HH walks native pages 1 and 2 only")
def dense_pages_step(search_outcome: SearchOutcome) -> None:
    # The slice [8, 13) at page_size 5 overlaps exactly pages 1 (5-9) and 2 (10-14).
    assert [request.url.params["page"] for request in _catalog_requests(search_outcome)] == ["1", "2"]


@then("exactly 6 vacancy details are requested")
def dense_details_step(search_outcome: SearchOutcome) -> None:
    # The over-enrichment regression proof: 6 slice items -> 6 detail GETs,
    # never one per listed item (10 would mean whole-page enrichment is back).
    assert _detail_ids(search_outcome) == [f"d-{position}" for position in range(8, 14)]


@then("the slice spans the requested positions")
def dense_slice_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.listing.is_ok
    listing = search_outcome.listing.unwrap()
    assert listing.offset == 8
    assert [vacancy.external_id for vacancy in listing.items] == [f"d-{position}" for position in range(8, 14)]
    assert (listing.scanned_start, listing.scanned_end) == (5, 15)
    assert listing.pages_scanned == 2
    assert listing.failure is None


@then("the slice contains only the new vacancies")
def excluded_items_step(search_outcome: SearchOutcome) -> None:
    assert search_outcome.listing.is_ok
    listing = search_outcome.listing.unwrap()
    assert [vacancy.external_id for vacancy in listing.items] == [f"d-{position}" for position in (1, 3, 4)]


@then("no detail request is made for the stored vacancies")
def excluded_details_step(search_outcome: SearchOutcome) -> None:
    # d-0 and d-2 were excluded before enrichment: their detail GETs never happen.
    assert _detail_ids(search_outcome) == [f"d-{position}" for position in (1, 3, 4)]
