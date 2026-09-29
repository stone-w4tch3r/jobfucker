"""BDD acceptance for the Habr search filter contract (test/AGENTS.md §3a, §3b).

State flows by fixture injection: a ``Given`` seeds a typed filter, the ``When``
returns the encoded wire model (or a rejection carrier), and ``Then`` steps only
assert. No transport is involved: this is the contract-to-wire bridge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError
from pytest_bdd import given, parsers, scenarios, then, when

from jobfucker.clients.habr.config import (
    HabrEmployment,
    HabrFilterConfig,
    HabrQualification,
    HabrSalaryCurrency,
    HabrSearchEntry,
    HabrSearchFilters,
    HabrSearchType,
    HabrSort,
    encode_search_type,
)

scenarios("bdd/filter_contract.feature")


@dataclass(frozen=True, slots=True)
class FilterConstruction:
    """Frozen outcome: whether constructing the invalid filter was rejected."""

    rejected: bool


@given("a default Habr filter", target_fixture="habr_filter")
def default_filter_step() -> HabrFilterConfig:
    return HabrFilterConfig()


@given("a Habr filter with every V1 field set", target_fixture="habr_filter")
def full_filter_step() -> HabrFilterConfig:
    return HabrFilterConfig(
        sort=HabrSort.DATE,
        qualification=HabrQualification.SENIOR,
        remote=True,
        with_salary=True,
        salary_from=100000,
        salary_currency=HabrSalaryCurrency.RUR,
        skills=(1, 2),
        locations=("c_1", "r_2"),
        employment=HabrEmployment.FULL_TIME,
    )


@given(parsers.parse("a Habr filter with qualification {grade}"), target_fixture="habr_filter")
def qualification_filter_step(grade: str) -> HabrFilterConfig:
    return HabrFilterConfig(qualification=HabrQualification(grade))


@given(parsers.parse("a Habr filter with sort {sort}"), target_fixture="habr_filter")
def sort_filter_step(sort: str) -> HabrFilterConfig:
    return HabrFilterConfig(sort=HabrSort(sort))


@given(parsers.parse("a Habr filter with currency {currency}"), target_fixture="habr_filter")
def currency_filter_step(currency: str) -> HabrFilterConfig:
    return HabrFilterConfig(salary_currency=HabrSalaryCurrency(currency))


@given(parsers.parse("a Habr filter with employment {employment}"), target_fixture="habr_filter")
def employment_filter_step(employment: str) -> HabrFilterConfig:
    return HabrFilterConfig(employment=HabrEmployment(employment))


@given(parsers.parse('a Habr filter with a bare location "{location}"'), target_fixture="filter_builder")
def bare_location_filter_step(location: str) -> Callable[[], HabrFilterConfig]:
    def build() -> HabrFilterConfig:
        return HabrFilterConfig(locations=(location,))

    return build


@given(parsers.parse("a Habr filter with skill id {skill_id:d}"), target_fixture="filter_builder")
def non_positive_skill_filter_step(skill_id: int) -> Callable[[], HabrFilterConfig]:
    def build() -> HabrFilterConfig:
        return HabrFilterConfig(skills=(skill_id,))

    return build


@given(parsers.parse("a Habr search entry with search type {kind}"), target_fixture="entry")
def search_entry_step(kind: str) -> HabrSearchEntry:
    return HabrSearchEntry(query="python", search_type=HabrSearchType(kind))


@when("the filter is constructed", target_fixture="construction")
def construct_filter_step(filter_builder: Callable[[], HabrFilterConfig]) -> FilterConstruction:
    try:
        filter_builder()
    except ValidationError:
        return FilterConstruction(rejected=True)
    return FilterConstruction(rejected=False)


@when("the filter is encoded to wire filters", target_fixture="wire")
def encode_filter_step(habr_filter: HabrFilterConfig) -> HabrSearchFilters:
    return habr_filter.to_search_filters()


@when("the entry's search type is encoded", target_fixture="wire_type")
def encode_search_type_step(entry: HabrSearchEntry) -> str:
    return encode_search_type(entry.search_type)


@then("the filter is rejected")
def assert_rejected_step(construction: FilterConstruction) -> None:
    assert construction.rejected


@then("the wire filter keeps the defaults")
def assert_defaults_step(wire: HabrSearchFilters) -> None:
    assert wire.sort == "relevance"
    assert wire.currency == "RUR"
    assert wire.qid is None
    assert wire.salary is None
    assert wire.skills == ()
    assert wire.locations == ()
    assert wire.employment_type is None
    assert wire.remote is False
    assert wire.with_salary is False


@then(parsers.parse("the wire sort is {sort}"))
def assert_sort_step(wire: HabrSearchFilters, sort: str) -> None:
    assert wire.sort == sort


@then(parsers.parse("the wire qid is {qid:d}"))
def assert_qid_step(wire: HabrSearchFilters, qid: int) -> None:
    assert wire.qid == qid


@then(parsers.parse("the wire currency is {code}"))
def assert_currency_step(wire: HabrSearchFilters, code: str) -> None:
    assert wire.currency == code


@then(parsers.parse("the wire employment type is {employment}"))
def assert_employment_step(wire: HabrSearchFilters, employment: str) -> None:
    assert wire.employment_type == employment


@then(parsers.parse("the wire salary is {amount:d}"))
def assert_salary_step(wire: HabrSearchFilters, amount: int) -> None:
    assert wire.salary == amount


@then(parsers.parse("the wire skills are {first:d} and {second:d}"))
def assert_skills_step(wire: HabrSearchFilters, first: int, second: int) -> None:
    assert wire.skills == (first, second)


@then(parsers.parse("the wire locations are {first} and {second}"))
def assert_locations_step(wire: HabrSearchFilters, first: str, second: str) -> None:
    assert wire.locations == (first, second)


@then("the wire filter marks remote")
def assert_remote_step(wire: HabrSearchFilters) -> None:
    assert wire.remote is True


@then("the wire filter marks with-salary")
def assert_with_salary_step(wire: HabrSearchFilters) -> None:
    assert wire.with_salary is True


@then(parsers.parse("the encoded search type is {kind}"))
def assert_search_type_step(wire_type: str, kind: str) -> None:
    assert wire_type == kind
