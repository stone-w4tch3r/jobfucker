# Client Contract

## Purpose

The client contract is the typed, board-neutral boundary between the jobfucker core and a job-board
client. Core code depends only on this contract. Board clients own transport, authentication,
response decoding, board-specific configuration, and mapping into the contract models.

```text
CLI / GUI
    │
    ▼
engine and stages
    │
    ▼
Client protocol + board-neutral models
    │
    ├── mock client
    ├── HH client
    └── future board clients
```

The concrete HH implementation is specified in [HH.ru Client](hh-client.md). Observed HH behavior
lives in the [HH.ru integration wiki](../hh/README.md).

## Boundary rules

- `clients/base.py` contains no board-specific identifiers or imports.
- Every expected client failure is represented by `Result`; exceptions indicate programmer errors
  or violated construction invariants.
- External responses are validated inside clients before they become contract models.
- Core never receives raw board JSON, cookies, tokens, or board-specific filter dictionaries.
- Client methods are asynchronous and must not block an event loop with terminal or file I/O.
- Every constructed client has an explicit asynchronous cleanup path.

## Models

```python
from dataclasses import dataclass
from typing import Literal, NewType

ServiceVacancyId = NewType("ServiceVacancyId", str)

@dataclass(frozen=True, slots=True)
class ServiceInfo:
    service: str
    # Conservative product safety policy, not necessarily the board's actual cap.
    per_auth_daily_cap: int
    # Board's maximum number of searchable listing items (e.g. HH: 2000); None
    # means no known cap. The fetch stage validates its window against it before
    # any request (an offset beyond the cap is a board-side 400).
    max_search_items: int | None = None

@dataclass(frozen=True, slots=True)
class Salary:
    from_: int | None
    to: int | None
    currency: str | None
    gross: bool

@dataclass(frozen=True, slots=True)
class Vacancy:
    external_id: ServiceVacancyId
    title: str
    url: str
    company: str | None
    description: str
    key_skills: tuple[str, ...]
    salary: Salary | None
    # Board-named member (see "Board-scoped capabilities"): the
    # fetch-time "this vacancy carries an employer screening test" fact. None =
    # unknown/board has no such notion; True/False = the board reported it.
    has_hh_test: bool | None = None

@dataclass(frozen=True, slots=True)
class SearchPageFailure:
    # Zero-based native page that failed mid-slice; the walk stopped at its start.
    page: int
    error: ClientError

@dataclass(frozen=True, slots=True)
class SearchSlice:
    # Enriched items in native listing order, covering [offset, offset + len(items)).
    items: tuple[Vacancy, ...]
    offset: int
    # Page-aligned bounds of the native pages actually fetched (used by the
    # caller's resume hints after a mid-slice failure).
    scanned_start: int
    scanned_end: int
    pages_scanned: int
    pages_planned: int
    # Listing ended (a page listed fewer than page_size items) before offset+limit.
    exhausted: bool
    # Page failure mid-slice: items holds everything loaded before it. None on a
    # complete walk.
    failure: SearchPageFailure | None = None

@dataclass(frozen=True, slots=True)
class VacancyShort:
    # Listing-level info straight off the board's search page — no enrichment
    # (HH: the "Vacancy search item" shape, docs/hh/api/response-models.md).
    # Kept next to Vacancy; update both together when the contract evolves.
    external_id: ServiceVacancyId
    title: str
    url: str                      # website (alternate) URL
    company: str | None
    salary: Salary | None
    area: str | None              # display name when the board reports it
    published_at: str | None      # ISO datetime string as the board reports it
    snippet_requirement: str | None      # raw board markup allowed
    snippet_responsibility: str | None   # raw board markup allowed

@dataclass(frozen=True, slots=True)
class SearchListing:
    # Short items in native listing order, covering [offset, offset + len(items));
    # no enrichment ever performed. Never persisted, so no partial-failure member.
    items: tuple[VacancyShort, ...]
    offset: int
    pages_scanned: int
    pages_planned: int
    exhausted: bool
    # Board envelope metadata; either may be None when a board does not report
    # it. ui_url is a display URL, not a guarantee it reproduces every filter.
    found: int | None
    ui_url: str | None

@dataclass(frozen=True, slots=True)
class ResumeInfo:
    resume_id: str
    title: str
    updated_at: str | None

@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    # The authenticated board-account identity (a board "whoami").
    # Display fields are optional on purpose: boards report names/emails in
    # different shapes and may null them. Consumers render their own fallbacks
    # (name -> email -> id).
    external_id: str
    display_name: str | None = None
    email: str | None = None

@dataclass(frozen=True, slots=True)
class ApplySucceeded:
    pass

ApplySkipReason = Literal[
    "already_applied",
    "vacancy_unavailable",
    "test_required",
    "external_application",
]

@dataclass(frozen=True, slots=True)
class ApplySkip:
    reason: ApplySkipReason
    text: str
    redirect_url: str | None = None

@dataclass(frozen=True, slots=True)
class ApplySkipped:
    skip: ApplySkip

@dataclass(frozen=True, slots=True)
class ApplyError:
    text: str
    code: str | None = None

@dataclass(frozen=True, slots=True)
class ApplyFailed:
    error: ApplyError

ApplyResult = ApplySucceeded | ApplySkipped | ApplyFailed
```

