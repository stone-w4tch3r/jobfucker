# Transport and Errors

Habr Career exposes one cookie-authenticated origin (`career.habr.com`) behind the Qrator WAF: HTML
for the website and JSON for `/api/frontend_v1/`. This page records the transport contract observed
over pure HTTP and how it maps onto the [client contract](../specs/client-contract.md) error union.

> Freshness: probes run 2026-09-18 over pure HTTP with an exported authenticated cookie jar and a
> clean anonymous jar, against live `career.habr.com`. Low volume (single requests, one 10-request
> burst); no challenge or throttle was induced.

## Request profile

- One persistent HTTP client per profile owns the cookie jar. Persist career cookies
  (`_career_session`, `remember_user_token`) plus the `.habr.com` SSO cookies; drop analytics
  ([Authentication](../authentication.md#session-cookies)).
- Recommended headers:

  ```http
  User-Agent: Mozilla/5.0 (...) Chrome/... Safari/537.36
  Accept-Language: ru-RU,ru;q=0.9,en;q=0.8
  Accept: application/json, text/javascript, */*; q=0.01   # XHR/JSON calls
  X-Requested-With: XMLHttpRequest                          # XHR/JSON calls
  ```

- The user-agent did **not** change Qrator or content behavior: Chrome, `python-urllib/3.12`, and
  `curl/8.0` all received `200` on `/vacancies` and `/api/frontend_v1/users/me`. Keep a desktop
  Chrome UA anyway for coherence with the captcha-gated login.
- Follow redirects by default. The authorization path is a redirect chain; an anonymous protected
  page is a `302` to `/users/auth_required` (see below), not a `401`.
- No `x-request-id`-style correlation header was observed.

## Content negotiation

- `/api/frontend_v1/*` always answers JSON.
- Website routes answer JSON error bodies when the request asks for JSON
  (`Accept: application/json` / `X-Requested-With: XMLHttpRequest`); the same URL with a plain HTML
  accept returns the HTML error page. `GET /vacancies/<unknown-id>` → `{"error":"Not found"}` under
  JSON accept, HTML `404` page otherwise.
- Ordinary HTML pages carry the Rails CSRF pair `<meta name="csrf-token">` and
  `<meta name="csrf-param" content="authenticity_token">`, and every Rails form embeds a hidden
  `authenticity_token`. An anonymous first request still sets `_career_session` (cookie-store
  session for flash/CSRF), so **cookie presence is not an authentication signal**.

## Response headers

| Header | Observed |
| --- | --- |
| `Server` | `QRATOR` on every response |
| `Vary` | `Origin` (API responses) |
| `Cache-Control` | `max-age=0, private, must-revalidate` |
| `Content-Type` | `text/html` for the site, `application/json` for the API, `application/xml` for RSS |
| Rate-limit headers | **none observed** (`X-RateLimit-*`, `Retry-After` absent) |

## Status and body matrix (verified)

| Request | HTTP | Body |
| --- | --- | --- |
| `GET /api/frontend_v1/users/me` (authenticated) | 200 | full `{user, meta, userCompanies}` |
| `GET /api/frontend_v1/users/me` (anonymous) | 200 | `{}` |
| `GET /api/frontend_v1/<unknown>` (JSON accept) | 404 | `{"error":"Not found"}` |
| `GET /vacancies/<unknown-id>` (JSON accept) | 404 | `{"error":"Not found"}` |
| `GET /<unknown-non-api>` (HTML accept) | 404 | Rails HTML error page |
| `POST`/`DELETE` on `/api/frontend_v1/*` without CSRF | 422 | `{"status":422,"error":"Unprocessable Entity"}` |
| `GET /responses` (anonymous) | 302 → 200 | `/users/auth_required` HTML |
| `GET /vacancies` (anonymous or authenticated) | 200 | HTML listing; `.vacancy-card` items |
| `GET /vacancies/rss?page=1&per_page=25` | 200 | RSS 2.0 (50 `<item>` on page 1) |
| `GET /vacancies/<id>` (anonymous or authenticated) | 200 | HTML detail with `JobPosting` `ld+json` |

Notes:

- The CSRF check runs before resource resolution: `POST /api/frontend_v1/<unknown>` also answers
  `422`, not `404`.
- The `{"httpCode":404,"errorCode":"NOT_FOUND",...}` envelope described during surface discovery was
  **not reproduced** in this pass; the observed error bodies are `{"error":"..."}` and
  `{"status":N,"error":"..."}`. Treat the structured envelope as unverified.

## Error mapping (proposed, for the future client)

| Signal | Client error | Retry |
| --- | --- | --- |
| Connect/TLS/timeout, DNS, unreadable stream | `TransportError(status=None)` | Bounded retry, safe operations only |
| `5xx` | `TransportError(status)` | Bounded retry, safe operations only |
| `429` (not observed) | `TransportError(status=429)` | Honor `Retry-After` if ever present, else backoff |
| `302` to `/users/auth_required`; `{}` from an identity call; login `errors.smart-token` | `AuthError` | Re-authorize; do not loop |
| `404` JSON `{"error":"Not found"}` or HTML error page | `NotFoundError` | No retry |
| `422` CSRF `{"status":422,...}`; other `400`/`422` validation | `BadRequestError` | No retry; fix request/CSRF |
| JSON promised, unparseable or wrong content type | `ProtocolError(status)` | No retry; capture body |
| Challenge markers (see [CAPTCHA](../captcha.md#detection-and-fail-fast)) | `CaptchaSolvingError(recovery_url)` | Client-owned bounded solve |
| Unknown challenge-like `4xx`/HTML (e.g. possible Qrator interstitial) | `ProtocolError` | No retry; hand off |

The CSRF `422` is a programming/config error for the client (missing or stale
`authenticity_token`), not a user-facing failure. On a `422`, refresh the token from a page and
retry the single mutation once; do not loop.

## CSRF

- Required on every state-changing request. Missing token → `422`
  `{"status":422,"error":"Unprocessable Entity"}`.
- Both carriers are accepted (verified via `POST /users/sign_out`, `_method=delete`):
  - hidden form field `authenticity_token` → `302`;
  - header `X-CSRF-Token` (no form field) → `302`.
- Obtain the token from any authenticated HTML page (`meta[name=csrf-token]` or a form's
  `authenticity_token`). It is bound to the session, not to a single form.

## Rate limits and Qrator

- No rate-limit headers and no `429`/`403` in a 10-request sequence (page 1..10, ~0.3 s apart,
  authenticated), and no UA-based block (Chrome / python-urllib / curl). Thresholds are unknown —
  keep HH-like pacing (≥0.3 s between requests, larger gaps before mutations).
- `Server: QRATOR` confirms a WAF in front of everything. A `qrator_msid2` cookie appears in a real
  browser session; no interstitial or cookie-refresh challenge was observed. Any
  interstitial/challenge must be handled per the [CAPTCHA rule](../research-playbook.md#captcha-handling-rule).
- No captcha was observed on `career.habr.com` (search/detail/identity) at this volume; the only
  observed SmartCaptcha is on the Habr Account login step ([CAPTCHA](../captcha.md)).

## Change canaries

| Area | Canary |
| --- | --- |
| WAF | Responses still carry `Server: QRATOR` |
| Identity (anonymous) | `GET /api/frontend_v1/users/me` without a session returns `200 {}` |
| API 404 | `GET /api/frontend_v1/<unknown>` (JSON accept) returns `404 {"error":"Not found"}` |
| CSRF | A `POST` without a token returns `422 {"status":422,...}` |
| CSRF carriers | `X-CSRF-Token` header and `authenticity_token` form field both accepted |
| Anonymous reads | `/vacancies`, `/vacancies/<id>`, `/vacancies/rss` return `200` with content |
| Anonymous protected | `GET /responses` redirects to `/users/auth_required` |
| Rate limits | No `X-RateLimit-*`/`Retry-After` on ordinary reads |

## Not established yet

- `5xx` body shapes and whether `Retry-After` ever appears; whether a `429` exists at all.
- The Qrator interstitial / block page shape and its trigger.
- Whether the `{"httpCode":...,"errorCode":...}` envelope exists on any route.
- Rate-limit and captcha behavior at real (fetch/apply) volume.
