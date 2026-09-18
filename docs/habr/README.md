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
| Log in through Habr Account SSO | [Authentication and session](authentication.md) |
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

Same-origin XHR ──► /api/frontend_v1/...  (JSON: identity, notifications, subscriptions)
Vacancy detail  ──► schema.org JobPosting JSON-LD (description HTML)
Listings        ──► HTML cards, or RSS /vacancies/rss?page=&per_page=
```

The website is the primary surface and is **server-rendered HTML**; the JSON API
(`/api/frontend_v1/`) covers account widgets but exposed no vacancy search or vacancy detail
endpoint during discovery. Whether a browserless pure-HTTP path fully covers login, apply, and
challenge handling is not established.

## Facts observed so far

- Habr Career is a **Rails app**. Mutations require the form field `authenticity_token`
  (`<meta name="csrf-token">` also present).
- Login is **Habr Account SSO**, reached at `/users/auth/tmid`; there is no local career password
  form on the entry URLs observed.
- A fresh login goes through `account.habr.com` and is gated by a **Yandex SmartCaptcha** checkbox;
  it passes automatically in headless Chrome **only with a normal desktop user-agent** — the
  `HeadlessChrome` UA escalates it to an image challenge. CloakBrowser is optional. See
  [CAPTCHA](captcha.md) and [Authentication](authentication.md).
- A logged-in session sets the Rails career session `_career_session`, the persistent
  `remember_user_token`, and `.habr.com` `s<hex>` SSO cookies; traffic passes through **Qrator**
  (`qrator_msid2`). Cookie inventory is in [Authentication](authentication.md#session-cookies).
- **Vacancy search and listings are server-rendered HTML.** No JSON listing/search endpoint was
  found under `/api/frontend_v1/` or `/v1/`.
- **Vacancy detail embeds `application/ld+json` schema.org `JobPosting`** including `title`,
  `datePosted`, and the full `description` HTML. This is the most stable structured detail source.
- The authenticated identity endpoint is `GET /api/frontend_v1/users/me`, returning
  `{user: {alias, fullName, email, ...}}`.
- Listing modes observed: `?type=all` and `?type=suitable` (resume-scoped for a logged-in user).
- An RSS 2.0 listing feed exists: `/vacancies/rss?page=&per_page=` (observed 200; default page size
  not yet established).
- Vacancy identifiers are numeric, e.g. `/vacancies/1000167594`.
- `/api/frontend_v1/` uses a structured error envelope
  `{"httpCode":404,"errorCode":"NOT_FOUND","message":"Not found","data":{}}`; some routes instead
  return `{"error":"Not found"}`. Both shapes must be tolerated.

## Scope and authority

This wiki is authoritative for observed Habr Career behavior and the integration constraints derived
from it. Product behavior and board-neutral interfaces remain authoritative in the parent jobfucker
specifications: [client contract](../specs/client-contract.md),
[architecture](../specs/architecture.md). The implementation spec for the future
`jobfucker.clients.habr` package will be `docs/specs/habr-client.md` (Session 4).

Raw HARs, cookies, tokens, CSRF values, and challenge captures are deliberately not dependencies
and live only in `/tmp`. The wiki contains the durable, sanitized contracts.
