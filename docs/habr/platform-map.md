# Platform Map

Habr Career exposes several HTTP surfaces. The website, the JSON endpoints, and the SSO flow are
integration-relevant; static assets and analytics are not.

| Surface | Base | Authentication | Typical content | Use |
| --- | --- | --- | --- | --- |
| Website | `https://career.habr.com/` | Session cookie + Rails CSRF `authenticity_token` | Server-rendered HTML, schema.org JSON-LD | Search listing, vacancy detail, profile, responses, apply UI |
| Search and listings | `GET /api/frontend/vacancies?<filters>` | Session cookie optional (anonymous works) | JSON `{list, meta}` | Primary listing surface ([Search and listings](api/search.md)) |
| Frontend JSON API | `https://career.habr.com/api/frontend_v1/` | Same-origin session cookie | JSON | Identity, notifications, subscriptions, skill suggestions, `chat/conversations`; **no vacancy search/detail** |
| Legacy frontend JSON | `https://career.habr.com/api/frontend/` | Session cookie optional | JSON | Vacancy search, suggestions, vacancy responses (apply/update/withdraw); not `/frontend_v1` |
| Employer API (official) | `https://career.habr.com/api/v1/integrations/` | OAuth 2.0 access token | JSON | Company vacancies + inbound responses with resumes; **employer CRM export, not a seeker apply path** ([Applications and responses](applications-and-responses.md#official-api-is-employer-side-not-a-seeker-apply-path)) |
| RSS | `https://career.habr.com/vacancies/rss` | Anonymous works | RSS 2.0, fixed latest 50 | Canary only — ignores `page`/`per_page`/`q` |
| SSO / Habr Account | `https://career.habr.com/users/auth/tmid` | Existing Habr Account session | Redirects + login/register pages | Login and registration ([Authentication](authentication.md)) |
| Static assets | `https://assets.habr.com/career/...` | None | JS/CSS/fonts | Not integration-relevant |
| Analytics | `https://effect.habr.com/a`, `https://stats.habr.com` | Session-scoped | JSON/beacon | Ignore |

`/v1/...` is disallowed by `robots.txt` but returned `404 {"error":"Not found"}` when probed as a
logged-in user; it is not an established API surface.

Generated id catalogs for search filters (cities, regions, countries, skills) live in
[aux/](aux/README.md); they are a prefix-crawl snapshot, not an official dictionary.

## Authentication boundaries

- The website session cookie is the credential for both HTML pages and same-origin
  `/api/frontend_v1/` XHR. A separate bearer token has not been observed.
- Mutations require the Rails CSRF token (form field or `X-CSRF-Token` header) —
  [Transport and errors](api/transport-and-errors.md#csrf).
- Login is delegated to Habr Account SSO; the fresh-login step is gated by a Yandex SmartCaptcha
  enforced on every fresh credential login ([Authentication](authentication.md),
  [CAPTCHA](captcha.md)).

## Anonymous behavior

Verified 2026-09-18 with a clean cookie jar; the status/body matrix is owned by
[Transport and errors](api/transport-and-errors.md#status-and-body-matrix-verified).

- Read surfaces are anonymous: `GET /vacancies`, `GET /vacancies/<id>` (with `JobPosting` JSON-LD),
  and `GET /vacancies/rss` all return `200` with content and no challenge.
- `GET /responses` is protected: `302` → `/users/auth_required` (HTML, no form).
- `GET /api/frontend_v1/users/me` returns `200 {}`; authentication is detected from the payload
  (`user` present), never the status.
- An anonymous first request still sets `_career_session`, so cookie presence is not an auth signal.
- Anonymous listing pages are larger than authenticated ones (login/registration promo markup);
  card markup (`vacancy-card`) is the same. Do not infer identity from page size.

## Operation routing (candidate surface per contract method)

Statuses: `Verified` (observed end-to-end), `Known-unverified` (surface located, contract not
exercised), `Unknown`.

| Contract method | Candidate surface | Status | Notes |
| --- | --- | --- | --- |
| `get_identity` | `GET /api/frontend_v1/users/me` | Verified | `user.alias` / `fullName` / `email`; anonymous call returns `200 {}` |
| `authorize` | SSO `/users/auth/tmid` → `account.habr.com` OAuth authorize + login form | Verified | Full browserless pure-HTTP login (captcha via vision OCR + `pow`); cookie set observed (see [Authentication](authentication.md)) |
| `search_vacancies` | `GET /api/frontend/vacancies?q=&type=all\|suitable&...` + `GET /vacancies/<id>` per item | Verified | JSON listing ([Search](api/search.md)) + detail inline `"vacancy"` JSON with full `description` ([Response models](api/response-models.md#detail-page)) |
| `list_vacancies` | `GET /api/frontend/vacancies?<filters>` | Verified | `meta.totalResults`→`found`; `list[]`→`VacancyShort`; listing-only, no enrichment; RSS is not viable |
| `get_resumes` | Own profile `GET /profile` (public `/{alias}`) | Verified | One profile-resume; `resume_id` observed as the account alias; no owned-resume JSON endpoint found |
| `apply_to_vacancy` | `POST /api/frontend/vacancies/<id>/responses` (multipart, optional `body` letter); `PATCH`/`DELETE` on `…/responses/<rid>` | Verified | Apply + letter + withdraw; `response.kind` `direct`/`applied`; full `ApplyResult` map and limits ([Applications and responses](applications-and-responses.md)) |
| `aclose` | n/a | n/a | No background resources observed |
| `service_info.per_auth_apply_cap` + `apply_period` | `POST …/responses` monthly quota | Verified | **150 responses/month** per account, not per IP (not daily); declare `per_auth_apply_cap = 150`, `apply_period = "month"`; 151st → `400 {"message":"Можно оставлять не более 150 откликов в месяц"}`; deletes still count; reset boundary unconfirmed (calendar month assumed) and accepted as non-blocking ([Applications and responses](applications-and-responses.md#limits-and-pacing)) |
| `service_info.max_search_items` | `/api/frontend/vacancies` accessible-position cap | Verified | Windows at offset ≥ ~1000 return an empty list; declare `1000` ([Search](api/search.md#pagination-page-size-and-caps)) |

## Detail enrichment

- Listing data comes from `GET /api/frontend/vacancies` as structured JSON (no scraping needed); see
  [Search and listings](api/search.md) and [Response models](api/response-models.md).
- The vacancy detail page embeds the full description under an inline `"vacancy":{...}` JSON, with a
  `schema.org/JobPosting` `ld+json` block as a fallback canary
  ([Response models](api/response-models.md#detail-page)).
- Company logos are served from `habrastorage.org`.

## Infrastructure behavior

Qrator fronts the site (`Server: QRATOR`, `qrator_msid2`), separate from the login CAPTCHA. No
interstitial or throttle was observed at low volume, but interstitial triggers, rate limits, and
retry headers are uncharacterized; the canonical rules and observed limits live in
[Transport and errors](api/transport-and-errors.md#rate-limits-and-qrator).
Any challenge encountered must be handled per the [CAPTCHA rule](research-playbook.md#captcha-handling-rule).
