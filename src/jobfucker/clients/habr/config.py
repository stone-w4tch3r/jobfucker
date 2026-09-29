"""Strict Habr service configuration: user-facing filter contract + wire-facing filters.

Two layers live here, mirroring the HH client:

- **Contract** (``HabrFilterConfig``) — what ``service.habr.searches[].filter`` in
  ``pipeline.yaml`` and the future UI form work with. Speaking field names and
  values, full ``Field(title/description)`` annotations so ``model_json_schema()``
  drives a labeled form. The only place Habr wire spellings are visible is the
  module-private ``*_WIRE`` tables below.
- **Wire** (``HabrSearchFilters``) — the encoder-facing flat model mirroring the
  verified ``/api/frontend/vacancies`` parameter catalog. ``HabrSearchFilters`` is
  built only by :meth:`HabrFilterConfig.to_search_filters` (and wire-level tests),
  so the search encoders never see contract names.

Only filters verified against live Habr are exposed (docs/habr/api/search.md);
``company_ids[]``/``divisions`` are deliberately absent because they silently lie
(docs/habr/known-unknowns.md).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobfucker.clients.base import SearchEntryBase

# --- Wire vocabulary (verified /api/frontend/vacancies params) ---------------
WireSearchType = Literal["all", "suitable"]
WireSort = Literal["relevance", "date", "salary_desc", "salary_asc"]
WireQualificationId = Literal[1, 3, 4, 5, 6]
WireCurrency = Literal["RUR", "EUR", "USD", "UAH", "KZT"]
WireEmployment = Literal["full_time", "part_time"]

# Location ids are prefixed so a bare numeric id (silently ignored upstream) can
# never be sent: c_<city>, r_<region>, ct_<country>.
_LOCATION_PATTERN: Final = re.compile(r"(?:ct|c|r)_\d+")


class HabrSearchFilters(BaseModel):
    """Typed wire surface covering the verified listing parameter catalog.

    Built only by :meth:`HabrFilterConfig.to_search_filters` (and wire-level
    tests); yaml authors never spell these names. ``extra="forbid"`` rejects
    unknown fields so a contract typo cannot become a silently ignored request.
    """

    model_config = ConfigDict(extra="forbid")

    sort: WireSort = "relevance"
    qid: WireQualificationId | None = None
    remote: bool = False
    with_salary: bool = False
    salary: int | None = Field(default=None, ge=0)
    currency: WireCurrency = "RUR"
    skills: tuple[int, ...] = ()
    locations: tuple[str, ...] = ()
    employment_type: WireEmployment | None = None


# --- Contract vocabulary (pipeline.yaml / UI form) ---------------------------


class HabrSearchType(StrEnum):
    """Which Habr listing the entry searches."""

    ALL = "all"  # type=all — the whole pool
    SUITABLE = "suitable"  # type=suitable — the board's own match; needs a session


class HabrSort(StrEnum):
    """Result ordering (ordering only; the result set does not change)."""

    RELEVANCE = "relevance"
    DATE = "date"
    SALARY_DESC = "salary_desc"
    SALARY_ASC = "salary_asc"


class HabrQualification(StrEnum):
    """Required applicant grade; maps to the board's numeric ``qid``."""

    INTERN = "intern"
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"


class HabrSalaryCurrency(StrEnum):
    """Currency the ``salary_from`` threshold is expressed in."""

    RUR = "rur"
    EUR = "eur"
    USD = "usd"
    UAH = "uah"
    KZT = "kzt"


