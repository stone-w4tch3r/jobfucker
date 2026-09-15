"""Wire-model pinning: ``HHSearchFilters`` matches docs/hh/api/search.md exactly.

This module guards only the wire vocabulary against wiki drift (every wiki
value stays an accepted Literal member; no member silently disappears).
Contract→wire mapping and end-to-end encoding live in
``test_filter_contract.py``.
"""

from collections.abc import Callable
from datetime import date
from typing import Final, get_args

import pytest
from pydantic import ValidationError

from jobfucker.clients.hh.config import (
    HHSearchFilters,
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
)

# Value sets copied character-for-character from docs/hh/api/search.md. A future
# typo or omission in any ``HHSearchFilters`` Literal member fails the parity
# test below: a mistyped member stops being accepted by the model, a removed
# member stops matching the wiki set.
_WIKI_ENUM_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "part_time": (
        "employment_project",
        "employment_part",
        "temporary_job_true",
        "from_four_to_six_hours_in_a_day",
        "only_saturday_and_sunday",
        "start_after_sixteen",
    ),
    "work_schedule_by_days": (
        "SIX_ON_ONE_OFF",
        "FIVE_ON_TWO_OFF",
        "FOUR_ON_THREE_OFF",
        "FOUR_ON_TWO_OFF",
        "THREE_ON_THREE_OFF",
        "THREE_ON_TWO_OFF",
        "TWO_ON_TWO_OFF",
        "TWO_ON_ONE_OFF",
        "ONE_ON_THREE_OFF",
        "ONE_ON_TWO_OFF",
        "WEEKEND",
        "FLEXIBLE",
        "OTHER",
        "FOUR_ON_FOUR_OFF",
    ),
    "working_hours": (
        "HOURS_2",
        "HOURS_3",
        "HOURS_4",
        "HOURS_5",
        "HOURS_6",
        "HOURS_7",
        "HOURS_8",
        "HOURS_9",
        "HOURS_10",
        "HOURS_11",
        "HOURS_12",
        "HOURS_24",
        "FLEXIBLE",
        "OTHER",
    ),
    "label": (
        "with_address",
        "not_from_agency",
        "accept_kids",
        "accredited_it",
        "low_performance",
        "internship",
        "night_shifts",
        "with_salary",
        "accept_labor_contract",
    ),
    "salary_frequency": ("DAILY", "WEEKLY", "TWICE_PER_MONTH", "MONTHLY", "PER_PROJECT"),
    "salary_mode": ("MONTH", "SHIFT", "HOUR", "FLY_IN_FLY_OUT", "SERVICE"),
    "order_by": ("relevance", "publication_time", "salary_desc", "salary_asc", "distance"),
    "education": ("not_required_or_not_specified", "special_secondary", "higher"),
    "employment": ("full", "part", "project", "volunteer", "probation"),
    "employment_form": ("FULL", "PART", "PROJECT", "FLY_IN_FLY_OUT"),
    "work_format": ("ON_SITE", "REMOTE", "HYBRID", "FIELD_WORK"),
    "schedule": ("fullDay", "shift", "flexible", "remote", "flyInFlyOut"),
    "experience": ("noExperience", "between1And3", "between3And6", "moreThan6"),
    "search_field": ("name", "company_name", "description"),
    "currency": ("RUR", "USD", "EUR", "KZT"),
}


def test_wire_literals_match_wiki_sets_exactly() -> None:
    """Every wire field's Literal members equal the wiki set, in order."""
    assert get_args(WirePartTime) == _WIKI_ENUM_VALUES["part_time"]
    assert get_args(WireWorkScheduleByDays) == _WIKI_ENUM_VALUES["work_schedule_by_days"]
    assert get_args(WireWorkingHours) == _WIKI_ENUM_VALUES["working_hours"]
    assert get_args(WireLabel) == _WIKI_ENUM_VALUES["label"]
    assert get_args(WireSalaryFrequency) == _WIKI_ENUM_VALUES["salary_frequency"]
    assert get_args(WireSalaryMode) == _WIKI_ENUM_VALUES["salary_mode"]
    assert get_args(WireOrderBy) == _WIKI_ENUM_VALUES["order_by"]
    assert get_args(WireEducation) == _WIKI_ENUM_VALUES["education"]
    assert get_args(WireEmployment) == _WIKI_ENUM_VALUES["employment"]
    assert get_args(WireEmploymentForm) == _WIKI_ENUM_VALUES["employment_form"]
    assert get_args(WireWorkFormat) == _WIKI_ENUM_VALUES["work_format"]
    assert get_args(WireSchedule) == _WIKI_ENUM_VALUES["schedule"]
    assert get_args(WireExperience) == _WIKI_ENUM_VALUES["experience"]
    assert get_args(WireSearchField) == _WIKI_ENUM_VALUES["search_field"]
    assert get_args(WireCurrency) == _WIKI_ENUM_VALUES["currency"]


def test_every_wire_enum_member_is_accepted_by_the_model() -> None:
    """Each wiki value constructs the wire model (repeated fields via 1-tuple)."""
    repeated = {
        "part_time",
        "work_schedule_by_days",
        "working_hours",
        "label",
        "salary_frequency",
        "salary_mode",
        "employment",
        "employment_form",
        "work_format",
        "schedule",
        "search_field",
    }
    for field, values in _WIKI_ENUM_VALUES.items():
        for value in values:
            if field == "order_by" and value == "distance":
                # Cross-field rule: distance sorting requires both sort points.
                HHSearchFilters.model_validate({"order_by": value, "sort_point_lat": 55.75, "sort_point_lng": 37.61})
                continue
            single: str | tuple[str, ...] = (value,) if field in repeated else value
            HHSearchFilters.model_validate({field: single})


def test_unknown_wire_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HHSearchFilters.model_validate({"nonexistent": "x"})


@pytest.mark.parametrize(
    "build_filters",
    [
        lambda: HHSearchFilters(order_by="distance"),
        lambda: HHSearchFilters(order_by="distance", sort_point_lat=55.75),
        lambda: HHSearchFilters(order_by="distance", sort_point_lng=37.61),
        lambda: HHSearchFilters(sort_point_lat=55.75, sort_point_lng=37.61),
        lambda: HHSearchFilters(sort_point_lat=55.75),
        lambda: HHSearchFilters(sort_point_lng=37.61),
        lambda: HHSearchFilters(bottom_left_lat=55.1),
        lambda: HHSearchFilters(bottom_left_lat=55.1, bottom_left_lng=37.1, top_right_lat=56.2),
        lambda: HHSearchFilters(bottom_left_lng=37.1, top_right_lng=38.2),
        lambda: HHSearchFilters(period=30, date_from=date(2026, 8, 1)),
        lambda: HHSearchFilters(period=30, date_to=date(2026, 8, 20)),
        lambda: HHSearchFilters(date_to=date(2026, 8, 20)),
    ],
)
def test_cross_field_combinations_fail_at_construction(build_filters: Callable[[], HHSearchFilters]) -> None:
    with pytest.raises(ValidationError):
        build_filters()


def test_cross_field_valid_combinations_construct() -> None:
    HHSearchFilters(order_by="distance", sort_point_lat=55.75, sort_point_lng=37.61)
    HHSearchFilters(bottom_left_lat=55.1, bottom_left_lng=37.1, top_right_lat=56.2, top_right_lng=38.2)
    HHSearchFilters(period=30)
    HHSearchFilters(date_from=date(2026, 8, 1))
    HHSearchFilters(date_from=date(2026, 8, 1), date_to=date(2026, 8, 20))
