"""The frozen client contract boundary.

This module is the single typed, board-neutral interface between the **core**
(engine, stages, storage, UI) and any **client** (HH today; habr
career, getmatch later). It is ported **exactly** from
``docs/specs/client-contract.md`` and is the handoff artifact between the core
build track and the HH-client build track: neither side may assume details of
the other beyond what is defined here.

Boundary rules honoured (see client-contract.md):
- ``base.py`` imports **no** board code and contains **no** board-specific
  identifiers (no ``hh``-prefixed names anywhere). All types are vendor
  neutral.
- Every ``Client`` method returns ``Result[T, ClientError]`` — expected
  failures never raise.
- ``CaptchaHandler`` and ``AuthInteractionProvider`` are shared,
  board-agnostic seams defined here so both tracks build against the same
  types; captcha solving/display is client-internal and never a ``ClientError``
  category (no ``CaptchaRequiredError``).

Authored by the **Core** track; the concrete HH client lives in the HH track
(``clients/hh/``) and implements this boundary on merge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NewType, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from rusty_results.prelude import Result

from jobfucker.reporting import NullReporter, Reporter


# --- 1. Service identity & limits -----------------------------------------
@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """Static, board-provided metadata the core reasons about generically."""

    service: str  # config `service` value; selects this client in the factory ("hh", ...)
    per_auth_daily_cap: int  # board's global per-auth daily application cap (HH -> 200)
    max_search_items: int | None = None  # board's max searchable listing items (HH -> 2000); None = no known cap


# --- 1a. Search pool (multi-search fetch) ------------------------------------
# Product restriction, stricter than the board API invariant (1..100): the CLI
# accepts only the page sizes the board's web UI offers (20/50/100), so a
# fetch window stays comparable with what a human could have picked.
ALLOWED_PAGE_SIZES: Final[frozenset[int]] = frozenset({20, 50, 100})


class SearchWindow(BaseModel):
    """One search entry's fetch window/slice controls (all 0-based listing positions).

    Single source for the window/slice vocabulary shared by the CLI flags and
    the pipeline.yaml ``searches[].window`` block: the CLI projects its flags
    here, a config entry validates its ``window`` block directly into this
    model, and the whole validation matrix (page sizes, group exclusivity,
    cap bounds) lives in exactly one place —
    :func:`jobfucker.stages.fetch_plan.plan_fetch`, which consumes this model.
    ``first_page``/``page_size``/``take_pages`` define the fetch window
    ``W = [first_page*page_size, (first_page + take_pages)*page_size)``;
    ``from_``/``to``/``take`` further select a slice inside ``W``.

    Config spelling uses the CLI flag words (``from`` is the yaml key of
    ``from_``); pydantic validates types here, never window semantics.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    first_page: int = Field(default=0, title="First page", description="Zero-based first native page of the window.")
    page_size: int = Field(
        default=100,
        title="Page size",
        description="Vacancies per native page; one of 20, 50, 100.",
    )
    take_pages: int = Field(default=10, title="Take pages", description="Native pages in the fetch window.")
    from_: int | None = Field(
        default=None,
        validation_alias="from",
        serialization_alias="from",
        title="Slice from",
        description="0-based listing position to start the persisted slice at.",
    )
    to: int | None = Field(
        default=None,
        title="Slice to",
        description="0-based inclusive end position of the persisted slice; mutually exclusive with ``take``.",
    )
    take: int | None = Field(
        default=None,
        title="Slice take",
        description="Vacancy count to persist; mutually exclusive with ``to``.",
    )


