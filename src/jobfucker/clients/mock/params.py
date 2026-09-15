"""Mock service config section: the mock's filter AND its manual-testing behavior.

The ``service.mock`` pipeline.yaml section is validated (config.py, pydantic)
into :class:`MockServiceConfig`, which implements the ``ServiceConfigSection``
protocol from the contract. It carries two things:

- ``filter``: a concrete :class:`MockSearchParams` — the mock's own,
  non-abstracted search filter object.
- ``behavior``: an optional :class:`MockBehaviorConfig` — the mock's
  programmable manual-testing seam (``authorize_error``, ``default_apply``,
  ``per_vacancy``). It is part of the mock's own section so tests and manual
  play can exercise every apply outcome and a failing ``authorize``; it is
   never exposed to core, which only ever reads ``resume_id`` off the section.

Every model below carries ``Field(title/description/examples)`` and ``Literal``
enums so its ``model_json_schema()`` drives a labeled, per-service typed form in
the UI (client-contract.md §6.2): config validates the YAML, the client consumes
the section, and the UI renders/edits the same typed model — no board knowledge
in core, no duplicated schema.

Nothing here touches a network or a board; the mock exists so the whole product
(CLI + GUI + pipeline) is runnable offline before the HH client lands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jobfucker.clients.base import SearchEntryBase

# --- Search filter shape (mirrors HH so the mock's form is realistic) --------
MockOutcome = Literal["applied", "skipped", "error", "limit_exceeded"]

# HH-style schedule values (multi-select form) and experience levels (single
# select). Kept as real enums so the mock exercises a genuinely typed, enum-driven
# search form rather than free text.
MockSchedule = Literal["fullDay", "remote", "flexible", "shift", "flyInFlyOut"]
MockExperience = Literal["noExperience", "between1And3", "between3And6", "moreThan6"]


class MockSearchParams(BaseModel, frozen=True):
    """The mock's concrete, non-abstracted search filter object.

    A pydantic model (not a bare dataclass) because it is surfaced to the UI as a
    typed JSON schema (``model_json_schema()``): titles, descriptions, examples
    and ``Literal`` enums let the form layer render labels + dropdowns instead of
    a raw config blob. Mirrors the HH-filter example (area/schedule/experience/
    only-with-salary) so config-time validation and forms are exercised
    realistically, but it is fully self-contained — no board terminology.
    """

    model_config = ConfigDict(title="Mock search filter", extra="forbid")

    area: tuple[int, ...] = Field(
        default=(),
        title="Area",
        description="Job-board region/city ids to search (mock keeps them generic).",
        examples=[[1]],
    )
    schedule: tuple[MockSchedule, ...] = Field(
        default=(),
        title="Schedule",
        description="Employment schedule types to accept (multi-select).",
        examples=[["fullDay", "remote"]],
    )
    experience: MockExperience | None = Field(
        default=None,
        title="Experience",
        description="Required work-experience level; null means 'any'.",
        examples=["between1And3"],
    )
    only_with_salary: bool = Field(
        default=False,
        title="Only with salary",
        description="Return only vacancies that state a salary.",
        examples=[True],
    )


class MockApplyBehaviorConfig(BaseModel):
    """A single programmable apply outcome for the mock's ``behavior`` block.

    ``outcome`` maps to the four mock behaviors: ``applied``/``skipped`` are
    ``ApplyResult`` successes, ``error`` is a per-vacancy ``Err(BadRequestError)``,
    and ``limit_exceeded`` is the stop signal ``Err(LimitExceededError)``.

    Carries field metadata so the section's JSON schema renders a labeled,
    enum-driven form (see client-contract.md §6.2).
    """

    outcome: MockOutcome = Field(
        title="Outcome",
        description="The apply outcome to script for a vacancy (manual-testing only).",
        examples=["applied"],
    )
    message: str | None = Field(
        title="Message",
        description="Reason text attached to the scripted outcome (skip reason / error text).",
        default=None,
        examples=["mock apply rejected"],
    )


class MockCaptchaConfig(BaseModel):
    """Script a captcha challenge on one mock operation (manual-testing only).

    When ``operation`` matches, the mock first routes its internal captcha
    image through the injected :data:`~jobfucker.clients.base.CaptchaHandler`
    (rendering/solving seam) ``attempts`` times before the operation proceeds.
    On ``search`` the challenge fires **once per slice call** (not per native
    page) — enough to exercise the human-solve path during a fetch. A handler
    failure (unsolvable/EOF) collapses into ``Err(CaptchaSolvingError)`` —
    exactly how an unsolvable real captcha surfaces. This lets a human test
    CLI captcha rendering/handling offline without a live board.
    """

    operation: Literal["authorize", "search", "apply"] = Field(
        title="Captcha trigger operation",
        description="Which mock operation first requires solving a captcha.",
        examples=["apply"],
    )
    attempts: int = Field(
        default=1,
        ge=1,
        le=10,
        title="Solve attempts",
        description="How many captcha challenges to solve before the operation proceeds.",
        examples=[1],
    )


class MockBehaviorConfig(BaseModel):
    """The mock's programmable behavior, read from its own config section.

    Consumed only by :class:`MockClient` as a manual-testing seam — never by
    core. ``per_vacancy`` keys are external ``str`` vacancy ids and override
    ``default_apply`` for those specific vacancies. ``captcha`` scripts an
    offline captcha challenge (see :class:`MockCaptchaConfig`).

    Carries field metadata so the section's JSON schema renders a labeled form
    (see client-contract.md §6.2).
    """

    authorize_error: str | None = Field(
        title="Authorize error",
        description="When set, the mock's ``authorize`` fails with this message (manual-testing only).",
        default=None,
        examples=["mock auth denied"],
    )
    default_apply: MockApplyBehaviorConfig | None = Field(
        title="Default apply outcome",
        description="Outcome applied to every vacancy not listed in ``per_vacancy``.",
        default=None,
    )
    per_vacancy: dict[str, MockApplyBehaviorConfig] = Field(  # lint-ignore[raw-dict]: config map
        default_factory=dict,
        title="Per-vacancy outcomes",
        description="Per-external-id outcome overrides (manual-testing only).",
        examples=[{"mock-1": {"outcome": "error", "message": "mock apply rejected"}}],
    )
    captcha: MockCaptchaConfig | None = Field(
        title="Captcha (manual testing)",
        description="Optional offline captcha challenge to fire before an operation (manual-testing only).",
        default=None,
        examples=[{"operation": "apply", "attempts": 1}],
    )


class MockVacancySalary(BaseModel):
    """Optional salary block of one entry in the mock's ``vacancies`` list.

    The lower bound field is ``from_`` (``from`` is a reserved word) with an
    explicit ``from`` input/output alias, so the authored config (YAML/JSON)
    says ``from:`` and the value lands on ``from_`` — pydantic does not map a
    trailing-underscore field name to ``from`` on its own.
    """

    model_config = ConfigDict(populate_by_name=True, title="Salary")

    from_: int | None = Field(
        default=None,
        validation_alias="from",
        serialization_alias="from",
        title="From",
        description="Lower salary bound, in ``currency`` per month.",
        examples=[250000],
    )
    to: int | None = Field(
        default=None,
        title="To",
        description="Upper salary bound, in ``currency`` per month.",
        examples=[350000],
    )
    currency: str | None = Field(
        default=None,
        title="Currency",
        description="ISO currency code of the bounds (``RUR`` for Russia).",
        examples=["RUR"],
    )
    gross: bool = Field(
        default=False,
        title="Gross",
        description="Whether the bounds are pre-tax (gross) rather than net.",
        examples=[False],
    )


class MockVacancyParams(BaseModel):
    """One canned vacancy in the mock's ``vacancies`` list.

    The mock's ``search_vacancies`` returns exactly these vacancies — offline
    data carried by the ``service.mock`` section itself, never read from disk.
    ``external_id`` is the board-neutral key
    ``behavior.per_vacancy`` matches on and ``apply_to_vacancy`` receives;
    ``description`` is full plaintext (enriched by construction — no snippet
    step needed for the mock).

    Fields carry ``title``/``description``/``examples`` so the section's JSON
    schema renders a labeled, per-service typed form (client-contract.md §6.2).
    """

    external_id: str = Field(
        title="External id",
        description="Board-neutral vacancy identifier; matches ``behavior.per_vacancy``.",
        examples=["mock-1"],
    )
    title: str = Field(title="Title", examples=["Python Backend Developer (Mock)"])
    url: str = Field(
        title="URL",
        description="Absolute page URL of the vacancy.",
        examples=["https://mock.example/vacancies/python-backend-1"],
    )
    company: str | None = Field(
        default=None,
        title="Company",
        description="Employer name; ``null`` when the vacancy is anonymous.",
        examples=["MockCorp"],
    )
    description: str = Field(
        title="Description",
        description="Full plaintext vacancy description (multi-sentence prose).",
    )
    key_skills: list[str] = Field(
        default_factory=list,
        title="Key skills",
        description="Skills/technologies listed on the vacancy.",
        examples=[["Python", "FastAPI"]],
    )
    salary: MockVacancySalary | None = Field(
        default=None,
        title="Salary",
        description="Optional salary bounds of the vacancy.",
    )


class MockSearchEntry(SearchEntryBase):
    """One ordered ``service.mock.searches[]`` entry.

    Self-contained like every search entry: its own query, an optional fetch
    ``window`` (base field), the mock's concrete ``filter``, and its own canned
    ``vacancies`` list (offline data — per entry, so a multi-search pipeline
    exercises distinct pools with no live board).
    """

    model_config = ConfigDict(title="Mock search entry", frozen=True)

    filter: MockSearchParams = Field(
        default_factory=MockSearchParams,
        title="Search filter",
        description="The mock's search filter (see :class:`MockSearchParams`).",
    )
    vacancies: list[MockVacancyParams] = Field(
        default_factory=list,
        title="Vacancies",
        description="Canned vacancies this entry's search returns (offline data, part of the entry).",
        examples=[{"external_id": "mock-1", "title": "Python Backend Developer (Mock)"}],
    )


class MockServiceConfig(BaseModel):
    """Typed ``service.mock`` pipeline.yaml section.

    Implements the contract's ``ServiceConfigSection`` (carries ``resume_id``
    plus the ordered ``searches`` pool); the ``behavior`` sub-block is the
    mock's optional manual-testing seam. The canned three-entry dataset used
    across the test suite is a test fixture (``test/fixtures/mock_vacancies.yaml``),
    not code.

    Fields carry title/description/examples so the section's ``model_json_schema()``
    drives a labeled, per-service typed form in the UI (client-contract.md §6.2).
    """

    resume_id: str = Field(
        title="Resume id",
        description="Service resume identifier, passed verbatim to ``apply_to_vacancy``.",
        examples=["mock-resume-1"],
    )
    searches: tuple[MockSearchEntry, ...] = Field(
        min_length=1,
        title="Search pool",
        description=(
            "Ordered search entries fetched one after another into the same DB; "
            "an entry's position is its stable search_index."
        ),
    )
    behavior: MockBehaviorConfig | None = Field(
        title="Behavior (manual testing)",
        description="Optional programmable offline outcomes for the mock client.",
        default=None,
    )