Model invariants:

- `Vacancy.description` is full normalized text, never a search-result snippet.
- `key_skills` remains structured.
- `resume_id` is the board-side identifier passed unchanged to an application.
- `ApplyResult` is a closed union of deterministic outcomes for one vacancy:
  - `ApplySucceeded` — the board confirms submission;
  - `ApplySkipped` — no submission is needed or supported; its typed `ApplySkip` carries the
    reason, required human-readable text, and an optional external redirect;
  - `ApplyFailed` — the board deterministically rejects this vacancy while the batch may continue;
    its `ApplyError` carries required human-readable text and an optional machine-readable code.
- The variants do not share a string `status` field and do not expose unrelated optional fields.
  Core must narrow the union and read details only from the matching variant.
- A batch stop, authorization failure, configuration failure, or uncertain application is a
  `ClientError`, not an `ApplyResult`.

## Client protocol

```python
from typing import Protocol, runtime_checkable
from rusty_results import Result

@runtime_checkable
class Client(Protocol):
    service: str
    service_info: ServiceInfo

    async def authorize(self) -> Result[None, ClientError]: ...

    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]: ...

    async def list_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
    ) -> Result[SearchListing, ClientError]: ...

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]: ...

    async def get_identity(self) -> Result[ServiceIdentity, ClientError]: ...

    async def apply_to_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None = None,
    ) -> Result[ApplyResult, ClientError]: ...

    async def aclose(self) -> None: ...
```

Method semantics:

- `authorize()` is idempotent. `Ok(None)` means subsequent authenticated operations may run;
  callers never receive a partial authorization state.
