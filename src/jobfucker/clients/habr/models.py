"""Validated Habr transport DTOs and persisted session state.

Every wire payload the client reads is decoded into a strict model here before it
touches business logic. Additive response models use ``extra="ignore"`` (Habr
sends more keys than the client needs); the persisted session uses
``extra="forbid"`` so a corrupt snapshot is treated as "no state".

``meta.logoutToken`` is deliberately not modelled: an unmodelled key cannot be
persisted or logged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobfucker.clients.shared.cookies import PersistedCookie


class HabrModel(BaseModel):
    """Additive Habr response model with strict field typing."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- Identity ----------------------------------------------------------------


class HabrUser(HabrModel):
    """The applicant fields the contract identity is mapped from."""

    alias: str = Field(min_length=1)
    full_name: str | None = Field(default=None, validation_alias="fullName")
    email: str | None = None


class HabrIdentityResponse(HabrModel):
    """``GET /api/frontend_v1/users/me`` payload.

    Authenticated carries ``user``; an anonymous caller gets ``200 {}``. Auth is
    decided by the presence of ``user``, never by the status code.
    """

    user: HabrUser | None = None


# --- Listing -----------------------------------------------------------------


class HabrCompany(HabrModel):
    """Company chip of a listing item; ``title`` is null when concealed."""

    title: str | None = None


class HabrSalary(HabrModel):
    """Stated salary range (``currency`` lowercased on the wire)."""

    from_: int | None = Field(default=None, validation_alias="from")
    to: int | None = None
    currency: str | None = None


class HabrLocation(HabrModel):
    """One location chip (``title`` is the display name, e.g. "Москва")."""

    title: str = Field(min_length=1)


class HabrSkill(HabrModel):
    """One structured skill name."""

    title: str = Field(min_length=1)


class HabrPublishedDate(HabrModel):
    """Publication stamp (``date`` is ISO-8601 with offset)."""

    date: str = Field(min_length=1)


class HabrResponseState(HabrModel):
    """Apply-mode signal carried by listing items and the detail object."""

    kind: str = Field(min_length=1)


class HabrListingItem(HabrModel):
    """One ``list[]`` element of the search endpoint."""

    id: int
    href: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: HabrCompany | None = None
    salary: HabrSalary | None = None
    locations: tuple[HabrLocation, ...] = ()
    skills: tuple[HabrSkill, ...] = ()
    published_date: HabrPublishedDate | None = Field(default=None, validation_alias="publishedDate")
    qualification: str | None = None
    employment: str | None = None
    archived: bool = False
    hidden: bool = False
    response: HabrResponseState | None = None

    @field_validator("locations", "skills", mode="before")
    @classmethod
    def _null_to_empty(cls, value: object) -> object:  # lint-ignore[restricted-object]: JSON list boundary
        """Treat a JSON ``null`` list field as an empty list.

        Live Habr sends ``locations: null`` (and ``skills: null``) for a large
        share of listing items; the strict tuple typing must not reject the
        whole page over an absent optional list.
        """
        return () if value is None else value


class HabrListingMeta(HabrModel):
    """Envelope metadata of one listing page."""

    total_results: int = Field(ge=0, validation_alias="totalResults")
    per_page: int = Field(ge=0, validation_alias="perPage")
    current_page: int = Field(ge=0, validation_alias="currentPage")
    total_pages: int = Field(ge=0, validation_alias="totalPages")


class HabrListingResponse(HabrModel):
    """Validated native Habr listing page."""

    items: tuple[HabrListingItem, ...] = Field(validation_alias="list")
    meta: HabrListingMeta


# --- Detail (SSR state) ------------------------------------------------------


class HabrVacancyDetail(HabrListingItem):
    """The detail object: a listing item plus the full ``description`` HTML.

    ``description`` is real HTML after ``json.loads`` unescapes the SSR blob; it
    is normalized to text by the shared ``DescriptionParser`` upstream.
    """

    description: str = ""


class HabrSsrState(HabrModel):
    """The parsed ``script[data-ssr-state=true]`` JSON, narrowed to its vacancy."""

    vacancy: HabrVacancyDetail


# --- Error envelopes ---------------------------------------------------------


class HabrErrorMessage(HabrModel):
    """Object form of an error: ``{"error": {"message": "…"}}``."""

    message: str = Field(min_length=1)


class HabrErrorEnvelope(HabrModel):
    """Plain error envelope: ``{"error": "Not found"}`` or ``{"error": {"message": …}}``."""

    error: str | HabrErrorMessage | None = None

    @property
    def message(self) -> str | None:
        """The human message from either accepted ``error`` shape."""
        match self.error:
            case str() as text:
                return text
            case HabrErrorMessage(message=message):
                return message
            case None:
                return None


class HabrStructuredError(HabrModel):
    """The ``/api/frontend_v1/responses*`` structured error envelope."""

    http_code: int = Field(validation_alias="httpCode")
    error_code: str = Field(validation_alias="errorCode")
    message: str = Field(min_length=1)


# --- Login -------------------------------------------------------------------


class HabrLoginResponse(HabrModel):
    """``POST /ru/ident/in/<state>`` submit contract."""

    success: bool
    rurl: str | None = None
    error: str | None = None


# --- SmartCaptcha challenge --------------------------------------------------


class HabrPow(HabrModel):
    """Proof-of-work challenge carried with every SmartCaptcha step."""

    prefix: str = Field(min_length=1)
    complexity: int = Field(ge=1)


class HabrCaptchaChallenge(HabrModel):
    """The ``captcha`` object of a ``/check`` response.

    ``type`` is kept as a free string so an unknown challenge type is detectable
    as data (and fails closed) rather than a validation error.
    """

    type: str = Field(min_length=1)
    key: str = Field(min_length=1)
    image: str | None = None


class HabrCaptchaResponse(HabrModel):
    """One ``smartcaptcha.cloud.yandex.ru/check`` response."""

    status: str = Field(min_length=1)
    captcha: HabrCaptchaChallenge | None = None
    pow: HabrPow | None = None
    spravka: str | None = None
    unique_key: str | None = None


# --- Persisted session -------------------------------------------------------


class PersistedHabrIdentity(BaseModel):
    """Cached identity of the authorized account (never a secret)."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1)
    display_name: str | None = None
    email: str | None = None


class PersistedHabrSession(BaseModel):
    """Versioned atomic session snapshot: cookie jar + cached identity."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    cookies: tuple[PersistedCookie, ...] = ()
    identity: PersistedHabrIdentity | None = None