class SearchEntryBase(BaseModel):
    """Board-neutral base of one ``service.<board>.searches[]`` entry.

    A search entry is self-contained: its own ``query`` (board query-language
    string, opaque to core) and an optional fetch ``window``. Board-specific
    fields (the concrete ``filter`` model) live only on the per-board subclass
    and are never visible to core — core reads entries through the
    :class:`ServiceConfigSection` protocol as ``Sequence[SearchEntryBase]``.
    The entry's stable identity is its **zero-based position** in the
    ``searches`` tuple (the ``search_index`` clients receive per call).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(title="Query", description="Board query-language search text for this entry.")
    window: SearchWindow | None = Field(
        default=None,
        title="Fetch window",
        description="Optional per-entry fetch window/slice; omitted = the default window.",
    )


# --- 2. Vendor-neutral models ----------------------------------------------
# Service-scoped identifier for a listing/vacancy on the board. Kept as a
# distinct type so the core cannot mix it with an internal DB id or a resume id.
ServiceVacancyId = NewType("ServiceVacancyId", str)


@dataclass(frozen=True, slots=True)
class Salary:
    """Reported salary range for a vacancy (board-independent)."""

    from_: int | None
    to: int | None
    currency: str | None  # ISO 4217, e.g. "RUR" (None when unspecified)
    gross: bool


@dataclass(frozen=True, slots=True)
class Vacancy:
    """A job listing, board-independent and always fully enriched."""

    external_id: ServiceVacancyId
    title: str
    url: str
    company: str | None
    description: str  # FULL plaintext description (enriched by the client)
    key_skills: tuple[str, ...]
    salary: Salary | None
    # Optional hh test-presence flag. The HH client reports it from
    # vacancy detail at fetch time; clients without the notion leave it None.
    has_hh_test: bool | None = None


@dataclass(frozen=True, slots=True)
class ResumeInfo:
    """A board-side resume, used verbatim in ``apply_to_vacancy``."""

    resume_id: str  # service resume id; passed straight into apply_to_vacancy()
    title: str  # resume headline, for display/selection
    updated_at: str | None  # ISO-8601 date-time if the board reports it


# --- Search slice result (search_vacancies payload) -------------------------
@dataclass(frozen=True, slots=True)
class SearchPageFailure:
    """A native page that failed mid-slice; the walk stopped at its start.

    Carried inside a successful :class:`SearchSlice` (partial result), never as
    a bare ``Err`` — everything before the failed page is already loaded and
    the caller can persist it. ``page`` is the zero-based native page number.
    """

    page: int
    error: ClientError


@dataclass(frozen=True, slots=True)
class SearchSlice:
    """The outcome of one slice-level ``search_vacancies`` call.

    Invariants (clients must uphold them; the shared driver in
    ``jobfucker.clients.paging`` does):

    - ``items`` are fully enriched, in native listing order, and cover exactly
      the global positions ``[offset, offset + len(items))``. The walk stops at
      ``limit`` items, or earlier when the listing ends (``exhausted``) or a
      page fails (``failure``). With ``exclude``, ``items`` are the non-excluded
      subset of those positions (native order kept); exclusion filters enriched
      items only, never the walk geometry.
    - Only positions inside ``[offset, offset + limit)`` are ever enriched —
      board items outside the requested range must not be detail-fetched.
      Excluded ids inside the range are not enriched either (the client skips
      their detail fetch entirely; the driver drops them from ``items`` as a
      guarantee).
    - ``scanned_start``/``scanned_end`` are **page-aligned** bounds of the
      native pages actually fetched (e.g. a slice [150, 250) at page_size 100
      scans [100, 300)): they delimit the caller's freshness sweep.
    - ``exhausted`` means the listing ended (a page listed fewer than
      ``page_size`` items) before ``offset + limit`` was reached.
    - ``failure`` is set when a native page failed mid-slice: ``items`` holds
      everything loaded before it. ``None`` on a complete walk.
    """

    items: tuple[Vacancy, ...]
    offset: int  # global listing position of items[0]
    scanned_start: int  # page-aligned start of the actually scanned range
    scanned_end: int  # page-aligned exclusive end of the actually scanned range
    pages_scanned: int  # native pages actually fetched
    pages_planned: int  # native pages the slice required
    exhausted: bool  # listing ended before offset + limit was filled
    failure: SearchPageFailure | None = None


# --- Listing-only search (list_vacancies payload) ----------------------------
@dataclass(frozen=True, slots=True)
class VacancyShort:
    """Listing-level vacancy info straight off the board's search page.

    No enrichment: every field is what the board's listing/search response
    already carries (HH: the "Vacancy search item" shape, docs/hh/api/
    response-models.md — the same shape ``VacancySearchItem`` decodes). Snippet
    fields are raw board markup (HH ``<highlighttext>`` tags allowed) — they
    are query-match fragments, never the full description. Kept next to
    :class:`Vacancy` on purpose: Vacancy is the enriched form, VacancyShort is
    the cheap listing form; both must be updated together if the contract
    evolves.
    """

    external_id: ServiceVacancyId
    title: str
    url: str  # website (alternate) URL
    company: str | None  # None when the board conceals the employer
    salary: Salary | None
    area: str | None  # display name (e.g. "Москва") when the board reports it
    published_at: str | None  # ISO datetime string as the board reports it
    snippet_requirement: str | None
    snippet_responsibility: str | None


@dataclass(frozen=True, slots=True)
class SearchListing:
    """The outcome of one listing-only ``list_vacancies`` call.

    Invariants (the shared driver ``jobfucker.clients.paging.scan_listing``
    upholds them for every client):

    - ``items`` are the listing's short items in native order and cover exactly
      the global positions ``[offset, offset + len(items))`` — the pages the
      walk trimmed to the requested window, with no enrichment ever performed.
    - ``exhausted`` means the listing ended (a page listed fewer than
      ``page_size`` items) before the full window was walked.
    - ``found``/``ui_url`` are the board's own envelope metadata (total count,
      the board's web search URL for the executed query); either may be
      ``None`` when a board does not report it. ``ui_url`` is a display URL —
      a convenience for humans, not a guarantee it reproduces every filter.
    - There is no partial-failure member: listing results are never persisted,
      so a failed page is a plain ``Err`` (see ``search_vacancies`` for the
      contrasting persisted-slice semantics).
    """

    items: tuple[VacancyShort, ...]
    offset: int  # global listing position of items[0]
    pages_scanned: int  # native pages actually fetched
    pages_planned: int  # native pages the window required
    exhausted: bool  # listing ended before the window was filled
    found: int | None  # board-reported total result count
    ui_url: str | None  # board's own web search URL for the executed query


# --- 3. Apply outcome -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ApplySucceeded:
    """The board confirmed that the application was submitted."""


ApplySkipReason = Literal[
    "already_applied",
    "vacancy_unavailable",
    "test_required",
    "external_application",
]


@dataclass(frozen=True, slots=True)
class ApplySkip:
    """A deterministic reason why no application should be submitted."""

    reason: ApplySkipReason
    text: str
    redirect_url: str | None = None


@dataclass(frozen=True, slots=True)
class ApplySkipped:
    """A successful classification that intentionally did not submit."""

    skip: ApplySkip


@dataclass(frozen=True, slots=True)
class ApplyError:
    """A deterministic per-vacancy rejection that does not stop the batch."""

    text: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyFailed:
    """A classified application rejection."""

    error: ApplyError


ApplyResult = ApplySucceeded | ApplySkipped | ApplyFailed


# --- 5. Apply-system interaction protocol ----------------------------------
@runtime_checkable
class Client(Protocol):
    """The board-agnostic client interface every client implements.

    **Asynchronous** (per the async-migration spec D4): every method is
    ``async def`` and returns ``Result[T, ClientError]``; expected failures
    never raise. The future HH client must use ``httpx.AsyncClient``.
    """

    service: str  # = ServiceInfo(...).service
    service_info: ServiceInfo

    async def authorize(self) -> Result[None, ClientError]: ...

    async def search_vacancies(
        self,
        search_index: int,  # 0-based pool position; selects the client's prebuilt (query, filter) pair
        *,
        offset: int = 0,  # 0-based listing position of the first item wanted
        limit: int = 100,  # items wanted: positions [offset, offset + limit)
        page_size: int = 100,  # native page size the client should walk with
        exclude: frozenset[ServiceVacancyId] = frozenset(),  # stored ids the caller already has; not requested/enriched
    ) -> Result[SearchSlice, ClientError]: ...

    # Err = pre-scan failures only (auth preflight, invalid arguments, config).
    # A mid-slice page failure is a partial Ok(SearchSlice, failure=...) so the
    # caller can persist progress (see SearchSlice). Pagination and slice
    # trimming happen client-side (shared driver: jobfucker.clients.paging):
    # only native pages overlapping [offset, offset + limit) are requested and
    # only items inside that range are enriched. The window/slice selection is
    # a fetch-plan concern resolved by the caller; the client never sees it —
    # only the resolved offset/limit/page_size and the entry index.

    async def list_vacancies(
        self,
        search_index: int,  # 0-based position in the pipeline's search pool
        *,
        offset: int = 0,  # 0-based listing position of the first item wanted
        limit: int = 100,  # items wanted: positions [offset, offset + limit)
        page_size: int = 100,  # native page size the client should walk with
    ) -> Result[SearchListing, ClientError]: ...

    # Listing-only counterpart of ``search_vacancies`` (the preview path): walk
    # the same native pages via the shared driver (jobfucker.clients.paging.
    # scan_listing) but decode only the listing-level short fields — no detail
    # fetches, no persistence. ``Err`` covers pre-scan failures AND any page
    # failure (nothing is persisted, so a partial result has no value). No
    # ``exclude``: a preview wants the full listing, already-stored vacancies
    # included (the caller joins them against its own store).

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]: ...

    async def apply_to_vacancy(
        self,
        *,
        resume_id: str,  # service resume id (resolved by the core, passed in)
        vacancy_id: ServiceVacancyId,  # external_id from Vacancy
        message: str | None = None,  # cover letter to attach, if the board supports it
    ) -> Result[ApplyResult, ClientError]: ...

    async def aclose(self) -> None: ...


# --- 6. ClientError taxonomy (closed union, typed signals) ------------------
@dataclass(frozen=True, slots=True)
class AuthError:
    """Authentication failed or re-auth is needed; core halts, never auto-retries."""

    message: str


@dataclass(frozen=True, slots=True)
class ConfigurationError:
    """Client configuration is invalid for the requested operation."""

    message: str


@dataclass(frozen=True, slots=True)
class NotFoundError:
    """A resource the board was asked for does not exist."""

    message: str


@dataclass(frozen=True, slots=True)
class BadRequestError:
    """The board rejected the request as malformed/declined."""

    message: str


@dataclass(frozen=True, slots=True)
class InternalError:
    """Internal client error"""

    message: str


@dataclass(frozen=True, slots=True)
class TransportError:
    """A transport/network-level failure (DNS/connect/parse/round-trip)."""

    message: str
    status: int | None = None  # HTTP status if round-tripped, else None


@dataclass(frozen=True, slots=True)
class ProtocolError:
    """The upstream response violates the endpoint contract."""

    message: str
    status: int | None = None


@dataclass(frozen=True, slots=True)
class CaptchaSolvingError:
    """Failed to solve a captcha (the challenge was not cleared)."""

    message: str
    recovery_url: str | None = None


@dataclass(frozen=True, slots=True)
class LimitExceededError:
    """STOP signal: the board's per-auth daily cap is reached (HH 400 limit_exceeded)."""

    message: str


