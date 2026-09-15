"""Contract surface: friendly ``HHFilterConfig`` names in, exact HH params out.

Every test drives the real user path (contract model → ``_search_params``), so
a renamed field, a swapped mapping value, or a dropped wiki param fails here.
Wire-vocabulary pinning against the wiki lives in ``test_search_params.py``.
"""

import json
from collections.abc import Callable
from datetime import date
from typing import Final

import pytest
from pydantic import ValidationError

from jobfucker.clients.base import BadRequestError, ConfigurationError
from jobfucker.clients.hh.config import (
    _CURRENCY_WIRE,  # pyright: ignore[reportPrivateUsage]
    _EDUCATION_WIRE,  # pyright: ignore[reportPrivateUsage]
    _EMPLOYMENT_FORM_WIRE,  # pyright: ignore[reportPrivateUsage]
    _EMPLOYMENT_WIRE,  # pyright: ignore[reportPrivateUsage]
    _EXPERIENCE_WIRE,  # pyright: ignore[reportPrivateUsage]
    _HOURS_PER_DAY_WIRE,  # pyright: ignore[reportPrivateUsage]
    _PART_TIME_WIRE,  # pyright: ignore[reportPrivateUsage]
    _SALARY_PAID_WIRE,  # pyright: ignore[reportPrivateUsage]
    _SALARY_UNIT_WIRE,  # pyright: ignore[reportPrivateUsage]
    _SEARCH_IN_WIRE,  # pyright: ignore[reportPrivateUsage]
    _SHIFT_ROTATION_WIRE,  # pyright: ignore[reportPrivateUsage]
    _SORT_BY_WIRE,  # pyright: ignore[reportPrivateUsage]
    _VACANCY_LABEL_WIRE,  # pyright: ignore[reportPrivateUsage]
    _WORK_FORMAT_WIRE,  # pyright: ignore[reportPrivateUsage]
    _WORK_SCHEDULE_WIRE,  # pyright: ignore[reportPrivateUsage]
    Currency,
    EducationLevel,
    EmploymentForm,
    EmploymentType,
    ExperienceLevel,
    HHDistancePoint,
    HHFilterConfig,
    HHGeoCorner,
    HHLegacyWorkParams,
    HHLocationFilters,
    HHOrderingFilters,
    HHPublishedFilters,
    HHQueryFilters,
    HHResponseExtras,
    HHSalaryFilters,
    HHSearchEntry,
    HHSearchMode,
    HHServiceConfig,
    HHTargetingFilters,
    HHWorkArrangementFilters,
    HoursPerDay,
    PartTimeOption,
    SalaryPaid,
    SalaryUnit,
    SearchIn,
    ShiftRotation,
    SortBy,
    VacancyLabel,
    WireCurrency,
    WireEducation,
    WireEmployment,
    WireEmploymentForm,
    WireExperience,
    WireLabel,
    WireOrderBy,
    WirePartTime,
    WireSalaryFrequency,
    WireSalaryMode,
    WireSchedule,
    WireSearchField,
    WireWorkFormat,
    WireWorkingHours,
    WireWorkScheduleByDays,
    WorkFormat,
    WorkSchedule,
)
from jobfucker.clients.hh.search import _search_params  # pyright: ignore[reportPrivateUsage]
from jobfucker.clients.hh.transport import FormFields
from jobfucker.config import dump_service_section, load_service_section

_SECTION = HHServiceConfig(resume_id="resume-1", searches=(HHSearchEntry(query="python"),))
_BASE_PARAMS: FormFields = (("text", "python"), ("page", "0"), ("per_page", "20"))


def _encoded(filters: HHFilterConfig) -> FormFields:
    """Encode contract filters through the same pure encoder the search service uses."""
    section = HHServiceConfig(resume_id=_SECTION.resume_id, searches=(HHSearchEntry(query="python", filter=filters),))
    result = _search_params(
        section, section.searches[0].filter.to_search_filters(), query="python", page=0, per_page=20
    )
    assert result.is_ok
    return result.unwrap()


# --- Contract↔wire parity ----------------------------------------------------


