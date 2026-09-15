# Client Blueprint

This page explains the implementation boundaries implied by observed HH behavior. The normative
client requirements live in the [HH.ru client specification](../specs/hh-client.md), and the
public board-neutral interface lives in the [jobfucker client contract](../specs/client-contract.md).

## Components

```text
HHClient
├── AuthCoordinator
│   ├── WebsiteLogin
│   ├── OAuthClient
│   └── TokenStore
├── HHTransport
│   ├── request pacing/retry
│   ├── response decoder
│   ├── challenge classifier
│   └── website cookie/XSRF jar
├── CaptchaCoordinator
│   ├── HHTextCaptchaProtocol
│   └── generic PNG solver/human fallback
├── SearchService
│   ├── catalog search
│   ├── resume-scoped search
│   └── vacancy enrichment
├── ResumeService
└── NegotiationService
    ├── apply
    ├── list/messages
    └── future mutations
```

Only transport owns raw HTTP. Endpoint services consume validated DTOs and typed transport errors.
Authentication and CAPTCHA coordinate retries but do not leak browser/session mechanics into the
board-neutral client interface.

## External boundaries

Validate at entry:

- credentials and profile IDs;
- persisted cookie/token state;
- OAuth and API JSON;
- search/filter configuration;
- URL origins before following/surfacing redirects;
- image content type and size before CAPTCHA decoding.

Use distinct DTOs for API errors, OAuth errors, search envelopes, vacancy detail, resume list,
negotiations, and messages. Do not decode all HH responses into one loose dictionary.

## Authorization workflow

1. Load profile cookie/token state.
2. If access token is unexpired, validate with `/me` when required by startup policy.
3. If expired and a refresh token exists, refresh once and atomically replace both tokens.
4. Otherwise perform browserless login, OAuth authorize, and token exchange.
5. Persist secrets only after `/me` succeeds.
6. If any step reports a recognized challenge, solve it through the same CAPTCHA coordinator.

Full protocol: [Authentication](authentication.md).

## Fetch workflow

```text
validate query + concrete HH filters
    │
choose catalog or resume-scoped endpoint explicitly
    │
page through the server-reported `pages`
    │
for each search item, fetch /vacancies/{id} with bounded concurrency/pacing
    │
parse HTML description and structured fields
    │
emit complete board-neutral vacancies
```

Do not use search snippets as full descriptions. Limit or serialize enrichment requests because
read bursts can trigger CAPTCHA. Keep partial-progress semantics explicit if enrichment fails.

## Apply workflow

1. Validate that the configured resume is owned and published.
2. Load current vacancy detail.
3. Apply deterministic skip rules for archive/closure, tests, and external response URLs.
4. Send the form-encoded POST after a randomized delay.
5. Classify `201` empty, business errors, challenge, redirect, transport uncertainty, and limit
   stop separately.
6. Persist an outcome only after classification. For an unknown POST outcome, reconcile via
   negotiations before any replay.

Full protocol: [Applications and negotiations](applications-and-negotiations.md).

## Error types worth preserving

At minimum, keep distinct outcomes for:

```text
transport_unavailable
protocol_drift
oauth_rejected(error_code)
authorization_required
captcha_required / captcha_failed
invalid_search_parameter
not_found
already_applied
test_required
resume_not_found
application_limit_reached
external_application
unknown_apply_outcome
upstream_failure
```

Every error should carry safe context: operation, status, request ID, HH machine type/value, and a
sanitized message. Never include credentials, tokens, cookies, authorization codes, CAPTCHA
answers, or full sensitive response bodies in logs.

## Retry policy

| Failure/operation | Retry rule |
| --- | --- |
| TLS EOF/connect timeout on GET | Bounded backoff with jitter |
| 502/selected 5xx on GET | Bounded retry |
| 403 challenge | Solve then retry original request within challenge bound |
| Expired token | Refresh once, then retry once |
| Search validation/business 4xx | Never retry unchanged |
| Apply connection loss after send | Do not replay until reconciled |
| `already_applied`, `test_required`, `resume_not_found`, `limit_exceeded` | Never retry unchanged |

Retry decisions must know the operation's idempotency and whether request bytes could have reached
HH.

## Data ownership

- Profile runtime state owns tokens, cookies, generated transport identity, and expiry timestamps.
- The application database owns normalized vacancies, pipeline state, and durable outcomes.
- Raw HH JSON is an external boundary object, not a domain model.
- Resume IDs and HH-specific search filters remain service-specific; do not erase their semantics
  into generic key/value bags.

## Minimum integration tests

Use a contract-capable local HTTP server to prove:

- HTTP 200 search body with `errors` is rejected;
- repeated-list query serialization and local cross-field validation;
- search paging stops at reported pages and handles empty pages;
- additive/unknown JSON fields do not break decoding;
- resume and negotiation live shapes decode without stale fields;
- `201` empty apply is success;
- `already_applied` is an idempotent outcome despite empty `relations`;
- standalone CAPTCHA key/image/wrong-answer/correct-answer/replay flow is bounded;
- embedded login CAPTCHA is classified separately, never posted to the standalone submit route,
  and is solved by re-submitting the login form with `captchaKey`/`captchaText`/`captchaState`
  ([Login CAPTCHA](captcha-login.md));
- pre-expiry refresh is not attempted;
- unknown apply outcomes are not auto-replayed;
- HTML-to-text preserves list boundaries, entities, and Cyrillic.

Live smoke tests should be opt-in, rate-limited, non-destructive by default, and use an explicit
allowlist for real applications.

## Change canaries

Revalidate these small signals when HH behavior appears to drift:

| Area | Canary |
| --- | --- |
| API auth | Bearer `/me` shape and status |
| OAuth | Authorize returns 302 custom-scheme code with exact redirect URI |
| Login | Direct page has role-card prefix or OAuth page has legacy username marker |
| Token refresh | Pre-expiry still rejects; expired-token rotation observed before relying on it |
| Search | One valid request and one invalid-enum 200/error-body case |
| Paging | `per_page=100` and reported cap behavior |
| Resume shape | `/resumes/mine` remains paginated and exposes published status |
| Vacancy detail | `description` HTML and `key_skills` list |
| Apply | Only with explicit allowlist; expect 201 empty or typed business error |
| CAPTCHA | Key issuance, image content type, submit status markers |
| Negotiations | List/message field keys and state IDs |

When a canary changes, update the canonical behavior page and its tests. Do not add a dated session
log to the wiki.

## Cross-reference map

| Component | Behavioral source |
| --- | --- |
| `AuthCoordinator`, `OAuthClient` | [Authentication](authentication.md) |
| Request decoder/retry/challenge classifier | [Transport and errors](api/transport-and-errors.md) |
| Search filter schema | [Vacancy search](api/search.md) |
| API DTOs | [Response models](api/response-models.md) |
| Browser/human recovery selectors | [Website behavior and DOM](website.md) |
| CAPTCHA protocol | [CAPTCHA](captcha.md) |
| Apply and chat service | [Applications and negotiations](applications-and-negotiations.md) |
