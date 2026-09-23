# Habr Career Integration Wiki

Technical reference for building and maintaining a `career.habr.com` client for jobfucker. Habr
Career is an external system and can change without notice; revalidate the canaries in each page when
behavior drifts. The wiki describes Habr Career as it behaves, not how it was researched.

> Freshness: surface discovery against live `career.habr.com` on 2026-09-18 with the authenticated
> `habr-exp` account; apply/letter/withdraw surface added 2026-09-19; consolidated 2026-09-23.

## Start here

| Need | Read |
| --- | --- |
| Understand which Habr Career surface owns which operation | [Platform map](platform-map.md) |
| Search, list, and page vacancies | [Search and listings](api/search.md) |
| Decode listing/detail/resume payloads | [Response models](api/response-models.md) |
| Look up filter ids (cities, regions, skills) | [Auxiliary id catalogs](aux/README.md) |
| Log in through Habr Account SSO | [Authentication and session](authentication.md) |
| Understand the login challenge | [CAPTCHA](captcha.md) |
| Apply, add a letter, withdraw, read responses back | [Applications and responses](applications-and-responses.md) |
| Understand transport, statuses, and error mapping | [Transport and errors](api/transport-and-errors.md) |
| See what is not yet established | [Known unknowns](known-unknowns.md) |
| Understand the research method for this wiki | [Research playbook](research-playbook.md) |
| Reuse the HH wiki as the target shape | [HH.ru wiki](../hh/README.md) |

## Core model

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
Apply/letter    ──► POST /api/frontend/vacancies/<id>/responses  (multipart; `body` = letter)
                    PATCH|DELETE .../responses/<rid>             (edit letter / withdraw)
Applied list    ──► GET /responses (HTML) · dialogs GET /api/frontend_v1/chat/conversations
Owned resume    ──► GET /profile (public /<alias>)
```

The website is the primary surface; listing data is JSON (`/api/frontend/vacancies`) and the vacancy
detail page embeds structured JSON, so scraping is not needed. Every `Client` method in the
[client contract](../specs/client-contract.md) has a verified Habr surface
([Platform map](platform-map.md#operation-routing-candidate-surface-per-contract-method)).

## Facts observed

- Habr Career is a **Rails app**. Mutations require `authenticity_token` (form field or
  `X-CSRF-Token` header); a missing token is `422`
  ([Transport](api/transport-and-errors.md#csrf)).
- Login is **Habr Account SSO** via `/users/auth/tmid`; there is no local career password form. A
  fresh credential login is always gated by Yandex SmartCaptcha, but the full login (including the
  captcha) has a verified pure-HTTP path ([Authentication](authentication.md#browserless-login-verified),
  [CAPTCHA](captcha.md#challenge-ladder-how-complexity-rises)).
- A logged-in session sets `_career_session` plus `remember_user_token` and `.habr.com` SSO
  cookies; `remember_user_token` re-issues the career session, while the SSO cookies alone do **not**
  authenticate career ([Authentication](authentication.md#session-cookies)).
- **Anonymous reads work** for listings, vacancy detail, and RSS. Authentication is detected from the
  payload, never the status: anonymous `/api/frontend_v1/users/me` is `200 {}`, and an anonymous
  first request still sets `_career_session`
  ([Platform map](platform-map.md#anonymous-behavior)).
- **Vacancy listings are JSON**: `GET /api/frontend/vacancies?<filters>` (`/api/frontend/`, not
  `/api/frontend_v1/`) returns `{list, meta}`; the detail page embeds the full description
  ([Search](api/search.md), [Response models](api/response-models.md#detail-page)).
- Listing caps: effective page size is **50**; offsets ≥ ~1000 return an empty list; a page beyond
  `meta.totalPages` is `404 {"error":"Not found"}`. Declare `max_search_items = 1000`
  ([Search](api/search.md#pagination-page-size-and-caps)).
- **One account = one resume**, identified by the account alias; no resume id is sent at apply
  ([Response models](api/response-models.md#owned-resume)).
- **Apply is `POST /api/frontend/vacancies/<id>/responses`** (multipart, optional `body` letter);
  letter edit is `PATCH …/responses/<rid>`, withdraw is `DELETE …/responses/<rid>`. `response.kind`
  is `direct` or `applied` ([Applications and responses](applications-and-responses.md)).
- Limits: **~10 s minimum interval** between responses and a **150 responses/month per-account**
  cap; deletes still count and there is no daily cap. Derive `service_info.per_auth_daily_cap` from
  the monthly quota ([Applications and responses](applications-and-responses.md#limits-and-pacing)).
- The site is fronted by **Qrator** (`Server: QRATOR`) and errors come in two shapes:
  `{"error":"Not found"}` and, under `/responses*`, a `{"httpCode","errorCode",…}` envelope
  ([Transport](api/transport-and-errors.md)).
- Habr documents an **OAuth 2.0 employer API** (`/info/api`) for CRM export; it is company-side and
  not a seeker search/apply API ([Applications and responses](applications-and-responses.md#official-api-is-employer-side-not-a-seeker-apply-path)).

## Scope and authority

This wiki is authoritative for observed Habr Career behavior and the integration constraints derived
from it. Product behavior and board-neutral interfaces remain authoritative in the parent jobfucker
specifications: [client contract](../specs/client-contract.md),
[architecture](../specs/architecture.md). The implementation spec for the future
`jobfucker.clients.habr` package (`docs/specs/habr-client.md`) will be written in a separate
interactive session; research itself is complete enough to build against.

Raw HARs, cookies, tokens, CSRF values, and challenge captures are deliberately not dependencies
and live only in `/tmp`. The wiki contains the durable, sanitized contracts.