@dataclass(frozen=True, slots=True)
class UnknownApplyOutcomeError:
    """An application may have been accepted and must not be replayed."""

    message: str


# The closed set of expected board outcomes the core can exhaustively switch on.
# NOTE: `RedirectError` is deliberately NOT a member — a redirect is mapped by
# the client to `ApplySkipped(ApplySkip(..., redirect_url=...))`, never a
# `ClientError` (see client-contract.md §5 table). Captcha is client-internal
# (no `CaptchaRequiredError`); an unsolvable challenge collapses into an
# ordinary `CaptchaSolvingError`/`AuthError`/apply failure.
ClientError = (
    AuthError
    | ConfigurationError
    | NotFoundError
    | BadRequestError
    | InternalError
    | TransportError
    | ProtocolError
    | CaptchaSolvingError
    | LimitExceededError
    | UnknownApplyOutcomeError
)


# --- 7. Shared interaction & captcha seams (contract-level) -----------------
# Display/solve-agnostic: takes PNG bytes of a captcha, returns the recognised
# entered text. A CLI handler prints sixel/kitty and reads a TTY; a GUI handler
# shows the PNG in a dialog. Both satisfy this exact callable. **Asynchronous**
# per the async-migration spec (fully-async end to end): a handler may block on
# human input (terminal read / dialog) and must remain awaitable — blocking
# prompts run off the loop inside the implementation (e.g. ``asyncio.to_thread``).
CaptchaHandler = Callable[[bytes], Awaitable[Result[str, str]]]


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """Resolved board login credentials supplied by the composition root."""

    login: str
    password: str