def test_mapping_tables_cover_the_wire_vocabulary_exactly() -> None:
    """Every table's values equal the full wire Literal set — no wiki value unmapped."""
    assert set(_SEARCH_IN_WIRE.values()) == set(WireSearchField.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_SORT_BY_WIRE.values()) == set(WireOrderBy.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_WORK_SCHEDULE_WIRE.values()) == set(WireSchedule.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_WORK_FORMAT_WIRE.values()) == set(WireWorkFormat.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_EMPLOYMENT_WIRE.values()) == set(WireEmployment.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_EMPLOYMENT_FORM_WIRE.values()) == set(WireEmploymentForm.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_EXPERIENCE_WIRE.values()) == set(WireExperience.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_EDUCATION_WIRE.values()) == set(WireEducation.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_SHIFT_ROTATION_WIRE.values()) == set(WireWorkScheduleByDays.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_HOURS_PER_DAY_WIRE.values()) == set(WireWorkingHours.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_PART_TIME_WIRE.values()) == set(WirePartTime.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_CURRENCY_WIRE.values()) == set(WireCurrency.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_SALARY_PAID_WIRE.values()) == set(WireSalaryFrequency.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_SALARY_UNIT_WIRE.values()) == set(WireSalaryMode.__args__)  # pyright: ignore[reportAttributeAccessIssue]
    assert set(_VACANCY_LABEL_WIRE.values()) == set(WireLabel.__args__)  # pyright: ignore[reportAttributeAccessIssue]


# --- Per-member pairing: every contract value encodes onto its wire param ----
# Expected wire values are spelled as literals here (NOT read from the tables)
# so an in-table value swap fails these tests. Table-vs-wiki completeness is
# pinned separately in test_mapping_tables_cover_the_wire_vocabulary_exactly.


