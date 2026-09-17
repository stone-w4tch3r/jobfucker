"""Validated HH transport DTOs and persisted authentication state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HHModel(BaseModel):
    """Additive HH response model with strict field typing."""

    model_config = ConfigDict(extra="ignore")


class OAuthTokenResponse(HHModel):
    """Successful OAuth token response."""

    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_in: int = Field(gt=0)
    token_type: str = Field(min_length=1)


class OAuthErrorResponse(HHModel):
    """OAuth error envelope."""

    error: str = Field(min_length=1)
    error_description: str | None = None


class CurrentUserResponse(HHModel):
    """Applicant identity decoded from HH ``/me``.

    ``id``/``auth_type``/``is_applicant`` are the auth-healthcheck minimum;
    the name/email fields are additive and optional (HH nulls them; the wire
    also carries a ``mid_name`` alias of ``middle_name`` — ignored, one
    spelling is enough). Unknown keys stay ignored.
    """

    id: str = Field(min_length=1)
    auth_type: str = Field(min_length=1)
    is_applicant: bool
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    email: str | None = None


class HHErrorItem(HHModel):
    """Minimum HH error item used for challenge classification."""

    type: str | None = None
    value: str | None = None
    captcha_url: str | None = None


class HHErrorEnvelope(HHModel):
    """Additive HH API error envelope."""

    errors: tuple[HHErrorItem, ...] = ()
    request_id: str | None = None
    bad_argument: str | None = None
    description: str | None = None


class VacancySearchItem(HHModel):
    """Minimum catalog item needed before full-detail enrichment.

    Widened additively for the listing-only ``list_vacancies`` path
    (docs/hh/api/response-models.md "Vacancy search item"): the short fields
    the search page already carries are decoded for preview instead of being
    discarded. Every field beyond ``id`` is optional — the enrichment path
    (``SearchService.search``) still reads only ``id``, and ``extra="ignore"``
    keeps unknown upstream fields out. ``published_at`` is not in the
    documented key-field list; it is decoded opportunistically and verified
    against live captures (docs/hh/known-unknowns.md).
    """

    id: str = Field(min_length=1)
    name: str | None = None
    alternate_url: str | None = None
    employer: VacancyEmployer | None = None
    salary: VacancySalary | None = None
    area: VacancySearchArea | None = None
    published_at: str | None = None
    snippet: VacancySearchSnippet | None = None


class VacancySearchResponse(HHModel):
    """Validated native HH vacancy page.

    ``alternate_url`` is the envelope's own web search URL for the executed
    query (docs/hh/api/search.md "Search envelope") — the listing-only path
    surfaces it as the board-provided UI link.
    """

    items: tuple[VacancySearchItem, ...]
    found: int = Field(ge=0)
    page: int = Field(ge=0)
    pages: int = Field(ge=0)
    per_page: int = Field(ge=1)
    alternate_url: str | None = None


class VacancyEmployer(HHModel):
    """Employer fields mapped into the board-neutral vacancy."""

    name: str = Field(min_length=1)


class VacancySearchArea(HHModel):
    """Area display name of one search item (docs/hh/api/response-models.md)."""

    name: str = Field(min_length=1)


class VacancySearchSnippet(HHModel):
    """Query-match fragments of one search item (raw HH markup allowed).

    ``<highlighttext>`` tags may wrap the matched terms; these are listing
    fragments, never the full description (docs/hh/api/response-models.md).
    """

    requirement: str | None = None
    responsibility: str | None = None


class VacancyKeySkill(HHModel):
    """One structured vacancy skill."""

    name: str = Field(min_length=1)


class VacancySalary(HHModel):
    """Structured salary returned by HH vacancy detail."""

    from_: int | None = Field(default=None, validation_alias="from")
    to: int | None = None
    currency: str | None = None
    gross: bool


class VacancyTest(HHModel):
    """Vacancy test requirement block of HH vacancy detail."""

    required: bool = False


class VacancyDetailResponse(HHModel):
    """Minimum full vacancy detail required by the client contract.

    The apply-preflight fields (``archived`` … ``adv_response_url``) are
    additive and default so search enrichment ignores them; apply re-fetches
    the detail fresh before every submission (docs/hh/applications-and-
    negotiations.md).
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    alternate_url: str = Field(min_length=1)
    employer: VacancyEmployer | None = None
    description: str
    key_skills: tuple[VacancyKeySkill, ...] = ()
    salary: VacancySalary | None = None
    archived: bool = False
    closed_for_applicants: bool = False
    has_test: bool = False
    test: VacancyTest | None = None
    response_url: str | None = None
    adv_response_url: str | None = None


class ResumeStatus(HHModel):
    """Publication status of one owned resume (only ``published`` is usable)."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ResumeListItem(HHModel):
    """Minimum resume-list fields required by listing and application validation.

    ``can_publish_or_update`` is genuinely nullable on the wire (docs/hh/api/
    response-models.md) and must not be decoded as a strict boolean.
    """

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    status: ResumeStatus
    can_publish_or_update: bool | None = None


class ResumesPage(HHModel):
    """Validated native HH owned-resumes page.

    The wire envelope is five fields; the leading ``found`` count is ignored
    (additive ``extra="ignore"`` decoding).
    """

    items: tuple[ResumeListItem, ...]
    page: int = Field(ge=0)
    pages: int = Field(ge=0)
    per_page: int = Field(ge=1)


class NegotiationVacancy(HHModel):
    """The vacancy reference inside one negotiation list item."""

    id: str = Field(min_length=1)


class NegotiationListItem(HHModel):
    """Minimum negotiation fields required by apply reconciliation."""

    vacancy: NegotiationVacancy


class NegotiationsPage(HHModel):
    """Validated native HH active-negotiations page."""

    items: tuple[NegotiationListItem, ...]
    page: int = Field(ge=0)
    pages: int = Field(ge=0)
    per_page: int = Field(ge=1)


class CaptchaImageKeyResponse(HHModel):
    """Standalone CAPTCHA image-key response."""

    key: str = Field(min_length=1)


class HHCaptchaStatus(HHModel):
    """Nested standalone CAPTCHA submission status."""

    captcha_error: bool = Field(default=False, validation_alias="captchaError")


class CaptchaSubmissionResponse(HHModel):
    """Standalone CAPTCHA wrong-answer response."""

    hhcaptcha: HHCaptchaStatus | None = None


class LoginCaptchaStatus(HHModel):
    """Embedded credential-login CAPTCHA state returned by the HH page model."""

    is_bot: bool = Field(validation_alias="isBot")
    captcha_state: str | None = Field(default=None, validation_alias="captchaState")
    captcha_error: bool | None = Field(default=None, validation_alias="captchaError")
    captcha_key: str | None = Field(default=None, validation_alias="captchaKey")


class LoginErrorStatus(HHModel):
    """Machine-readable credential rejection embedded in a login response."""

    code: str = Field(min_length=1)


class LoginResponse(HHModel):
    """Minimum additive model for credential and CAPTCHA login responses."""

    hhcaptcha: LoginCaptchaStatus | None = None
    login_error: LoginErrorStatus | None = Field(default=None, validation_alias="loginError")
    user_type: str | None = Field(default=None, validation_alias="userType")


class PersistedCookie(BaseModel):
    """Portable subset of an HH website cookie."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: str
    domain: str = Field(min_length=1)
    path: str = "/"
    secure: bool = True
    expires: int | None = None


class PersistedAuthState(BaseModel):
    """Versioned atomic authentication snapshot."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_at: float = Field(gt=0)
    cookies: tuple[PersistedCookie, ...] = ()