class HabrEmployment(StrEnum):
    """Employment type as the board exposes it."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"


# Contract member -> Habr wire value. The only place Habr spellings are visible.
_SEARCH_TYPE_WIRE: Final[dict[HabrSearchType, WireSearchType]] = {
    HabrSearchType.ALL: "all",
    HabrSearchType.SUITABLE: "suitable",
}
_SORT_WIRE: Final[dict[HabrSort, WireSort]] = {
    HabrSort.RELEVANCE: "relevance",
    HabrSort.DATE: "date",
    HabrSort.SALARY_DESC: "salary_desc",
    HabrSort.SALARY_ASC: "salary_asc",
}
# Grade -> the board's qid numbering (2 = unknown, intentionally unmapped).
_QUALIFICATION_WIRE: Final[dict[HabrQualification, WireQualificationId]] = {
    HabrQualification.INTERN: 1,
    HabrQualification.JUNIOR: 3,
    HabrQualification.MIDDLE: 4,
    HabrQualification.SENIOR: 5,
    HabrQualification.LEAD: 6,
}
_CURRENCY_WIRE: Final[dict[HabrSalaryCurrency, WireCurrency]] = {
    HabrSalaryCurrency.RUR: "RUR",
    HabrSalaryCurrency.EUR: "EUR",
    HabrSalaryCurrency.USD: "USD",
    HabrSalaryCurrency.UAH: "UAH",
    HabrSalaryCurrency.KZT: "KZT",
}
_EMPLOYMENT_WIRE: Final[dict[HabrEmployment, WireEmployment]] = {
    HabrEmployment.FULL_TIME: "full_time",
    HabrEmployment.PART_TIME: "part_time",
}


def encode_search_type(search_type: HabrSearchType) -> WireSearchType:
    """Map the contract search type onto its ``type=`` wire spelling."""
    return _SEARCH_TYPE_WIRE[search_type]


def _wire_single[EnumMember: StrEnum, WireValue](
    table: Mapping[EnumMember, WireValue], member: EnumMember | None
) -> WireValue | None:
    """Map one optional contract enum member onto its wire value (str or int)."""
    return None if member is None else table[member]


SkillsTuple = Annotated[tuple[int, ...], Field(description="Habr skill ids (docs/habr/aux/skills.yaml).")]
LocationsTuple = Annotated[
    tuple[str, ...],
    Field(description="Prefixed location ids: c_<city> / r_<region> / ct_<country>."),
]


class HabrFilterConfig(BaseModel):
    """User-facing flat filter contract for ``service.habr.searches[].filter``.

    Every field carries UI annotations; :meth:`to_search_filters` is the one-way
    bridge to the wire model. Only verified filters are exposed — a filter that
    lies (silently ignored upstream) is worse than no filter.
    """

    model_config = ConfigDict(extra="forbid", title="Habr vacancy-search filter", frozen=True)

    sort: HabrSort = Field(
        default=HabrSort.RELEVANCE,
        title="Sort",
        description="Result ordering: relevance, date, salary_desc, salary_asc.",
    )
    qualification: HabrQualification | None = Field(
        default=None,
        title="Qualification",
        description="Required grade (qid 1/3/4/5/6); omitted = any grade.",
    )
    remote: bool = Field(default=False, title="Remote", description="Only remote vacancies (remote=true).")
    with_salary: bool = Field(
        default=False,
        title="With salary",
        description="Only vacancies stating a salary (with_salary=true).",
    )
    salary_from: int | None = Field(
        default=None,
        ge=0,
        title="Salary from",
        description="Minimum salary threshold in the chosen currency; omitted = any.",
    )
    salary_currency: HabrSalaryCurrency = Field(
        default=HabrSalaryCurrency.RUR,
        title="Salary currency",
        description="Currency for 'salary from'; one of rur, eur, usd, uah, kzt.",
    )
    skills: SkillsTuple = Field(default=(), title="Skills", description="Required skill ids (skills[]=…).")
    locations: LocationsTuple = Field(
        default=(),
        title="Locations",
        description="Prefixed location ids (locations[]=…); bare numeric ids are rejected.",
    )
    employment: HabrEmployment | None = Field(
        default=None,
        title="Employment",
        description="Employment type (employment_type=…); omitted = any.",
    )

    @field_validator("skills")
    @classmethod
    def _reject_non_positive_skill_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(skill_id <= 0 for skill_id in value):
            raise ValueError("skill ids must be positive")
        return value

    @field_validator("locations")
    @classmethod
    def _reject_unprefixed_locations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # A bare numeric location id is silently ignored by the board, so it must
        # never leave the client: reject it at construction (a lying filter).
        if any(_LOCATION_PATTERN.fullmatch(location) is None for location in value):
            raise ValueError("locations must use the c_/r_/ct_ prefixed ids")
        return value

    def to_search_filters(self) -> HabrSearchFilters:
        """Map the contract onto the wire filter model (all Habr spellings hidden here)."""
        return HabrSearchFilters(
            sort=_SORT_WIRE[self.sort],
            qid=_wire_single(_QUALIFICATION_WIRE, self.qualification),
            remote=self.remote,
            with_salary=self.with_salary,
            salary=self.salary_from,
            currency=_CURRENCY_WIRE[self.salary_currency],
            skills=self.skills,
            locations=self.locations,
            employment_type=_wire_single(_EMPLOYMENT_WIRE, self.employment),
        )


class HabrSearchEntry(SearchEntryBase):
    """One ordered ``service.habr.searches[]`` entry: query + search type + filter (+ window).

    Self-contained by design: each entry carries its own board query string, its
    own listing :class:`HabrSearchType`, and its own typed filter (no defaulting
    or merging between entries). The ``window`` base field optionally scopes the
    entry's fetch window; core reads only ``query`` + ``window``.
    """

    model_config = ConfigDict(extra="forbid", title="Habr search entry", frozen=True)

    search_type: HabrSearchType = Field(
        default=HabrSearchType.ALL,
        title="Search type",
        description="all = the whole pool; suitable = the board's own match (requires a session).",
    )
    filter: HabrFilterConfig = Field(default_factory=HabrFilterConfig, title="Habr vacancy-search filter")


class HabrServiceConfig(BaseModel):
    """Validated ``service.habr`` pipeline section."""

    model_config = ConfigDict(extra="forbid")

    searches: tuple[HabrSearchEntry, ...] = Field(
        min_length=1,
        title="Search pool",
        description=(
            "Ordered search entries fetched one after another into the same DB; "
            "an entry's position is its stable search_index."
        ),
    )
    resume_id: str | None = Field(
        default=None,
        min_length=1,
        title="Resume id",
        description="Optional; defaults to the account alias. A mismatch with the alias is a pipeline stop.",
    )
    captcha_max_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        title="CAPTCHA answer attempts",
        description="Maximum fresh SmartCaptcha images solved for one login challenge.",
    )