class AuthInteractionProvider(Protocol):
    """Injectable human-interaction seam for the auth flow.

    The client never calls ``input()``/reads the TTY directly; every interactive
    step goes through this provider. Both the CLI (terminal prompts) and the Qt
    GUI (dialogs) implement and inject it. **Async** per the async-migration
    spec (fully-async end to end): a prompt may block on human input and must
    remain awaitable — blocking reads run off the loop inside the implementation
    (e.g. ``asyncio.to_thread``).
    """

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        """Ask the human for a one-time code (SMS/email 2FA), returning it."""
        ...

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        """Ask the human to confirm an action (e.g. 'captcha solved, proceed?')."""
        ...


@dataclass(frozen=True, slots=True)
class ClientDeps:
    """Client construction contract: the deps the composition root builds once.

    ``data_dir`` is the runtime dir where cookies/tokens/session live (see
    runtime isolation); ``auth_interaction`` and ``captcha_handler`` are the
    injected board-agnostic seams above; ``reporter`` is the shared run-output
    sink (``jobfucker.reporting``) a client emits board-operation detail on.
    It is **never** ``None`` — GUI, terminal, and tests each inject a concrete
    reporter (the default no-op :class:`NullReporter` keeps silent construction
    like tests working without a guard). Contract §8.
    """

    service: str  # config `service`, also identifies the profile
    profile_id: str | None  # isolates per-profile session/cookies/tokens
    data_dir: Path  # runtime dir: cookies/tokens/session live here
    credentials: ClientCredentials  # resolved values; clients never read credential files
    auth_interaction: AuthInteractionProvider  # UI-agnostic human prompts (§7.1)
    captcha_handler: CaptchaHandler  # display/solve-agnostic captcha solver (§7.1)
    reporter: Reporter = field(default_factory=NullReporter)  # live run-output sink (§8)


# --- 8. Service config section (client self-configuration) ------------------
class ServiceConfigSection(Protocol):
    """Typed base for a board's ``service.<board>`` pipeline.yaml section.

    Implemented by a per-board pydantic model validated at the config boundary
    (never a raw dict). ``resume_id`` is the one field every board's section
    carries (used by ``apply_to_vacancy``); ``searches`` is the ordered search
    pool core iterates at fetch time — read through this protocol, each entry
    narrows to :class:`SearchEntryBase` (query + optional window only). The
    per-entry board ``filter`` and any other board-specific fields live on the
    concrete section/entry types only, where each client derives its per-entry
    search behavior at ``(deps, section)`` construction.

    Properties are declared read-only so frozen and mutable section models
    satisfy the protocol.
    """

    @property
    def resume_id(self) -> str: ...

    @property
    def searches(self) -> Sequence[SearchEntryBase]: ...
