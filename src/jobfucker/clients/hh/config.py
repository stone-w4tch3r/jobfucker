"""Strict HH service configuration: user-facing filter contract + wire-facing filters.

Two layers live here:

- **Contract** (``HHFilterConfig`` + group models) — what ``service.hh.filter``
  in ``pipeline.yaml`` and the future UI form work with. Speaking field names
  and values, nested groups, full ``Field(title/description/examples)``
  annotations so ``model_json_schema()`` drives a labeled form
  (client-contract.md §6.2). The only place HH vocabulary is visible is the
  module-private ``*_WIRE`` mapping tables below.
- **Wire** (``HHSearchFilters``) — the encoder-facing flat model mirroring the
  full ``/vacancies`` parameter catalog (docs/hh/api/search.md). Unchanged
  public surface; ``HHFilterConfig.to_search_filters()`` is the one-way
  contract→wire bridge, so the search encoders never see contract names.

Cross-field rules fail at construction (``init``/``update``/stored-section
reconstruction) so an invalid combination can never produce a silently inert
or silently broadened upstream request. Encoding rules live in
:func:`jobfucker.clients.hh.search._search_params`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobfucker.clients.base import SearchEntryBase

# --- Wire vocabulary (HH /vacancies catalog, docs/hh/api/search.md) ----------

WireSearchField = Literal["name", "company_name", "description"]
WireOrderBy = Literal["relevance", "publication_time", "salary_desc", "salary_asc", "distance"]
WireSchedule = Literal["fullDay", "shift", "flexible", "remote", "flyInFlyOut"]
WireWorkFormat = Literal["ON_SITE", "REMOTE", "HYBRID", "FIELD_WORK"]
WireEmployment = Literal["full", "part", "project", "volunteer", "probation"]
WireEmploymentForm = Literal["FULL", "PART", "PROJECT", "FLY_IN_FLY_OUT"]
WireExperience = Literal["noExperience", "between1And3", "between3And6", "moreThan6"]
WireEducation = Literal["not_required_or_not_specified", "special_secondary", "higher"]
WireCurrency = Literal["RUR", "USD", "EUR", "KZT"]
WireSalaryFrequency = Literal["DAILY", "WEEKLY", "TWICE_PER_MONTH", "MONTHLY", "PER_PROJECT"]
WireSalaryMode = Literal["MONTH", "SHIFT", "HOUR", "FLY_IN_FLY_OUT", "SERVICE"]
WireLabel = Literal[
    "with_address",
    "not_from_agency",
    "accept_kids",
    "accredited_it",
    "low_performance",
    "internship",
    "night_shifts",
    "with_salary",
    "accept_labor_contract",
]
WirePartTime = Literal[
    "employment_project",
    "employment_part",
    "temporary_job_true",
    "from_four_to_six_hours_in_a_day",
    "only_saturday_and_sunday",
    "start_after_sixteen",
]
WireWorkScheduleByDays = Literal[
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
]
WireWorkingHours = Literal[
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
]


class HHSearchFilters(BaseModel):
    """Typed wire surface covering the full ``/vacancies`` parameter catalog.

    Built only by :meth:`HHFilterConfig.to_search_filters` (and the wire-level
    tests); yaml authors never spell these names. ``extra="forbid"`` rejects
    unknown fields; the model validator enforces the wiki cross-field
    constraints as defense in depth behind the contract's own validators.
    """

    model_config = ConfigDict(extra="forbid")

    no_magic: bool = False
    search_field: tuple[WireSearchField, ...] = ()
    order_by: WireOrderBy = "relevance"
    sort_point_lat: float | None = None
    sort_point_lng: float | None = None
    clusters: bool = False
    describe_arguments: bool = False
    responses_count_enabled: bool = False
    schedule: tuple[WireSchedule, ...] = ()
    work_format: tuple[WireWorkFormat, ...] = ()
    employment: tuple[WireEmployment, ...] = ()
    employment_form: tuple[WireEmploymentForm, ...] = ()
    experience: WireExperience | None = None
    education: WireEducation | None = None
    part_time: tuple[WirePartTime, ...] = ()
    work_schedule_by_days: tuple[WireWorkScheduleByDays, ...] = ()
    working_hours: tuple[WireWorkingHours, ...] = ()
    accept_temporary: bool = False
    salary: int | None = Field(default=None, ge=0)
    currency: WireCurrency | None = None
    only_with_salary: bool = False
    salary_frequency: tuple[WireSalaryFrequency, ...] = ()
    salary_mode: tuple[WireSalaryMode, ...] = ()
    period: int | None = Field(default=None, gt=0)
    date_from: date | None = None
    date_to: date | None = None
    area: tuple[int, ...] = ()
    metro: tuple[str, ...] = ()
    district: tuple[int, ...] = ()
    bottom_left_lat: float | None = None
    bottom_left_lng: float | None = None
    top_right_lat: float | None = None
    top_right_lng: float | None = None
    professional_role: tuple[int, ...] = ()
    industry: tuple[int, ...] = ()
    employer_id: tuple[int, ...] = ()
    excluded_employer_id: tuple[int, ...] = ()
    excluded_text: str | None = Field(default=None, min_length=1)
    label: tuple[WireLabel, ...] = ()
    saved_search_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _reject_inert_or_broadening_combinations(self) -> HHSearchFilters:
        """Enforce the wiki cross-field constraints (docs/hh/api/search.md)."""
        both_points = self.sort_point_lat is not None and self.sort_point_lng is not None
        any_point = self.sort_point_lat is not None or self.sort_point_lng is not None
        if self.order_by == "distance" and not both_points:
            raise ValueError("order_by='distance' requires both sort_point_lat and sort_point_lng")
        if self.order_by != "distance" and any_point:
            raise ValueError("sort_point_lat/sort_point_lng are forbidden without order_by='distance'")
        corners = (self.bottom_left_lat, self.bottom_left_lng, self.top_right_lat, self.top_right_lng)
        if any(corner is not None for corner in corners) and not all(corner is not None for corner in corners):
            raise ValueError("bbox corners (bottom_left_*, top_right_*) must be set together or not at all")
        if self.period is not None and (self.date_from is not None or self.date_to is not None):
            raise ValueError("period cannot coexist with date_from/date_to")
        if self.date_to is not None and self.date_from is None:
            raise ValueError("date_to requires date_from")
        return self


# --- Contract vocabulary (pipeline.yaml / UI form) ---------------------------


class SearchIn(StrEnum):
    """Vacancy text fields the pipeline query is matched against."""

    NAME = "name"
    COMPANY_NAME = "company_name"
    DESCRIPTION = "description"


class SortBy(StrEnum):
    """Result ordering; ``DISTANCE`` requires ``distance_from_point``."""

    RELEVANCE = "relevance"
    PUBLICATION_TIME = "publication_time"
    SALARY_HIGH_TO_LOW = "salary_high_to_low"
    SALARY_LOW_TO_HIGH = "salary_low_to_high"
    DISTANCE = "distance"


class WorkSchedule(StrEnum):
    """Work schedule (classic HH vocabulary; legacy_params group)."""

    FULL_DAY = "full_day"
    SHIFT = "shift"
    FLEXIBLE = "flexible"
    REMOTE = "remote"
    FLY_IN_FLY_OUT = "fly_in_fly_out"


class WorkFormat(StrEnum):
    """Where the work is performed (modern HH vocabulary)."""

    ON_SITE = "on_site"
    REMOTE = "remote"
    HYBRID = "hybrid"
    FIELD_WORK = "field_work"


class EmploymentType(StrEnum):
    """Type of employment (classic HH vocabulary; legacy_params group)."""

    FULL = "full"
    PART = "part"
    PROJECT = "project"
    VOLUNTEER = "volunteer"
    PROBATION = "probation"


class EmploymentForm(StrEnum):
    """Form of employment (modern HH vocabulary)."""

    FULL = "full"
    PART = "part"
    PROJECT = "project"
    FLY_IN_FLY_OUT = "fly_in_fly_out"


class ExperienceLevel(StrEnum):
    """Required work-experience bucket."""

    NO_EXPERIENCE = "no_experience"
    BETWEEN_1_AND_3_YEARS = "between_1_and_3_years"
    BETWEEN_3_AND_6_YEARS = "between_3_and_6_years"
    MORE_THAN_6_YEARS = "more_than_6_years"


class EducationLevel(StrEnum):
    """Required education level."""

    ANY = "any"
    SPECIAL_SECONDARY = "special_secondary"
    HIGHER = "higher"


class ShiftRotation(StrEnum):
    """Shift-work rotation pattern (days on / days off)."""

    SIX_ON_ONE_OFF = "6_on_1_off"
    FIVE_ON_TWO_OFF = "5_on_2_off"
    FOUR_ON_THREE_OFF = "4_on_3_off"
    FOUR_ON_TWO_OFF = "4_on_2_off"
    THREE_ON_THREE_OFF = "3_on_3_off"
    THREE_ON_TWO_OFF = "3_on_2_off"
    TWO_ON_TWO_OFF = "2_on_2_off"
    TWO_ON_ONE_OFF = "2_on_1_off"
    ONE_ON_THREE_OFF = "1_on_3_off"
    ONE_ON_TWO_OFF = "1_on_2_off"
    WEEKENDS_ONLY = "weekends_only"
    FOUR_ON_FOUR_OFF = "4_on_4_off"
    FLEXIBLE = "flexible"
    OTHER = "other"


class HoursPerDay(StrEnum):
    """Working-hours-per-day bucket (``AROUND_THE_CLOCK`` = 24-hour shift day)."""

    HOURS_2 = "hours_2"
    HOURS_3 = "hours_3"
    HOURS_4 = "hours_4"
    HOURS_5 = "hours_5"
    HOURS_6 = "hours_6"
    HOURS_7 = "hours_7"
    HOURS_8 = "hours_8"
    HOURS_9 = "hours_9"
    HOURS_10 = "hours_10"
    HOURS_11 = "hours_11"
    HOURS_12 = "hours_12"
    AROUND_THE_CLOCK = "around_the_clock"
    FLEXIBLE = "flexible"
    OTHER = "other"


class PartTimeOption(StrEnum):
    """Part-time / side-job checkbox options (composite HH filter)."""

    PROJECT_WORK = "project_work"
    PART_TIME_EMPLOYMENT = "part_time_employment"
    TEMPORARY_ONLY = "temporary_only"
    FOUR_TO_SIX_HOURS_A_DAY = "four_to_six_hours_a_day"
    WEEKENDS_ONLY = "weekends_only"
    START_AFTER_4PM = "start_after_4pm"


class Currency(StrEnum):
    """Salary currency."""

    RUR = "rur"
    USD = "usd"
    EUR = "eur"
    KZT = "kzt"


class SalaryPaid(StrEnum):
    """How often the salary is paid."""

    DAILY = "daily"
    WEEKLY = "weekly"
    TWICE_A_MONTH = "twice_a_month"
    MONTHLY = "monthly"
    PER_PROJECT = "per_project"


class SalaryUnit(StrEnum):
    """Unit the stated salary is measured in."""

    PER_MONTH = "per_month"
    PER_SHIFT = "per_shift"
    PER_HOUR = "per_hour"
    PER_FLY_IN_FLY_OUT = "per_fly_in_fly_out"
    PER_SERVICE = "per_service"


class VacancyLabel(StrEnum):
    """Vacancy badge (HH ``label``)."""

    WITH_SALARY = "with_salary"
    DIRECT_EMPLOYER = "direct_employer"
    WITH_OFFICE_ADDRESS = "with_office_address"
    ACCEPTS_TEENS = "accepts_teens"
    ACCREDITED_IT = "accredited_it"
    INTERNSHIP = "internship"
    NIGHT_SHIFTS = "night_shifts"
    ACCEPTS_LABOR_CONTRACT = "accepts_labor_contract"
    LOW_PERFORMANCE = "low_performance"


# Contract member → HH wire value. The only place HH spellings are visible.
# Exhaustive-parity with the wire Literals is pinned in
# test/clients/hh/test_filter_contract.py.
_SEARCH_IN_WIRE: Final[dict[SearchIn, WireSearchField]] = {member: member.value for member in SearchIn}
_SORT_BY_WIRE: Final[dict[SortBy, WireOrderBy]] = {
    SortBy.RELEVANCE: "relevance",
    SortBy.PUBLICATION_TIME: "publication_time",
    SortBy.SALARY_HIGH_TO_LOW: "salary_desc",
    SortBy.SALARY_LOW_TO_HIGH: "salary_asc",
    SortBy.DISTANCE: "distance",
}
_WORK_SCHEDULE_WIRE: Final[dict[WorkSchedule, WireSchedule]] = {
    WorkSchedule.FULL_DAY: "fullDay",
    WorkSchedule.SHIFT: "shift",
    WorkSchedule.FLEXIBLE: "flexible",
    WorkSchedule.REMOTE: "remote",
    WorkSchedule.FLY_IN_FLY_OUT: "flyInFlyOut",
}
_WORK_FORMAT_WIRE: Final[dict[WorkFormat, WireWorkFormat]] = {
    WorkFormat.ON_SITE: "ON_SITE",
    WorkFormat.REMOTE: "REMOTE",
    WorkFormat.HYBRID: "HYBRID",
    WorkFormat.FIELD_WORK: "FIELD_WORK",
}
_EMPLOYMENT_WIRE: Final[dict[EmploymentType, WireEmployment]] = {member: member.value for member in EmploymentType}
_EMPLOYMENT_FORM_WIRE: Final[dict[EmploymentForm, WireEmploymentForm]] = {
    EmploymentForm.FULL: "FULL",
    EmploymentForm.PART: "PART",
    EmploymentForm.PROJECT: "PROJECT",
    EmploymentForm.FLY_IN_FLY_OUT: "FLY_IN_FLY_OUT",
}
_EXPERIENCE_WIRE: Final[dict[ExperienceLevel, WireExperience]] = {
    ExperienceLevel.NO_EXPERIENCE: "noExperience",
    ExperienceLevel.BETWEEN_1_AND_3_YEARS: "between1And3",
    ExperienceLevel.BETWEEN_3_AND_6_YEARS: "between3And6",
    ExperienceLevel.MORE_THAN_6_YEARS: "moreThan6",
}
_EDUCATION_WIRE: Final[dict[EducationLevel, WireEducation]] = {
    EducationLevel.ANY: "not_required_or_not_specified",
    EducationLevel.SPECIAL_SECONDARY: "special_secondary",
    EducationLevel.HIGHER: "higher",
}
_SHIFT_ROTATION_WIRE: Final[dict[ShiftRotation, WireWorkScheduleByDays]] = {
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
_HOURS_PER_DAY_WIRE: Final[dict[HoursPerDay, WireWorkingHours]] = {
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
_PART_TIME_WIRE: Final[dict[PartTimeOption, WirePartTime]] = {
    PartTimeOption.PROJECT_WORK: "employment_project",
    PartTimeOption.PART_TIME_EMPLOYMENT: "employment_part",
    PartTimeOption.TEMPORARY_ONLY: "temporary_job_true",
    PartTimeOption.FOUR_TO_SIX_HOURS_A_DAY: "from_four_to_six_hours_in_a_day",
    PartTimeOption.WEEKENDS_ONLY: "only_saturday_and_sunday",
    PartTimeOption.START_AFTER_4PM: "start_after_sixteen",
}
_CURRENCY_WIRE: Final[dict[Currency, WireCurrency]] = {
    Currency.RUR: "RUR",
    Currency.USD: "USD",
    Currency.EUR: "EUR",
    Currency.KZT: "KZT",
}
_SALARY_PAID_WIRE: Final[dict[SalaryPaid, WireSalaryFrequency]] = {
    SalaryPaid.DAILY: "DAILY",
    SalaryPaid.WEEKLY: "WEEKLY",
    SalaryPaid.TWICE_A_MONTH: "TWICE_PER_MONTH",
    SalaryPaid.MONTHLY: "MONTHLY",
    SalaryPaid.PER_PROJECT: "PER_PROJECT",
}
_SALARY_UNIT_WIRE: Final[dict[SalaryUnit, WireSalaryMode]] = {
    SalaryUnit.PER_MONTH: "MONTH",
    SalaryUnit.PER_SHIFT: "SHIFT",
    SalaryUnit.PER_HOUR: "HOUR",
    SalaryUnit.PER_FLY_IN_FLY_OUT: "FLY_IN_FLY_OUT",
    SalaryUnit.PER_SERVICE: "SERVICE",
}
_VACANCY_LABEL_WIRE: Final[dict[VacancyLabel, WireLabel]] = {
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


def _wire_members[EnumMember: StrEnum, WireValue: str](
    table: Mapping[EnumMember, WireValue], members: tuple[EnumMember, ...]
) -> tuple[WireValue, ...]:
    """Map every contract enum member of a repeated field onto its wire value."""
    return tuple(table[member] for member in members)


def _wire_single[EnumMember: StrEnum, WireValue: str](
    table: Mapping[EnumMember, WireValue], member: EnumMember | None
) -> WireValue | None:
    """Map one optional contract enum member onto its wire value."""
    return None if member is None else table[member]


Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]


# --- Contract groups (pipeline.yaml service.hh.filter) -----------------------


class HHQueryFilters(BaseModel):
    """Text-query contract: how the pipeline query is matched and narrowed."""

    model_config = ConfigDict(extra="forbid", title="Query", frozen=True)

    use_hh_query_language: bool = Field(
        default=True,
        title="Use HH query language",
        description="false sends the pipeline query verbatim (no HH query-language operators).",
        examples=[True],
    )
    fields_to_search_in: tuple[SearchIn, ...] = Field(
        default=(),
        title="Fields to search in",
        description="Vacancy fields the query matches; requires a non-empty pipeline query.",
        examples=[(SearchIn.NAME, SearchIn.DESCRIPTION)],
    )
    exclude_words: str | None = Field(
        default=None,
        min_length=1,
        title="Exclude words",
        description="Comma-separated words that disqualify a vacancy.",
        examples=["стажер,intern"],
    )


class HHDistancePoint(BaseModel):
    """Geographic point for distance sorting."""

    model_config = ConfigDict(extra="forbid", title="Distance point", frozen=True)

    lat: Latitude = Field(title="Latitude", examples=[55.75])
    lng: Longitude = Field(title="Longitude", examples=[37.61])


class HHOrderingFilters(BaseModel):
    """Result-ordering contract."""

    model_config = ConfigDict(extra="forbid", title="Ordering", frozen=True)

    sort_results_by: SortBy = Field(
        default=SortBy.RELEVANCE,
        title="Sort results by",
        description="DISTANCE sorts by proximity to ``distance_from_point``.",
        examples=[SortBy.PUBLICATION_TIME],
    )
    distance_from_point: HHDistancePoint | None = Field(
        default=None,
        title="Distance from point",
        description="Reference point; required by and only allowed with ``sort_results_by: distance``.",
    )

    @model_validator(mode="after")
    def _point_belongs_to_distance_sort(self) -> HHOrderingFilters:
        """Enforce the point/distance pairing at contract level (wiki constraint)."""
        if self.sort_results_by is SortBy.DISTANCE and self.distance_from_point is None:
            raise ValueError("sort_results_by='distance' requires distance_from_point")
        if self.sort_results_by is not SortBy.DISTANCE and self.distance_from_point is not None:
            raise ValueError("distance_from_point is only allowed with sort_results_by='distance'")
        return self


class HHLegacyWorkParams(BaseModel):
    """Classic HH work vocabulary (parallel to the modern one; expected untouched).

    Kept because its value coverage differs from the modern families
    (docs/hh/api/search.md): both vocabularies remain functional upstream.
    """

    model_config = ConfigDict(extra="forbid", title="Legacy work params", frozen=True)

    work_schedules: tuple[WorkSchedule, ...] = Field(
        default=(),
        title="Work schedules",
        description="Classic schedule vocabulary (график работы).",
        examples=[(WorkSchedule.REMOTE,)],
    )
    employment_types: tuple[EmploymentType, ...] = Field(
        default=(),
        title="Employment types",
        description="Classic employment vocabulary (тип занятости).",
        examples=[(EmploymentType.FULL,)],
    )


class HHWorkArrangementFilters(BaseModel):
    """Work-arrangement contract (when/where/how the work is done)."""

    model_config = ConfigDict(extra="forbid", title="Work arrangement", frozen=True)

    work_formats: tuple[WorkFormat, ...] = Field(
        default=(),
        title="Work formats",
        description="Where the work is performed (modern vocabulary).",
        examples=[(WorkFormat.REMOTE,)],
    )
    employment_forms: tuple[EmploymentForm, ...] = Field(
        default=(),
        title="Employment forms",
        description="Form of employment (modern vocabulary).",
        examples=[(EmploymentForm.FULL,)],
    )
    shift_rotations: tuple[ShiftRotation, ...] = Field(
        default=(),
        title="Shift rotations",
        description="Days-on/days-off rotation patterns.",
        examples=[(ShiftRotation.SIX_ON_ONE_OFF,)],
    )
    hours_per_day: tuple[HoursPerDay, ...] = Field(
        default=(),
        title="Hours per day",
        description="Working-hours-per-day buckets.",
        examples=[(HoursPerDay.HOURS_8,)],
    )
    part_time_options: tuple[PartTimeOption, ...] = Field(
        default=(),
        title="Part-time options",
        description="Composite part-time checkbox filters (подработка).",
        examples=[(PartTimeOption.WEEKENDS_ONLY,)],
    )
    required_experience: ExperienceLevel | None = Field(
        default=None,
        title="Required experience",
        description="Experience bucket the vacancy requires; unset means any.",
        examples=[ExperienceLevel.BETWEEN_1_AND_3_YEARS],
    )
    required_education: EducationLevel | None = Field(
        default=None,
        title="Required education",
        description="Education level the vacancy requires; unset means any.",
        examples=[EducationLevel.HIGHER],
    )
    temporary_jobs_only: bool = Field(
        default=False,
        title="Temporary jobs only",
        description="Restrict to vacancies marked for temporary work.",
        examples=[True],
    )
    legacy_params: HHLegacyWorkParams = Field(
        default_factory=HHLegacyWorkParams,
        title="Legacy work params",
        description="Classic HH vocabulary (schedule/employment); expected untouched.",
    )


class HHSalaryFilters(BaseModel):
    """Salary contract fork."""

    model_config = ConfigDict(extra="forbid", title="Salary", frozen=True, populate_by_name=True)

    from_: int | None = Field(
        default=None,
        ge=0,
        validation_alias="from",
        serialization_alias="from",
        title="Salary from",
        description="Minimum salary fork; unstated-salary vacancies remain unless only_with_salary.",
        examples=[250000],
    )
    currency: Currency | None = Field(
        default=None,
        title="Currency",
        description="Currency of the ``from`` bound.",
        examples=[Currency.RUR],
    )
    only_with_salary: bool = Field(
        default=False,
        title="Only with salary",
        description="Return only vacancies that state a salary.",
        examples=[True],
    )
    paid: tuple[SalaryPaid, ...] = Field(
        default=(),
        title="Paid",
        description="How often the salary is paid.",
        examples=[(SalaryPaid.MONTHLY,)],
    )
    measured_in: tuple[SalaryUnit, ...] = Field(
        default=(),
        title="Measured in",
        description="Unit the stated salary is measured in.",
        examples=[(SalaryUnit.PER_MONTH,)],
    )


class HHPublishedFilters(BaseModel):
    """Publication-freshness contract (``within_days`` XOR from/to)."""

    model_config = ConfigDict(extra="forbid", title="Published", frozen=True, populate_by_name=True)

    within_days: int | None = Field(
        default=None,
        gt=0,
        title="Published within days",
        description="Vacancy age in days; cannot coexist with ``from``/``to``.",
        examples=[30],
    )
    from_: date | None = Field(
        default=None,
        validation_alias="from",
        serialization_alias="from",
        title="Published from",
        description="Publication date lower bound (YYYY-MM-DD).",
        examples=["2026-08-01"],
    )
    to: date | None = Field(
        default=None,
        title="Published to",
        description="Publication date upper bound (YYYY-MM-DD); requires ``from``.",
        examples=["2026-08-29"],
    )

    @model_validator(mode="after")
    def _freshness_window_is_consistent(self) -> HHPublishedFilters:
        """Enforce the wiki publication-time constraints at contract level."""
        if self.within_days is not None and (self.from_ is not None or self.to is not None):
            raise ValueError("within_days cannot coexist with from/to")
        if self.to is not None and self.from_ is None:
            raise ValueError("to requires from")
        return self


class HHGeoCorner(BaseModel):
    """One rectangular-area corner (``[lat, lng]``)."""

    model_config = ConfigDict(extra="forbid", title="Geo corner", frozen=True)

    lat: Latitude = Field(title="Latitude", examples=[55.40])
    lng: Longitude = Field(title="Longitude", examples=[37.20])


class HHLocationFilters(BaseModel):
    """Location contract."""

    model_config = ConfigDict(extra="forbid", title="Location", frozen=True)

    regions: tuple[int, ...] = Field(
        default=(),
        title="Regions",
        description="HH area ids (1=Москва, 88=Казань, 16=Беларусь).",
        examples=[(1,)],
    )
    districts: tuple[int, ...] = Field(
        default=(),
        title="Districts",
        description="Administrative-district ids.",
        examples=[(1205,)],
    )
    metro_stations: tuple[str, ...] = Field(
        default=(),
        title="Metro stations",
        description="``line.station`` ids (e.g. ``1.118``).",
        examples=[("1.118",)],
    )
    rectangular_area: tuple[HHGeoCorner, HHGeoCorner] | None = Field(
        default=None,
        title="Rectangular area",
        description="Exactly two corners: south-west then north-east.",
    )


class HHTargetingFilters(BaseModel):
    """Employer/role/badge targeting contract."""

    model_config = ConfigDict(extra="forbid", title="Targeting", frozen=True)

    professional_roles: tuple[int, ...] = Field(
        default=(),
        title="Professional roles",
        description="HH professional-role ids (96=programmer, 165=data scientist).",
        examples=[(96,)],
    )
    industries: tuple[int, ...] = Field(
        default=(),
        title="Industries",
        description="HH industry ids (7=IT).",
        examples=[(7,)],
    )
    employers: tuple[int, ...] = Field(
        default=(),
        title="Employers",
        description="Only these employer ids.",
        examples=[(1455,)],
    )
    exclude_employers: tuple[int, ...] = Field(
        default=(),
        title="Exclude employers",
        description="Never these employer ids.",
        examples=[(391,)],
    )
    vacancy_labels: tuple[VacancyLabel, ...] = Field(
        default=(),
        title="Vacancy labels",
        description="Required vacancy badges.",
        examples=[(VacancyLabel.WITH_SALARY,)],
    )
    saved_search: str | None = Field(
        default=None,
        min_length=1,
        title="Saved search",
        description="Account-owned saved-search id; catalog mode only.",
    )


class HHResponseExtras(BaseModel):
    """Response-decoration flags (rarely needed by the engine; full parity kept)."""

    model_config = ConfigDict(extra="forbid", title="Response extras", frozen=True)

    include_clusters: bool = Field(
        default=False,
        title="Include clusters",
        description="Return facet cluster counts in the search response.",
        examples=[True],
    )
    include_arguments_description: bool = Field(
        default=False,
        title="Include arguments description",
        description="Return the server's description of the applied arguments.",
        examples=[True],
    )
    include_responses_count: bool = Field(
        default=False,
        title="Include responses count",
        description="Return per-vacancy response counts.",
        examples=[True],
    )


class HHFilterConfig(BaseModel):
    """User-facing nested filter contract for ``service.hh.filter``.

    Every field carries UI annotations; ``to_search_filters()`` is the one-way
    bridge to the wire model. Groups are optional; an empty config sends no
    filter params.
    """

    model_config = ConfigDict(extra="forbid", title="HH vacancy-search filter", frozen=True)

    query: HHQueryFilters = Field(default_factory=HHQueryFilters, title="Query")
    ordering: HHOrderingFilters = Field(default_factory=HHOrderingFilters, title="Ordering")
    work_arrangement: HHWorkArrangementFilters = Field(
        default_factory=HHWorkArrangementFilters, title="Work arrangement"
    )
    salary: HHSalaryFilters = Field(default_factory=HHSalaryFilters, title="Salary")
    published: HHPublishedFilters = Field(default_factory=HHPublishedFilters, title="Published")
    location: HHLocationFilters = Field(default_factory=HHLocationFilters, title="Location")
    targeting: HHTargetingFilters = Field(default_factory=HHTargetingFilters, title="Targeting")
    response_extras: HHResponseExtras = Field(default_factory=HHResponseExtras, title="Response extras")

    def to_search_filters(self) -> HHSearchFilters:
        """Map the contract onto the wire filter model (all HH spellings hidden here).

        Wire-model validators run on construction, so a contract bug cannot
        produce a silently inert or broadened request.
        """
        query, ordering = self.query, self.ordering
        work, salary = self.work_arrangement, self.salary
        published, location = self.published, self.location
        targeting, extras = self.targeting, self.response_extras
        distance = ordering.distance_from_point
        corners = location.rectangular_area
        south_west, north_east = corners if corners is not None else (None, None)
        return HHSearchFilters(
            no_magic=not query.use_hh_query_language,
            search_field=_wire_members(_SEARCH_IN_WIRE, query.fields_to_search_in),
            order_by=_SORT_BY_WIRE[ordering.sort_results_by],
            sort_point_lat=None if distance is None else distance.lat,
            sort_point_lng=None if distance is None else distance.lng,
            clusters=extras.include_clusters,
            describe_arguments=extras.include_arguments_description,
            responses_count_enabled=extras.include_responses_count,
            schedule=_wire_members(_WORK_SCHEDULE_WIRE, work.legacy_params.work_schedules),
            work_format=_wire_members(_WORK_FORMAT_WIRE, work.work_formats),
            employment=_wire_members(_EMPLOYMENT_WIRE, work.legacy_params.employment_types),
            employment_form=_wire_members(_EMPLOYMENT_FORM_WIRE, work.employment_forms),
            experience=_wire_single(_EXPERIENCE_WIRE, work.required_experience),
            education=_wire_single(_EDUCATION_WIRE, work.required_education),
            part_time=_wire_members(_PART_TIME_WIRE, work.part_time_options),
            work_schedule_by_days=_wire_members(_SHIFT_ROTATION_WIRE, work.shift_rotations),
            working_hours=_wire_members(_HOURS_PER_DAY_WIRE, work.hours_per_day),
            accept_temporary=work.temporary_jobs_only,
            salary=salary.from_,
            currency=_wire_single(_CURRENCY_WIRE, salary.currency),
            only_with_salary=salary.only_with_salary,
            salary_frequency=_wire_members(_SALARY_PAID_WIRE, salary.paid),
            salary_mode=_wire_members(_SALARY_UNIT_WIRE, salary.measured_in),
            period=published.within_days,
            date_from=published.from_,
            date_to=published.to,
            area=location.regions,
            metro=location.metro_stations,
            district=location.districts,
            bottom_left_lat=None if south_west is None else south_west.lat,
            bottom_left_lng=None if south_west is None else south_west.lng,
            top_right_lat=None if north_east is None else north_east.lat,
            top_right_lng=None if north_east is None else north_east.lng,
            professional_role=targeting.professional_roles,
            industry=targeting.industries,
            employer_id=targeting.employers,
            excluded_employer_id=targeting.exclude_employers,
            excluded_text=query.exclude_words,
            label=_wire_members(_VACANCY_LABEL_WIRE, targeting.vacancy_labels),
            saved_search_id=targeting.saved_search,
        )


class HHSearchMode(StrEnum):
    """The explicit HH vacancy-search endpoint selection."""

    CATALOG = "catalog"
    RESUME_SIMILAR = "resume_similar"


class HHSearchEntry(SearchEntryBase):
    """One ordered ``service.hh.searches[]`` entry: query + HH filter (+ window).

    Self-contained by design: each entry carries its own board query string and
    its own typed :class:`HHFilterConfig` (no defaulting/merging between
    entries — every verified search set is copied verbatim into the pool). The
    ``window`` base field optionally scopes this entry's fetch window.
    """

    model_config = ConfigDict(extra="forbid", title="HH search entry", frozen=True)

    filter: HHFilterConfig = Field(default_factory=HHFilterConfig, title="HH vacancy-search filter")


class HHServiceConfig(BaseModel):
    """Validated ``service.hh`` pipeline section."""

    model_config = ConfigDict(extra="forbid")

    resume_id: str = Field(min_length=1)
    search_mode: HHSearchMode = HHSearchMode.CATALOG
    captcha_max_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        title="CAPTCHA answer attempts",
        description="Maximum fresh HH text-CAPTCHA images solved for one challenge.",
    )
    searches: tuple[HHSearchEntry, ...] = Field(
        min_length=1,
        title="Search pool",
        description=(
            "Ordered search entries fetched one after another into the same DB; "
            "an entry's position is its stable search_index."
        ),
    )
