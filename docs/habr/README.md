# Habr Career Integration Wiki

Technical reference for building and maintaining a `career.habr.com` client for jobfucker. Early
stage: this wiki starts as a high-level playbook and is deepened one topic per research session per
[the research plan](research-plan.md). It describes Habr Career as it behaves, not how it was
researched.

> Freshness: surface discovery performed against live `career.habr.com` on 2026-09-18 with the
> authenticated `habr-exp` account. Habr Career is an external system and can change without notice.
> Revalidate the canaries in each page when behavior drifts.

## Start here

| Need | Read |
| --- | --- |
| Understand the plan and method for this wiki | [Research plan](research-plan.md) |
| Method every research session follows | [Research playbook](research-playbook.md) |
| Understand which Habr Career surface owns which operation | [Platform map](platform-map.md) |
| Search, list, and page vacancies | [Search and listings](api/search.md) |
| Decode listing/detail/resume payloads | [Response models](api/response-models.md) |
| Log in through Habr Account SSO | [Authentication and session](authentication.md) |
| Understand transport, statuses, and error mapping | [Transport and errors](api/transport-and-errors.md) |
| Understand the login challenge | [CAPTCHA](captcha.md) |
| See what is not yet established | [Known unknowns](known-unknowns.md) |
| Reuse the HH wiki as the target shape | [HH.ru wiki](../hh/README.md) |

## Core model (discovered, not yet verified)

```text
credentials
    │
    ▼
Habr Account SSO  account.habr.com login form + Yandex SmartCaptcha
    │  OAuth authorize: client_id=career-<uuid>, redirect_uri=/users/auth/tmid/callback_oauth
    ▼
career.habr.com session cookie  (Rails CSRF `authenticity_token`)
    │
    ▼
career.habr.com website (server-rendered HTML)
    search / vacancy detail / profile / responses

Listings        ──► GET /api/frontend/vacancies?<filters>   (JSON {list, meta})
Vacancy detail  ──► /vacancies/<id> inline "vacancy" JSON (description HTML) + JobPosting JSON-LD
Same-origin XHR ──► /api/frontend_v1/...  (JSON: identity, notifications, subscriptions, suggestions)
Owned resume    ──► GET /profile (public /<alias>)
```

The website is the primary surface; the **listing data is JSON** (`/api/frontend/vacancies`) and the
vacancy detail page embeds structured JSON, so scraping is not needed. Login is established as fully
browserless (see [Authentication](authentication.md#browserless-login-verified)); whether a pure-HTTP
path fully covers apply and the rest of the challenge handling is not established.

## Facts observed so far

- Habr Career is a **Rails app**. Mutations require the form field `authenticity_token`
  (`<meta name="csrf-token">` also present).
- Login is **Habr Account SSO**, reached at `/users/auth/tmid`; there is no local career password
  form on the entry URLs observed.
- A fresh login goes through `account.habr.com` and is gated by a **Yandex SmartCaptcha that is
  enforced on every fresh credential login** (a token-less POST returns
  `errors.smart-token`). It is risk-based: a trusted browser passes the checkbox, while a detectable
  fingerprint (e.g. the `HeadlessChrome` UA) escalates to an **image (distorted-text) challenge**
  protected by a proof-of-work. Both the escalation contract and a **fully browserless login** (pure
  HTTP: vision OCR + `pow` → `spravka` → login POST → `rurl`) were verified. An existing Habr
  Account session skips the login form and captcha. See [CAPTCHA](captcha.md) and
  [Authentication](authentication.md).
- A logged-in session sets the Rails career session `_career_session`, the persistent
  `remember_user_token`, and `.habr.com` `s<hex>` SSO cookies; traffic passes through **Qrator**
  (`qrator_msid2`). Cookie inventory is in [Authentication](authentication.md#session-cookies).
- **Vacancy listings are JSON**: `GET /api/frontend/vacancies?<filters>` (note `/api/frontend/`, not
  `/api/frontend_v1/`) returns `{list, meta}` with `meta.totalResults`. `/vacancies` is the same data
  server-rendered. See [Search and listings](api/search.md).
- Listing filters: `q`, `type=all|suitable`, `qid`, `remote`, `with_salary`, `salary`+`currency`,
  `skills[]`, `city_id`, `employment_type`, `sort=relevance|date|salary_desc|salary_asc`, `page`,
  `per_page`. `type=suitable` is resume-scoped when authenticated and **ignored when anonymous**.
- Listing caps: effective page size is **50** (even when `per_page` is larger); positions at/after
  offset ~1000 return an empty list; a page beyond `meta.totalPages` is `404 {"error":"Not found"}`.
  Declare `max_search_items = 1000`.
- **Vacancy detail** (`/vacancies/<id>`) embeds the listing item shape plus the full `description`
  HTML under `"vacancy":{...}`; a `schema.org/JobPosting` `ld+json` block is also present. Prefer the
  inline JSON. No description/snippet is in the listing.
- The authenticated identity endpoint is `GET /api/frontend_v1/users/me`, returning
  `{user: {alias, fullName, email, ...}}`.
- **Owned resume** is the profile page `GET /profile` (public `/<alias>`); no owned-resume JSON
  endpoint was found (`/api/frontend_v1/resumes` is the public specialist directory). The observable
  resume identifier is the account alias.
- **RSS is not a search surface**: `/vacancies/rss` returns a fixed latest 50 and ignores
  `page`/`per_page`/`q`; use it only as a canary.
- Vacancy identifiers are numeric, e.g. `/vacancies/1000167594`.
- **Anonymous reads work**: listing (JSON), detail (with inline JSON / JSON-LD), and RSS all return
  content without a session. Protected pages (`/responses`) answer `302 → /users/auth_required`. The
  anonymous identity call returns `200 {}` — auth is detected from the payload, never the status.
  An anonymous request still sets `_career_session`, so cookie presence is not an auth signal.
- **Session persistence** is `remember_user_token`: dropping `_career_session` but keeping it
  re-issues a career session; the `.habr.com` SSO cookies alone do **not** authenticate career.
  `_career_session` is a cookie-store session, so logout does not invalidate a captured cookie value.
- **CSRF** is required on mutations (`422` without a token); both the `authenticity_token` form field
  and the `X-CSRF-Token` header are accepted.
- `/api/frontend_v1/` errors observed are `{"error":"Not found"}` (404) and
  `{"status":422,"error":"Unprocessable Entity"}` (CSRF/validation), returned as JSON; the same web
  URL reacts to JSON accept headers. The structured `{"httpCode":...,"errorCode":...}` envelope seen
  during surface discovery was not reproduced and remains unverified. See
  [Transport and errors](api/transport-and-errors.md).

## Scope and authority

This wiki is authoritative for observed Habr Career behavior and the integration constraints derived
from it. Product behavior and board-neutral interfaces remain authoritative in the parent jobfucker
specifications: [client contract](../specs/client-contract.md),
[architecture](../specs/architecture.md). The implementation spec for the future
`jobfucker.clients.habr` package will be `docs/specs/habr-client.md` (Session 4).

Raw HARs, cookies, tokens, CSRF values, and challenge captures are deliberately not dependencies
and live only in `/tmp`. The wiki contains the durable, sanitized contracts.