def test_search_in_members_encode() -> None:
    expected: Final[dict[SearchIn, str]] = {
        SearchIn.NAME: "name",
        SearchIn.COMPANY_NAME: "company_name",
        SearchIn.DESCRIPTION: "description",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(query=HHQueryFilters(fields_to_search_in=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("search_field", wire))


def test_sort_by_members_encode() -> None:
    plain: Final[dict[SortBy, str]] = {
        SortBy.PUBLICATION_TIME: "publication_time",
        SortBy.SALARY_HIGH_TO_LOW: "salary_desc",
        SortBy.SALARY_LOW_TO_HIGH: "salary_asc",
    }
    assert _encoded(HHFilterConfig(ordering=HHOrderingFilters(sort_results_by=SortBy.RELEVANCE))) == _BASE_PARAMS
    for member, wire in plain.items():
        filters = HHFilterConfig(ordering=HHOrderingFilters(sort_results_by=member))
        assert _encoded(filters) == (*_BASE_PARAMS, ("order_by", wire))
    distance = HHFilterConfig(
        ordering=HHOrderingFilters(
            sort_results_by=SortBy.DISTANCE, distance_from_point=HHDistancePoint(lat=1.5, lng=2.5)
        )
    )
    assert _encoded(distance) == (
        *_BASE_PARAMS,
        ("order_by", "distance"),
        ("sort_point_lat", "1.5"),
        ("sort_point_lng", "2.5"),
    )


def test_work_schedule_members_encode() -> None:
    expected: Final[dict[WorkSchedule, str]] = {
        WorkSchedule.FULL_DAY: "fullDay",
        WorkSchedule.SHIFT: "shift",
        WorkSchedule.FLEXIBLE: "flexible",
        WorkSchedule.REMOTE: "remote",
        WorkSchedule.FLY_IN_FLY_OUT: "flyInFlyOut",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(
            work_arrangement=HHWorkArrangementFilters(legacy_params=HHLegacyWorkParams(work_schedules=(member,)))
        )
        assert _encoded(filters) == (*_BASE_PARAMS, ("schedule", wire))


def test_work_format_members_encode() -> None:
    expected: Final[dict[WorkFormat, str]] = {
        WorkFormat.ON_SITE: "ON_SITE",
        WorkFormat.REMOTE: "REMOTE",
        WorkFormat.HYBRID: "HYBRID",
        WorkFormat.FIELD_WORK: "FIELD_WORK",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(work_formats=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("work_format", wire))


def test_employment_type_members_encode() -> None:
    expected: Final[dict[EmploymentType, str]] = {
        EmploymentType.FULL: "full",
        EmploymentType.PART: "part",
        EmploymentType.PROJECT: "project",
        EmploymentType.VOLUNTEER: "volunteer",
        EmploymentType.PROBATION: "probation",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(
            work_arrangement=HHWorkArrangementFilters(legacy_params=HHLegacyWorkParams(employment_types=(member,)))
        )
        assert _encoded(filters) == (*_BASE_PARAMS, ("employment", wire))


def test_employment_form_members_encode() -> None:
    expected: Final[dict[EmploymentForm, str]] = {
        EmploymentForm.FULL: "FULL",
        EmploymentForm.PART: "PART",
        EmploymentForm.PROJECT: "PROJECT",
        EmploymentForm.FLY_IN_FLY_OUT: "FLY_IN_FLY_OUT",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(employment_forms=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("employment_form", wire))


def test_experience_members_encode() -> None:
    expected: Final[dict[ExperienceLevel, str]] = {
        ExperienceLevel.NO_EXPERIENCE: "noExperience",
        ExperienceLevel.BETWEEN_1_AND_3_YEARS: "between1And3",
        ExperienceLevel.BETWEEN_3_AND_6_YEARS: "between3And6",
        ExperienceLevel.MORE_THAN_6_YEARS: "moreThan6",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(required_experience=member))
        assert _encoded(filters) == (*_BASE_PARAMS, ("experience", wire))


def test_education_members_encode() -> None:
    expected: Final[dict[EducationLevel, str]] = {
        EducationLevel.ANY: "not_required_or_not_specified",
        EducationLevel.SPECIAL_SECONDARY: "special_secondary",
        EducationLevel.HIGHER: "higher",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(required_education=member))
        assert _encoded(filters) == (*_BASE_PARAMS, ("education", wire))


def test_shift_rotation_members_encode() -> None:
    expected: Final[dict[ShiftRotation, str]] = {
        ShiftRotation.SIX_ON_ONE_OFF: "SIX_ON_ONE_OFF",
        ShiftRotation.FIVE_ON_TWO_OFF: "FIVE_ON_TWO_OFF",
        ShiftRotation.FOUR_ON_THREE_OFF: "FOUR_ON_THREE_OFF",
        ShiftRotation.FOUR_ON_TWO_OFF: "FOUR_ON_TWO_OFF",
        ShiftRotation.THREE_ON_THREE_OFF: "THREE_ON_THREE_OFF",
        ShiftRotation.THREE_ON_TWO_OFF: "THREE_ON_TWO_OFF",
        ShiftRotation.TWO_ON_TWO_OFF: "TWO_ON_TWO_OFF",
        ShiftRotation.TWO_ON_ONE_OFF: "TWO_ON_ONE_OFF",
        ShiftRotation.ONE_ON_THREE_OFF: "ONE_ON_THREE_OFF",
        ShiftRotation.ONE_ON_TWO_OFF: "ONE_ON_TWO_OFF",
        ShiftRotation.WEEKENDS_ONLY: "WEEKEND",
        ShiftRotation.FOUR_ON_FOUR_OFF: "FOUR_ON_FOUR_OFF",
        ShiftRotation.FLEXIBLE: "FLEXIBLE",
        ShiftRotation.OTHER: "OTHER",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(shift_rotations=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("work_schedule_by_days", wire))


def test_hours_per_day_members_encode() -> None:
    expected: Final[dict[HoursPerDay, str]] = {
        HoursPerDay.HOURS_2: "HOURS_2",
        HoursPerDay.HOURS_3: "HOURS_3",
        HoursPerDay.HOURS_4: "HOURS_4",
        HoursPerDay.HOURS_5: "HOURS_5",
        HoursPerDay.HOURS_6: "HOURS_6",
        HoursPerDay.HOURS_7: "HOURS_7",
        HoursPerDay.HOURS_8: "HOURS_8",
        HoursPerDay.HOURS_9: "HOURS_9",
        HoursPerDay.HOURS_10: "HOURS_10",
        HoursPerDay.HOURS_11: "HOURS_11",
        HoursPerDay.HOURS_12: "HOURS_12",
        HoursPerDay.AROUND_THE_CLOCK: "HOURS_24",
        HoursPerDay.FLEXIBLE: "FLEXIBLE",
        HoursPerDay.OTHER: "OTHER",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(hours_per_day=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("working_hours", wire))


def test_part_time_members_encode() -> None:
    expected: Final[dict[PartTimeOption, str]] = {
        PartTimeOption.PROJECT_WORK: "employment_project",
        PartTimeOption.PART_TIME_EMPLOYMENT: "employment_part",
        PartTimeOption.TEMPORARY_ONLY: "temporary_job_true",
        PartTimeOption.FOUR_TO_SIX_HOURS_A_DAY: "from_four_to_six_hours_in_a_day",
        PartTimeOption.WEEKENDS_ONLY: "only_saturday_and_sunday",
        PartTimeOption.START_AFTER_4PM: "start_after_sixteen",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(work_arrangement=HHWorkArrangementFilters(part_time_options=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("part_time", wire))


def test_currency_members_encode() -> None:
    expected: Final[dict[Currency, str]] = {
        Currency.RUR: "RUR",
        Currency.USD: "USD",
        Currency.EUR: "EUR",
        Currency.KZT: "KZT",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(salary=HHSalaryFilters(from_=1, currency=member))
        assert _encoded(filters) == (*_BASE_PARAMS, ("salary", "1"), ("currency", wire))


def test_salary_paid_members_encode() -> None:
    expected: Final[dict[SalaryPaid, str]] = {
        SalaryPaid.DAILY: "DAILY",
        SalaryPaid.WEEKLY: "WEEKLY",
        SalaryPaid.TWICE_A_MONTH: "TWICE_PER_MONTH",
        SalaryPaid.MONTHLY: "MONTHLY",
        SalaryPaid.PER_PROJECT: "PER_PROJECT",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(salary=HHSalaryFilters(paid=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("salary_frequency", wire))


def test_salary_unit_members_encode() -> None:
    expected: Final[dict[SalaryUnit, str]] = {
        SalaryUnit.PER_MONTH: "MONTH",
        SalaryUnit.PER_SHIFT: "SHIFT",
        SalaryUnit.PER_HOUR: "HOUR",
        SalaryUnit.PER_FLY_IN_FLY_OUT: "FLY_IN_FLY_OUT",
        SalaryUnit.PER_SERVICE: "SERVICE",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(salary=HHSalaryFilters(measured_in=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("salary_mode", wire))


def test_vacancy_label_members_encode() -> None:
    expected: Final[dict[VacancyLabel, str]] = {
        VacancyLabel.WITH_SALARY: "with_salary",
        VacancyLabel.DIRECT_EMPLOYER: "not_from_agency",
        VacancyLabel.WITH_OFFICE_ADDRESS: "with_address",
        VacancyLabel.ACCEPTS_TEENS: "accept_kids",
        VacancyLabel.ACCREDITED_IT: "accredited_it",
        VacancyLabel.INTERNSHIP: "internship",
        VacancyLabel.NIGHT_SHIFTS: "night_shifts",
        VacancyLabel.ACCEPTS_LABOR_CONTRACT: "accept_labor_contract",
        VacancyLabel.LOW_PERFORMANCE: "low_performance",
    }
    for member, wire in expected.items():
        filters = HHFilterConfig(targeting=HHTargetingFilters(vacancy_labels=(member,)))
        assert _encoded(filters) == (*_BASE_PARAMS, ("label", wire))


def test_rectangular_area_encodes_to_the_four_corner_params() -> None:
    filters = HHFilterConfig(
        location=HHLocationFilters(rectangular_area=(HHGeoCorner(lat=55.4, lng=37.2), HHGeoCorner(lat=56.0, lng=38.0)))
    )
    assert _encoded(filters) == (
        *_BASE_PARAMS,
        ("bottom_left_lat", "55.4"),
        ("bottom_left_lng", "37.2"),
        ("top_right_lat", "56.0"),
        ("top_right_lng", "38.0"),
    )


def test_remaining_pass_through_families_encode() -> None:
    """Scalar/repeated families with no value remapping, pinned end-to-end."""
    filters = HHFilterConfig(
        query=HHQueryFilters(use_hh_query_language=False),
        response_extras=HHResponseExtras(
            include_clusters=True, include_arguments_description=True, include_responses_count=True
        ),
        salary=HHSalaryFilters(from_=200_000, only_with_salary=True),
        published=HHPublishedFilters(within_days=30),
        location=HHLocationFilters(regions=(1, 40), districts=(117,), metro_stations=("1.118",)),
        targeting=HHTargetingFilters(
            professional_roles=(96,), industries=(7,), employers=(1455,), exclude_employers=(391,), saved_search="ss-1"
        ),
    )
    assert _encoded(filters) == (
        *_BASE_PARAMS,
        ("no_magic", "true"),
        ("clusters", "true"),
        ("describe_arguments", "true"),
        ("responses_count_enabled", "true"),
        ("salary", "200000"),
        ("only_with_salary", "true"),
        ("period", "30"),
        ("area", "1"),
        ("area", "40"),
        ("metro", "1.118"),
        ("district", "117"),
        ("professional_role", "96"),
        ("industry", "7"),
        ("employer_id", "1455"),
        ("excluded_employer_id", "391"),
        ("saved_search_id", "ss-1"),
    )


# --- Defaults, omissions, context checks -------------------------------------


def test_use_hh_query_language_inverts_to_no_magic() -> None:
    assert HHFilterConfig().to_search_filters().no_magic is False  # absent → query language ON
    assert HHFilterConfig(query=HHQueryFilters(use_hh_query_language=False)).to_search_filters().no_magic is True


def test_empty_contract_encodes_only_core_params() -> None:
    assert _encoded(HHFilterConfig()) == _BASE_PARAMS


def test_explicit_false_booleans_are_omitted() -> None:
    filters = HHFilterConfig(
        salary=HHSalaryFilters(only_with_salary=False),
        work_arrangement=HHWorkArrangementFilters(temporary_jobs_only=False),
        response_extras=HHResponseExtras(include_clusters=False),
    )
    assert _encoded(filters) == _BASE_PARAMS


def test_list_values_are_repeated_not_comma_joined() -> None:
    encoded = _encoded(
        HHFilterConfig(
            location=HHLocationFilters(regions=(1, 40)),
            work_arrangement=HHWorkArrangementFilters(
                legacy_params=HHLegacyWorkParams(work_schedules=(WorkSchedule.FULL_DAY, WorkSchedule.REMOTE))
            ),
        )
    )
    assert encoded.count(("area", "1")) == 1
    assert encoded.count(("area", "40")) == 1
    assert all("," not in value for _, value in encoded)


def test_empty_query_without_search_field_is_sent_verbatim() -> None:
    result = _search_params(_SECTION, _SECTION.searches[0].filter.to_search_filters(), query="", page=0, per_page=20)
    assert result.is_ok
    assert result.unwrap() == (("text", ""), ("page", "0"), ("per_page", "20"))


def test_search_field_without_query_fails_closed() -> None:
    filters = HHFilterConfig(query=HHQueryFilters(fields_to_search_in=(SearchIn.NAME,)))
    section = HHServiceConfig(resume_id=_SECTION.resume_id, searches=(HHSearchEntry(query="", filter=filters),))
    result = _search_params(section, section.searches[0].filter.to_search_filters(), query="", page=0, per_page=20)
    assert result.is_err
    assert isinstance(result.unwrap_err(), ConfigurationError)


def test_resume_similar_rejects_saved_search() -> None:
    section = HHServiceConfig(
        resume_id="resume-1",
        search_mode=HHSearchMode.RESUME_SIMILAR,
        searches=(
            HHSearchEntry(
                query="python",
                filter=HHFilterConfig(targeting=HHTargetingFilters(saved_search="ss-1")),
            ),
        ),
    )
    result = _search_params(
        section, section.searches[0].filter.to_search_filters(), query="python", page=0, per_page=20
    )
    assert result.is_err
    assert isinstance(result.unwrap_err(), ConfigurationError)


def test_resume_similar_accepts_the_shared_filter_surface() -> None:
    filters = HHFilterConfig(
        location=HHLocationFilters(regions=(1,)),
        work_arrangement=HHWorkArrangementFilters(
            legacy_params=HHLegacyWorkParams(work_schedules=(WorkSchedule.REMOTE,)),
            temporary_jobs_only=True,
        ),
        response_extras=HHResponseExtras(include_arguments_description=True, include_responses_count=True),
        query=HHQueryFilters(exclude_words="стажер"),
    )
    section = HHServiceConfig(
        resume_id="resume-1",
        search_mode=HHSearchMode.RESUME_SIMILAR,
        searches=(HHSearchEntry(query="python", filter=filters),),
    )
    result = _search_params(
        section, section.searches[0].filter.to_search_filters(), query="python", page=0, per_page=20
    )
    assert result.is_ok
    assert result.unwrap() == (
        *_BASE_PARAMS,
        ("describe_arguments", "true"),
        ("responses_count_enabled", "true"),
        ("schedule", "remote"),
        ("accept_temporary", "true"),
        ("area", "1"),
        ("excluded_text", "стажер"),
    )


@pytest.mark.parametrize(("page", "per_page"), [(-1, 20), (0, 0), (0, 101)])
def test_paging_invariants_fail_closed(page: int, per_page: int) -> None:
    result = _search_params(
        _SECTION, _SECTION.searches[0].filter.to_search_filters(), query="python", page=page, per_page=per_page
    )
    assert result.is_err
    assert isinstance(result.unwrap_err(), BadRequestError)


# --- Contract cross-field rules -----------------------------------------------


@pytest.mark.parametrize(
    "build_filters",
    [
        lambda: HHFilterConfig(ordering=HHOrderingFilters(sort_results_by=SortBy.DISTANCE)),
        lambda: HHFilterConfig(ordering=HHOrderingFilters(distance_from_point=HHDistancePoint(lat=1.0, lng=2.0))),
        lambda: HHFilterConfig(
            ordering=HHOrderingFilters(
                sort_results_by=SortBy.PUBLICATION_TIME, distance_from_point=HHDistancePoint(lat=1.0, lng=2.0)
            )
        ),
        lambda: HHFilterConfig(published=HHPublishedFilters(within_days=30, from_=date(2026, 8, 1))),
        lambda: HHFilterConfig(published=HHPublishedFilters(within_days=30, to=date(2026, 8, 20))),
        lambda: HHFilterConfig(published=HHPublishedFilters(to=date(2026, 8, 20))),
    ],
)
def test_contract_cross_field_combinations_fail_at_construction(build_filters: Callable[[], HHFilterConfig]) -> None:
    with pytest.raises(ValidationError):
        build_filters()


def test_contract_cross_field_valid_combinations_construct() -> None:
    HHFilterConfig(
        ordering=HHOrderingFilters(
            sort_results_by=SortBy.DISTANCE, distance_from_point=HHDistancePoint(lat=1.0, lng=2.0)
        )
    )
    HHFilterConfig(published=HHPublishedFilters(within_days=30))
    HHFilterConfig(published=HHPublishedFilters(from_=date(2026, 8, 1)))
    HHFilterConfig(published=HHPublishedFilters(from_=date(2026, 8, 1), to=date(2026, 8, 20)))


def test_geo_ranges_are_validated() -> None:
    with pytest.raises(ValidationError):
        HHFilterConfig(ordering=HHOrderingFilters(distance_from_point=HHDistancePoint(lat=91.0, lng=2.0)))
    with pytest.raises(ValidationError):
        HHFilterConfig(
            location=HHLocationFilters(
                rectangular_area=(HHGeoCorner(lat=55.4, lng=200.0), HHGeoCorner(lat=56.0, lng=201.0))
            )
        )


# --- No legacy compat ---------------------------------------------------------


def test_old_flat_filter_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        HHServiceConfig.model_validate({"resume_id": "resume-1", "filter": {"area": [1], "no_magic": True}})


def test_old_wire_value_spellings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        HHFilterConfig.model_validate(
            {"work_arrangement": {"work_formats": ["REMOTE"], "required_experience": "between1And3"}}
        )


# --- Snapshot round-trip -------------------------------------------------------


def test_contract_round_trips_through_stored_section_json() -> None:
    section = HHServiceConfig(
        resume_id="resume-1",
        searches=(
            HHSearchEntry(
                query="python",
                filter=HHFilterConfig(
                    query=HHQueryFilters(exclude_words="стажер"),
                    published=HHPublishedFilters(from_=date(2026, 8, 1), to=date(2026, 8, 20)),
                    location=HHLocationFilters(
                        rectangular_area=(HHGeoCorner(lat=55.4, lng=37.2), HHGeoCorner(lat=56.0, lng=38.0))
                    ),
                ),
            ),
        ),
    )
    raw = dump_service_section(section)
    assert "from" in json.loads(raw)["searches"][0]["filter"]["published"]  # alias key, not from_
    loaded = load_service_section("hh", raw)
    assert loaded.is_ok
    restored = loaded.unwrap()
    assert restored is not None
    assert restored == section
