# HH.ru Client Specification

## Purpose and authority

`jobfucker.clients.hh` implements the board-neutral [Client contract](client-contract.md) for
HH.ru. This document specifies the client we build: its components, ownership, workflows, failure
semantics, and acceptance criteria.

The [HH.ru integration wiki](../hh/README.md) is authoritative for observed endpoints, payloads,
response shapes, DOM markers, and upstream behavior. This specification is authoritative for how
the client uses those facts. When HH behavior changes, update the wiki first and then update this
specification before the required client behavior changes.

V1 exposes five capabilities:

- authorize an applicant account;
- search for and fully enrich vacancies;
- list the applicant's resumes;
- apply to a vacancy with a selected resume;
- solve an employer screening test — a board-scoped `HhTestCapable` capability, not part of the
  generic `Client` protocol (see [client-contract.md](client-contract.md#board-scoped-capabilities)).

Negotiation reads may be used internally to reconcile an uncertain application. Sending follow-up
messages, declining negotiations, blacklisting, saved-search management, and resume mutations are
outside V1.

## Architecture

```text
HHClient
├── AuthCoordinator
│   ├── WebsiteLogin
│   ├── OAuthClient
│   └── TokenStore
├── HHTransport
│   ├── request pacing and safe retries
│   ├── endpoint-specific decoding
│   ├── business-error mapping
│   └── challenge classification
├── CaptchaCoordinator
│   └── StandaloneCaptchaProtocol
├── SearchService
│   ├── catalog search
│   ├── resume-similarity search
│   └── vacancy-detail enrichment
├── ResumeService
├── ApplicationService
│   ├── preflight
│   ├── application submission
│   └── uncertain-outcome reconciliation
└── HhTestService
    ├── apply-page blob parsing (HH-Lux-InitialState)
    ├── screening-test multipart submission
    └── test-removed fallback to the plain API apply
```

Only `HHTransport` sends raw HTTP. Coordinators own multi-request workflows and bounded replay.
Services consume validated HH DTOs. `HHClient` maps service results into board-neutral contract
types. The client has no Playwright dependency and never launches a browser.

A suitable package shape is:

```text
clients/hh/
├── client.py          # Client implementation and contract mapping
├── config.py          # HHServiceConfig, HHFilterConfig (contract), HHSearchFilters (wire)
├── transport.py       # HTTP lifecycle, decoding, pacing, retry, errors
├── models.py          # validated HH transport DTOs and internal outcomes
├── auth.py            # login, OAuth, refresh, TokenStore
├── captcha.py         # classifier and standalone CAPTCHA protocol
├── search.py          # both search modes and detail enrichment
├── resumes.py         # owned/published resume listing
├── applications.py    # preflight, submit, reconciliation
└── tests.py           # screening-test fetch + website multipart submit (HhTestCapable)
```

## Configuration and construction

The composition root resolves credential files before constructing the client. `HHClient` receives
credentials through `ClientDeps`; it never opens credential files or reads environment variables.

The board-specific section is validated before construction:

```python
class HHSearchMode(StrEnum):
    CATALOG = "catalog"
    RESUME_SIMILAR = "resume_similar"

class HHSearchEntry(SearchEntryBase):
    filter: HHFilterConfig = Field(default_factory=HHFilterConfig)

class HHServiceConfig(BaseModel):
    resume_id: str = Field(min_length=1)
    search_mode: HHSearchMode = HHSearchMode.CATALOG
    captcha_max_attempts: int = Field(default=4, ge=1, le=10)
    searches: tuple[HHSearchEntry, ...] = Field(min_length=1)   # the search pool
```

### Filter contract (`HHFilterConfig`)

`HHSearchEntry.filter` is a nested, user-facing contract with speaking names and values; it carries
full `Field(title/description/examples)` annotations so `model_json_schema()` (and the generated
`pipeline.schema.json`) drives a labeled UI form (client-contract.md §6.2). Groups (all optional):

| Group              | Fields                                                                                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `query`            | `use_hh_query_language`, `fields_to_search_in`, `exclude_words`                                                                                                                  |
| `ordering`         | `sort_results_by`, `distance_from_point`                                                                                                                                         |
| `work_arrangement` | `work_formats`, `employment_forms`, `shift_rotations`, `hours_per_day`, `part_time_options`, `required_experience`, `required_education`, `temporary_jobs_only`, `legacy_params` |
| `salary`           | `from`, `currency`, `only_with_salary`, `paid`, `measured_in`                                                                                                                    |
| `published`        | `within_days`, `from`, `to`                                                                                                                                                      |
| `location`         | `regions`, `districts`, `metro_stations`, `rectangular_area`                                                                                                                     |
| `targeting`        | `professional_roles`, `industries`, `employers`, `exclude_employers`, `vacancy_labels`, `saved_search`                                                                           |
| `response_extras`  | `include_clusters`, `include_arguments_description`, `include_responses_count`                                                                                                   |

`work_arrangement.legacy_params` (`work_schedules`, `employment_types`) holds the classic HH
vocabulary, kept because its value coverage differs from the modern families
([Vacancy search](../hh/api/search.md#parameter-catalog)); both vocabularies remain functional
upstream, and real users are expected to stay on the modern families.

The client owns all HH spellings. Contract enum values map onto wire values through
module-private `*_WIRE` tables in `clients/hh/config.py`, and `HHFilterConfig.to_search_filters()`
is the one-way bridge to the flat wire model `HHSearchFilters` (unchanged encoder-facing surface,
`extra="forbid"`).
An old flat filter section (yaml or stored snapshot JSON) fails validation; re-run
`jobfucker update` to append a new-style snapshot.

The client encodes every configured wire field onto the request:

- lists serialize as repeated query parameters (`area=1&area=40`); comma-joined values are never sent;
- booleans serialize as lowercase `"true"` and are emitted only when set — absence means false;
- integers, floats, and `YYYY-MM-DD` dates are stringified; `text`, `page`, and `per_page` are always sent;
- defaults are omitted: `order_by` is sent only when it differs from `relevance`; empty tuples and `None` fields are omitted;
- unknown fields are rejected at both layers (`extra="forbid"`) as are locally invalid enum values;
- client paging stays `page >= 0` and `1 <= per_page <= 100`.

Cross-field rules fail at construction — at the contract layer (so `init`, `update`, and
stored-section reconstruction reject them with user-facing names) and again on the wire model as
defense in depth:

- `ordering.sort_results_by="distance"` requires `distance_from_point`, and the point is forbidden without it;
- `published.within_days` cannot coexist with `published.from`/`to`, and `to` requires `from`;
- `location.rectangular_area` is exactly two corners (south-west then north-east) with validated lat/lng ranges.

Two checks need context outside the filter model and fail at search time with
`ConfigurationError`, before any request:

- `query.fields_to_search_in` without a query is inert and rejected;
- `resume_similar` mode rejects `targeting.saved_search`: on `/vacancies` a nonexistent or
  not-owned ID is silently ignored (inert), while resume-scoped search returns `400 bad_argument`,
  so the client fails closed instead of sending a request that cannot succeed.

`search_mode` selects the endpoint. An empty query never changes modes implicitly.
`resume_similar` requires the configured `resume_id` and uses the resume-scoped endpoint.

Each HH profile has an opaque, non-empty `profile_id` supplied by the composition root. Do not put
the raw login in paths. Runtime state lives below `<data_dir>/hh/<profile_id>/` and deleting that
directory resets the HH session.

## HTTP transport

Use one persistent `httpx.AsyncClient` per `HHClient`. The client owns website cookies, OAuth and
Bearer token state, request pacing, retry state, and connection cleanup. `HHClient.aclose()` closes
the transport and is safe to call more than once.

Request rules:

| Concern         | Requirement                                                                                     |
| --------------- | ----------------------------------------------------------------------------------------------- |
| Origins         | Allow only `https://hh.ru` and `https://api.hh.ru`; validate redirect origins before using them |
| User agent      | Use one stable Chrome-like user agent per profile                                               |
| API auth        | Send `Authorization: Bearer <token>` only to authenticated `api.hh.ru` calls                    |
| App header      | Send `X-HH-App-Active: true` on API requests                                                    |
| Redirects       | Disable automatic redirects; OAuth, CAPTCHA, and apply classify `3xx` themselves                |
| Ordinary pacing | Keep at least 0.345 seconds between requests using monotonic time                               |
| Apply pacing    | Wait a random 1–3 seconds immediately before an application POST                                |
| TLS             | Verify certificates; never provide an insecure mode                                             |

Keep only HH-family cookies using the allow-list documented in
[Transport and errors](../hh/api/transport-and-errors.md#cookies-and-xsrf). Website cookies are
required for login, OAuth authorize, and CAPTCHA. Ordinary API operations remain Bearer-only.

Transport decoding follows an endpoint contract rather than a global `2xx == success` rule:

```text
network result
  → challenge classification
  → endpoint-specific success decoder
  → business-error mapping
  → broad status/protocol fallback
```

External JSON is untrusted. Parse it into strict-enough Pydantic DTOs that require fields used by
the workflow, allow documented optional fields, and ignore unknown additive fields. Preserve safe
diagnostic context: operation, status, request ID, HH error type/value, and a bounded body marker.
Use `X-Request-ID` when the JSON body has no `request_id`.

Retry only safe operations:

- retry connection/TLS failures and selected upstream `5xx` responses for GET requests with
  bounded exponential backoff and jitter;
- never retry unchanged validation or business errors;
- refresh an expired token once and replay the request once;
- solve a supported challenge within its own bound and replay once;
- never blindly replay an application after request bytes may have been sent.

See [Transport and errors](../hh/api/transport-and-errors.md) for the envelope and mapping table.

## Authentication and token lifecycle

Authorization is HTTP-only:

```text
load token state
  ├─ unexpired access token → GET /me
  │    ├─ authorized → done
  │    ├─ supported challenge → solve, replay once
  │    └─ auth rejection → full authorization
  ├─ expired access token + refresh token → refresh once → GET /me
  └─ no usable token
       → GET OAuth-context /account/login for cookies and _xsrf
       → multipart POST /account/login
            └─ embedded CAPTCHA → fresh key + PNG + solver + bounded login-form replay
       → GET /oauth/authorize without following redirect
       → validate custom-scheme code and state
       → POST /oauth/token
       → GET /me
       → persist state atomically
```

Requirements:

- Use the exact mobile OAuth client and redirect URI from
  [Authentication](../hh/authentication.md#oauth-client) without copying them into logs.
- Pass a cryptographically random OAuth `state` and validate it when returned.
- Treat the `hhtoken` cookie and successful authorize-code redirect as the website-login proof;
  do not infer success from `hhrole` or response HTML alone.
- Require `redirect_uri` to match exactly in authorize and token exchange.
- Validate token type, non-empty access/refresh tokens, `expires_in`, and the observed `USER`
  prefix as corruption/protocol checks rather than assertions.
- Store an absolute expiry timestamp with a small request-time skew.
- Refresh only after local token state says the access token is expired. A pre-expiry
  `invalid_grant` must not trigger a loop.
- Replace access and refresh tokens atomically after refresh; never reuse the prior refresh token.
- Route defensive HTTP 401 and non-challenge 403 responses through the auth coordinator. Do not
  refresh a still-valid token; perform full authorization instead.
- Ask for a one-time code only through `AuthInteractionProvider`; never read stdin in the client.

Persist token JSON and cookies atomically with user-only permissions where the platform supports
them. Never log credentials, tokens, cookies, authorization codes, OAuth state, or raw auth bodies.

If login returns `hhcaptcha.isBot=true` and a stable `captchaState`, solve it through the shared
PNG handler and re-submit the original credential form with `captchaKey`, `captchaText`, and
`captchaState`. Never submit this variant to `/account/captcha`. Bound attempts, issue a fresh
image key after every rejected answer, reject state drift, and inspect `loginError` separately
after CAPTCHA acceptance. See [Login CAPTCHA](../hh/captcha-login.md).

## CAPTCHA handling

Challenge classification runs before ordinary status mapping on every response. The internal
challenge model records kind, context, optional URL/state, status, request ID, and safe markers.
It distinguishes:

- standalone HH text CAPTCHA;
- embedded login CAPTCHA;
- reCAPTCHA;
- unknown challenge/protocol drift.

Both observed HH text-CAPTCHA contexts are automatically solvable. The standalone variant uses the
persistent website cookie jar and the protocol in
[CAPTCHA](../hh/captcha.md):

```text
GET captcha_url
  → obtain state and _xsrf
POST /captcha?lang=RU
  → obtain a fresh image key
GET /captcha/picture?key=...
  → validate image type and size
CaptchaHandler(PNG)
  → answer text
POST /account/captcha with answer, key, state, and XSRF headers
  → validate exact success redirect
replay blocked request once
```

The embedded variant shares the key/image/solver mechanics but submits its answer with the original
credentials on the OAuth-context login form, as specified in
[Login CAPTCHA](../hh/captcha-login.md#submission-contract-the-difference-from-the-standalone).

Policy:

- bound answer attempts per challenge using `captcha_max_attempts` and permit at most one
  challenge recovery/replay per operation;
- obtain a new image key after every wrong answer;
- accept success only when the same-origin redirect contains the documented
  `hhtmFrom=account_captcha` marker and no CAPTCHA error marker;
- never copy website cookies into the API request as part of solving—the unlock is
  account/Bearer-bound;
- return `CaptchaSolvingError(recovery_url=captcha_url)` when attempts are exhausted;
- return a typed failure without guessing for reCAPTCHA and unknown challenges.

The generic `CaptchaHandler` owns only `PNG bytes → Result[str, str]`. It does not know HTTP,
cookies, retries, or HH selectors.

## Vacancy search and enrichment

The selected `search_mode` routes explicitly:

| Mode             | Endpoint                                     | Seed                                                  |
| ---------------- | -------------------------------------------- | ----------------------------------------------------- |
| `catalog`        | `GET /vacancies`                             | generic query string plus catalog-supported filters   |
| `resume_similar` | `GET /resumes/{resume_id}/similar_vacancies` | configured resume plus filters verified for this mode |

Do not send `resume` or `resume_id` to `/vacancies`; HH silently ignores them. Do not assume a
filter verified for catalog search is supported by resume similarity. The mode-specific support
matrix and validation constraints live in [Vacancy search](../hh/api/search.md).

Search success requires a 2xx response, no `errors`, and valid `items`, `found`, `page`, `pages`,
and `per_page` fields. HTTP 200 with an error envelope is a failure. The contract's
`search_vacancies(search_index, offset=..., limit=..., page_size=...)` is slice-level: the client (via
the shared driver `jobfucker.clients.paging`) walks exactly the native pages overlapping
`[offset, offset + limit)`, passing each page's zero-based `page` and `per_page=page_size`
directly to HH, and stops at a short page (listing exhausted) or at the first failing page
(partial `SearchSlice` with `failure`; `Err` stays reserved for pre-scan failures — auth
preflight, invalid arguments, config). Return an empty `items` page as a successful empty page.

HH limits searchable results to 2000 items (`maxSearchResult=2000`): the server pre-caps `pages`
and an offset beyond the cap returns `400 bad_argument` ("you can't look up more than 2000 items").
The client declares this cap as `ServiceInfo(max_search_items=2000)` so the generic fetch stage
validates its window up front; the client still maps the upstream 400 to `BadRequestError` as a
defense in depth. Keep `per_page` within the client invariant 1–100 (the product CLI additionally
restricts `--page-size` to {20, 50, 100} for sanity; the client does not enforce the product
restriction).

Search items contain snippets, not the full job description. Enrich **only the items whose global
listing position lies inside the requested slice** through `GET /vacancies/{id}` before returning
board-neutral `Vacancy`s — items outside `[offset, offset + limit)` are never detail-fetched
(`--take 202` costs 202 detail GETs, not 300). Bound enrichment concurrency and
use the shared pacing gate; a large detail burst can trigger CAPTCHA.

Map detail data as follows:

- `description` HTML → normalized plaintext using `html.parser.HTMLParser` with
  `convert_charrefs=True` and newlines for block elements;
- `key_skills[].name` → `tuple[str, ...]`;
- structured salary → `Salary | None` without string flattening;
- missing employer → `company=None`;
- `alternate_url` → public vacancy URL.

Do not use regex tag stripping and do not scrape the website vacancy page as a fallback.

## Resumes

`get_resumes()` pages through `GET /resumes/mine` using the server envelope from
[Response models](../hh/api/response-models.md#resume-list-item). It returns `Ok([])` when the
account has no resumes and maps only fields required by `ResumeInfo`.

The client retains internal ownership and publication fields for application validation.
`can_publish_or_update` is nullable and must not be decoded as a strict boolean.

Before applying, the configured `resume_id` must identify a resume owned by the current account and
its `status.id` must be `published`. A missing, foreign, or unpublished configured resume is a
`ConfigurationError` and must stop the pipeline rather than fail each vacancy independently.

## Applications

Before each application, fetch current vacancy detail and classify deterministic skips:

| Condition                                     | Result                                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------ |
| archived or closed for applicants             | `ApplySkipped(ApplySkip(reason="vacancy_unavailable", text=...))`                    |
| external `response_url` or `adv_response_url` | `ApplySkipped(ApplySkip(reason="external_application", text=..., redirect_url=...))` |
| product exclusion/manual skip                 | handled by the product layer before calling the client                               |

Do not use `relations` as an authoritative already-applied check.

Submit form-encoded `resume_id`, `vacancy_id`, and optional `message` to `POST /negotiations`
after the randomized delay. Classify outcomes exactly:

| HH signal                           | Contract outcome                                                                         |
| ----------------------------------- | ---------------------------------------------------------------------------------------- |
| `201` with zero body bytes          | `Ok(ApplySucceeded())`                                                                   |
| `already_applied`                   | `Ok(ApplySkipped(ApplySkip(reason="already_applied", text=...)))`                        |
| `test_required`                     | `Ok(ApplySkipped(ApplySkip(reason="test_required", text=...)))`                          |
| external/redirect response          | `Ok(ApplySkipped(ApplySkip(reason="external_application", text=..., redirect_url=...)))` |
| deterministic per-vacancy rejection | `Ok(ApplyFailed(ApplyError(text=..., code=...)))`                                        |
| `resume_not_found`                  | `Err(ConfigurationError)`                                                                |
| `limit_exceeded`                    | `Err(LimitExceededError)`                                                                |
| unsolved challenge                  | `Err(CaptchaSolvingError)`                                                               |
| authorization failure               | `Err(AuthError)`                                                                         |
| connection loss after send          | reconcile, otherwise `Err(UnknownApplyOutcomeError)`                                     |

For an uncertain POST, query active negotiations and match `vacancy.id`. If the application is
present, return `ApplySucceeded()`; if absence cannot be established reliably, return
`UnknownApplyOutcomeError`. Never auto-replay while the outcome is unknown.

### Employer screening tests

A test-bearing vacancy is not a preflight skip: it is routed through the board-scoped
`HhTestCapable` capability that `HHClient` implements on top of the generic contract.

- `get_vacancy_test(vacancy_id)` opens the website apply page and parses the escaped
  `HH-Lux-InitialState` blob into a validated test (name, description, tasks, options); it returns
  `Ok(None)` when the page carries no test. All blob JSON is validated with pydantic models at this
  boundary — shape, task kind matrix, options, and ids per [docs/hh/tests.md](../hh/tests.md).
- `apply_to_vacancy_with_test(...)` re-fetches the blob (a stale test is never trusted), rebuilds
  the multipart `POST /applicant/vacancy_response/popup` form with one answer field per task plus
  the page's `xsrf`/`startTime`, and classifies the response. The fresh blob wins: `alreadyApplied`
  → `ApplySkipped(reason="already_applied")`; a submitted task id absent from the fresh test →
  `ApplyFailed(ApplyError(code="test_changed"))`; a fresh blob with no test falls back **once** to
  the plain API `apply_to_vacancy`; documented website errors map per `_classify_web_error`
  (`already_applied`, `test_required`, `resume_incomplete`, else `web_apply_error`).
- Unobserved checkbox (`multiple=true`) tasks are submitted as a single choice and logged at warn
  level until real-world evidence lands ([docs/hh/tests.md](../hh/tests.md) §10).

Solver selection, prompt rendering, and the file answers path are core-side (`jobfucker/hh_tests/`),
not client concerns.

## Error and reporting policy

Map HH transport and business outcomes into the generic error types defined by the client
contract. Preserve machine-readable routing distinctions; do not make callers parse messages.

`Reporter` events may include operation, safe progress, request ID, vacancy title/URL, attempt
number, and sanitized HH type/value. They must not contain secrets or full sensitive payloads.

Programmer/configuration invariants fail fast at construction. Expected network, auth, upstream,
challenge, and application outcomes return `Result` errors or `ApplyResult` as specified above.

## Acceptance tests

Use `httpx.MockTransport` or a local fixture server; tests must exercise real client code rather
than patching its internals.

| Area            | Required scenarios                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Construction    | rejects a non-HH config section; receives resolved credentials; closes transport idempotently                                                                                                                                                                                                                                                                                                                              |
| Auth            | restored token; expired-token refresh; raw credential login and OAuth exchange; state/redirect validation; pre-expiry refresh rejection; 401/403 recovery                                                                                                                                                                                                                                                                  |
| Auth challenge  | embedded login CAPTCHA success, fresh-key retry, exhaustion, state drift, and post-solve credential failure; never standalone submission                                                                                                                                                                                                                                                                                   |
| Transport       | pacing; repeated query params; redirects disabled; request-ID fallback; additive JSON fields; malformed/non-JSON protocol failure                                                                                                                                                                                                                                                                                          |
| Search          | both explicit modes with the full filter surface; contract↔wire mapping parity; HTTP 200 error body; exact native page and empty result; cross-field filter combinations fail at construction; `resume_similar` rejects `saved_search`; `fields_to_search_in` without a query fails closed                                                                                                                                 |
| Enrichment      | full detail fetched; HTML blocks/entities/Cyrillic preserved; nullable employer/salary; key skills remain structured                                                                                                                                                                                                                                                                                                       |
| Resumes         | multiple pages; empty list; nullable fields; published/owned selection; invalid configured resume stops                                                                                                                                                                                                                                                                                                                    |
| CAPTCHA         | key/image flow; wrong answer gets new key; validated success redirect; bounded replay; unknown/reCAPTCHA failure                                                                                                                                                                                                                                                                                                           |
| Apply           | exact 201-empty success; already applied; test required; external flow; resume missing; limit stop; CAPTCHA replay                                                                                                                                                                                                                                                                                                         |
| Screening tests | apply-page blob parse vs a fixture mirroring the captured `HH-Lux-InitialState` blob (escaped template, stringly booleans, numeric/string ids); website multipart submission (one field per task); `already_applied`/`response_impossible`; `test_changed` on a task set that drifted either way; test-removed blob falls back to the plain apply; documented website error mapping (`test-required`, `resume-incomplete`) |
| Uncertain apply | negotiation reconciliation finds application; unresolved outcome is never replayed                                                                                                                                                                                                                                                                                                                                         |

Live HH smoke tests are opt-in, rate-limited, non-destructive by default, and use explicit allowlists
for any real application.

## Non-goals

- Browser automation or cookie extraction from a browser.
- Automated embedded-login CAPTCHA solving until its HTTP contract is verified.
- External employer application forms (in-site screening tests are in scope, see above).
- Public negotiation/message methods.
- Follow-up messages, decline, chat hiding, saved-search mutations, blacklists, and resume editing.
- Hard-coding an assumed HH daily application cap or CAPTCHA trigger threshold.