- `search_vacancies()` receives the selected search entry (`search_index`, the zero-based
  position in the pipeline's search pool) plus a **slice**: the caller wants the items
  at global listing positions `[offset, offset + limit)`, walked with native pages of
  `page_size`. Pagination and slice trimming are **client-side**: only the native pages
  overlapping the slice are requested and only items inside the range are enriched (for HH that
  means detail `GET`s happen solely for slice items — never for items the caller will drop).
  One call returns a `SearchSlice`; clients share the walk mechanics through
  `jobfucker.clients.paging` (`plan_pages` + `scan_slice`) so a new board cannot re-implement
  (or get wrong) the no-over-fetch invariant. Core validates `offset >= 0`, `limit >= 1`,
  `page_size >= 1` before calling; each client additionally enforces its board-specific
  maximum page size. The optional `exclude` set names `external_id`s the caller already
  stores: those items are **skipped on the client** — the client does not enrich them
  (for HH that means no detail `GET` at all), and `scan_slice` drops them from `items` as
  a guarantee for every client. Exclusion filters enriched items only, never the walk
  geometry: pages are still walked (that is how new items are discovered), `listed` still
  counts native page items, and exhaustion detection is unchanged. The constructed client
  owns its typed service filter and search mode. An
  empty query is valid but does not select an endpoint implicitly. Persistence is the caller's
  business: the core fetch stage treats the slice as an insert-only mirror sync (existing rows
  are never overwritten; `--refresh` overwrites listing fields only), so absence from a listing
  never soft-deletes anything.
- `Err` from `search_vacancies()` is reserved for **pre-scan** failures: auth preflight, invalid
  paging arguments, client configuration. Any failure while walking a page (transport, board
  rejection, decode, enrichment) yields `Ok(SearchSlice)` with everything loaded before the
  failure plus a `failure: SearchPageFailure` descriptor (partial result), so the caller can
  persist progress — the failure carries the zero-based native `page` and the `ClientError`.
- `SearchSlice` invariants: `items` are fully enriched, in native listing order, covering exactly
  `[offset, offset + len(items))` (with `exclude`, `items` are the non-excluded subset of those
  positions, native order kept); the walk stops early on a short page (`exhausted = True`, the
  listing ended) or on a page failure (`failure` set). `scanned_start`/`scanned_end` are the
  **page-aligned** bounds of the native pages actually fetched (used for the caller's resume
  hints after a mid-slice failure; a slice `[150, 250)` at `page_size` 100 scans `[100, 300)`).
  `pages_scanned`/`pages_planned` report the walk for messages and audit.
- `list_vacancies()` is the **listing-only** counterpart of `search_vacancies()` (the preview
  path): it receives the same entry-selected filter surface + window semantics and walks the same native pages through
  the shared driver (`jobfucker.clients.paging.plan_pages` + `scan_listing`), but decodes only
  the listing-level short fields — **no detail fetch, no persistence, no `exclude`** (a preview
  wants the whole listing, already-stored vacancies included; the caller joins them against its
  own store). `items` cover exactly `[offset, offset + len(items))`. Envelope metadata rides
  along: `found` (board-reported total) and `ui_url` (the board's own web search URL for the
  executed query — a display convenience, not a filter-exactness guarantee); either is `None`
  when the board does not report it. There is **no** partial-failure member: a listing is never
  persisted, so a failed page is a plain `Err` (pre-scan failures included).
- `get_resumes()` returns every board-side resume the client can map to `ResumeInfo`.
- `get_identity()` returns the authenticated account's identity (a board "whoami"). Boards SHOULD
  reuse their auth healthcheck response instead of issuing an extra request (HH decodes `/me`
  once per preflight and caches it). `external_id` is the only required member.
- `apply_to_vacancy()` receives the explicit service resume ID selected by core.
- `aclose()` releases owned transports/resources, is safe after partial initialization, and is
  idempotent.

## Error types

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class AuthError:
    message: str

@dataclass(frozen=True, slots=True)
class ConfigurationError:
    message: str

@dataclass(frozen=True, slots=True)
class NotFoundError:
    message: str

@dataclass(frozen=True, slots=True)
class BadRequestError:
    message: str

@dataclass(frozen=True, slots=True)
class InternalError:
    message: str

@dataclass(frozen=True, slots=True)
class TransportError:
    message: str
    status: int | None = None

@dataclass(frozen=True, slots=True)
class ProtocolError:
    message: str
    status: int | None = None

@dataclass(frozen=True, slots=True)
class CaptchaSolvingError:
    message: str
    recovery_url: str | None = None

@dataclass(frozen=True, slots=True)
class LimitExceededError:
    message: str

@dataclass(frozen=True, slots=True)
class UnknownApplyOutcomeError:
    message: str

ClientError = (
    AuthError
    | ConfigurationError
    | NotFoundError
    | BadRequestError
    | TransportError
    | InternalError
    | ProtocolError
    | CaptchaSolvingError
    | LimitExceededError
    | UnknownApplyOutcomeError
)
```

| Error | Core behavior |
| --- | --- |
| `AuthError` | Stop the operation and require authorization recovery |
| `ConfigurationError` | Stop or quarantine the pipeline; retrying unchanged input is invalid |
| `NotFoundError` | Record according to stage semantics; do not retry unchanged |
| `BadRequestError` | Reject invalid input or request; do not retry unchanged |
| `TransportError` | Retry only when the operation is known to be safe |
| `ProtocolError` | Stop and report upstream contract drift |
| `CaptchaSolvingError` | Stop after client-owned bounded solving; surface `recovery_url` when present |
| `LimitExceededError` | Stop the apply batch and leave unattempted vacancies pending |
| `UnknownApplyOutcomeError` | Do not replay automatically; require reconciliation |

Redirects are not a generic error. An external application redirect maps to
`ApplySkipped(ApplySkip(reason="external_application", text=..., redirect_url=...))`.

## Board-scoped capabilities

Generic layers stay board-free. When the core must invoke a *board-only* behavior (something the
board-neutral `Client` protocol has no business promising every client), it does so through a
small board-named capability protocol plus a dedicated board-named module, reached through a
runtime `isinstance` gate — never by widening `Client`. The concrete example is
`jobfucker.hh_tests.contract.HhTestCapable`, implemented by the HH client: the apply stage checks
`isinstance(client, HhTestCapable)` and takes the screening-test path only for clients that opt
in; every other client (mock, future boards) is excluded by construction. Base `Client` remains
the only required protocol; the capability, its models, solvers, and prompt live in the board-named
package (`jobfucker/hh_tests/`), not in `clients/base.py`.

A board name may appear in generic core in ONLY if it has associated core level board-specific capability. Eg the persisted
`Vacancy.has_hh_test` flag, mirrored by the `vacancies.has_hh_test` column and the
`apply --has-hh-tests` filter.

## Core outcome handling

Core handles every `ApplyResult` variant explicitly and persists its own storage status at this
boundary. It must not infer an outcome from optional fields or client-specific text.

```python
from dataclasses import replace
from typing import assert_never

match apply_result:
    case ApplySucceeded():
        updated = replace(
            vacancy,
            apply_status="applied",
            apply_error=None,
            applied_at=now,
            applied_snapshot_id=snapshot_id,
        )
    case ApplySkipped(skip=skip):
        updated = replace(
            vacancy,
            apply_status="skipped",
            apply_error=None,
            applied_snapshot_id=snapshot_id,
        )
    case ApplyFailed(error=error):
        updated = replace(
            vacancy,
            apply_status="error",
            apply_error=error.text,
            applied_snapshot_id=snapshot_id,
        )
    case unexpected:
        assert_never(unexpected)

await storage.vacancies.update(updated)
```

## Construction dependencies

The composition root resolves user configuration and injects dependencies explicitly:

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from rusty_results import Result

CaptchaHandler = Callable[[bytes], Awaitable[Result[str, str]]]

@dataclass(frozen=True, slots=True)
class ClientCredentials:
    login: str
    password: str

class AuthInteractionProvider(Protocol):
    async def request_code(self, *, prompt: str) -> Result[str, str]: ...
    async def confirm(self, *, prompt: str) -> Result[bool, str]: ...

@dataclass(frozen=True, slots=True)
class ClientDeps:
    service: str
    profile_id: str | None
    data_dir: Path
    credentials: ClientCredentials
    auth_interaction: AuthInteractionProvider
    captcha_handler: CaptchaHandler
    reporter: Reporter = field(default_factory=NullReporter)
```

Rules:

- The config loader resolves inline/file-backed credentials before constructing
  `ClientCredentials`. Clients do not perform file I/O for credentials.
- `profile_id` scopes persisted board state without exposing raw credentials in paths.
- The client never reads stdin or creates UI. Human input goes through
  `AuthInteractionProvider`; CAPTCHA image recognition goes through `CaptchaHandler`.
- A handler failure is an expected `Result` failure and is mapped by the client to the relevant
  typed client error.
- `Reporter` is a UI-neutral event sink and is never `None`. Events contain safe progress and
  diagnostic metadata, never secrets.

## Service configuration and factory

Every board defines one strict Pydantic model for its `service.<board>` configuration. The model is
the single source for config validation, client construction, serialization, and schema-driven UI
metadata.

```python
class ServiceConfigSection(Protocol):
    @property
    def resume_id(self) -> str: ...
    @property
    def searches(self) -> Sequence[SearchEntryBase]: ...

class Factory:
    def register(self, service: str, client: type[Client]) -> None: ...
    def get(self, service: str) -> Client: ...
    def cap(self, service: str) -> int: ...
```

- The factory binds one `ClientDeps` and one validated service section.
- `get(service)` constructs the registered client from `(deps, section)`.
- Unknown services, missing sections, and section/client mismatches are construction errors and
  fail fast.
- `cap(service)` returns the client-declared conservative product safety cap without claiming it
  is the board's exact upstream limit.
- Core may read only `resume_id` and `searches` through `ServiceConfigSection`; all other fields
  remain opaque to generic code. `searches` is the ordered **search pool**: one pipeline declares
  an ordered list of self-contained search entries (query + board filter + optional per-entry
  fetch `window`); the board filter stays a service-specific typed model held inside the client.
  The board-neutral shell is `SearchEntryBase` (query + optional `window: SearchWindow`); every
  concrete entry model extends it and is self-contained (no defaulting/merging between entries —
  each set copied verbatim, as the verified query-pool workflow produces them). A one-entry
  pipeline produces a one-element `searches` tuple.
- Board-specific filters are typed models, not a shared `SearchParams` abstraction or raw mapping.

## CAPTCHA and interaction boundary

The generic contract owns only two presentation-neutral seams:

```text
CaptchaHandler: PNG bytes → Result[answer text, reason]
AuthInteractionProvider: prompt → Result[user response, reason]
```

Each client owns challenge detection, challenge HTTP mechanics, attempt bounds, and replay. A
supported challenge is solved internally before the original operation returns. Exhaustion or an
unsupported challenge returns `CaptchaSolvingError`; there is no public `CaptchaRequired` signal
that asks core to orchestrate board-specific state.

The board-neutral CAPTCHA implementation owns terminal/AI/GUI presentation and handler selection.
It does not own board HTTP, cookies, tokens, selectors, or retry policy.

## Lifecycle and governance

- Construct clients only in the composition root/factory.
- Close every constructed client in `finally`/async-context cleanup before the event loop ends.
- Keep transport and session state inside the client; core never persists it directly.
- A contract change updates this specification, `clients/base.py`, the mock client, the fake
  client, consuming pattern matches, and conformance tests in one implementation change.
- Adding optional model data must be backward-compatible. Removing, renaming, or retyping public
  members is a coordinated breaking change.
- Adding a new error requires an exhaustive core routing decision.

## Conformance requirements

- The fake and mock clients implement every protocol member, including lifecycle.
- Contract tests exercise success and every error-routing category without a live board.
- Client tests use real client code against a mock transport or local fixture server.
- Type checking proves every registered client satisfies `Client`.
- No board name or board response model appears in core, storage, or the generic contract, except
  when there is a dedicated board specific capability in core.

## Related

- [Product specification](jobfucker.md)
- [Architecture](architecture.md)
- [HH.ru client specification](hh-client.md)
