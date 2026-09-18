# Platform Map

Habr Career exposes several HTTP surfaces. The website, the JSON endpoints, and the SSO flow are
integration-relevant; static assets and analytics are not.

| Surface | Base | Authentication | Typical content | Use |
| --- | --- | --- | --- | --- |
| Website | `https://career.habr.com/` | Session cookie + Rails CSRF `authenticity_token` | Server-rendered HTML, schema.org JSON-LD | Search listing, vacancy detail, profile, responses, apply UI |
| Search and listings | `GET /api/frontend/vacancies?<filters>` | Session cookie optional (anonymous works) | JSON `{list, meta}` | Primary listing surface ([Search and listings](api/search.md)) |
| Frontend JSON API | `https://career.habr.com/api/frontend_v1/` | Same-origin session cookie | JSON | Identity, notifications, subscriptions, skill suggestions; **no vacancy search/detail** |
| Legacy frontend JSON | `https://career.habr.com/api/frontend/` | Session cookie optional | JSON | Vacancy search, suggestions, apply endpoint hint; not `/frontend_v1` |
| RSS | `https://career.habr.com/vacancies/rss` | Anonymous works | RSS 2.0, fixed latest 50 | Canary only — ignores `page`/`per_page`/`q` |
| SSO / Habr Account | `https://career.habr.com/users/auth/tmid` | Existing Habr Account session | Redirects + login/register pages | Login and registration |
| Static assets | `https://assets.habr.com/career/...` | None | JS/CSS/fonts | Not integration-relevant |
| Analytics | `https://effect.habr.com/a`, `https://stats.habr.com` | Session-scoped | JSON/beacon | Ignore |

`/v1/...` is disallowed by `robots.txt` but returned `404 {"error":"Not found"}` when probed as a
logged-in user; it is not an established API surface.

## Authentication boundaries

- The website session cookie is the credential for both HTML pages and same-origin
  `/api/frontend_v1/` XHR. A separate bearer token has not been observed.
- Mutations require the Rails CSRF token. It appears as a hidden `authenticity_token` field in forms
  and as `<meta name="csrf-token">` on pages; the `X-CSRF-Token` header is **also accepted**
  ([Transport and errors](api/transport-and-errors.md#csrf)).
- Login is delegated to Habr Account SSO. The OAuth authorize step, the `account.habr.com` login
  form, and the browserless login are mapped in [Authentication](authentication.md); the fresh-login
  step is gated by a Yandex SmartCaptcha that is enforced on every fresh credential login
  ([CAPTCHA](captcha.md)). The post-login cookie set is observed.

## Anonymous behavior

Verified 2026-09-18 with a clean cookie jar (see
[Transport and errors](api/transport-and-errors.md#status-and-body-matrix-verified)):

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
| `apply_to_vacancy` | Vacancy detail "Откликнуться"; `quickResponseHref: /api/frontend/quick_responses`; detail `response.kind` | Known-unverified | Apply-mode signal located; request contract is Session 3 |
| `aclose` | n/a | n/a | No background resources observed |
| `service_info.per_auth_daily_cap` | Unknown | Unknown | No limit signal observed yet |
| `service_info.max_search_items` | `/api/frontend/vacancies` accessible-position cap | Verified | Windows at offset ≥ ~1000 return an empty list; declare `1000` ([Search](api/search.md#pagination-page-size-and-caps)) |

## Media and detail enrichment

- Listing data comes from `GET /api/frontend/vacancies` as structured JSON (no scraping needed); see
  [Search and listings](api/search.md) and [Response models](api/response-models.md).
- The vacancy detail page embeds the listing item shape plus the full `description` HTML under
  `"vacancy":{...}`; a `schema.org/JobPosting` JSON-LD block is also present as a fallback canary.
  Prefer the inline JSON over DOM scraping.
- Listing items carry title, company, date, skill names, salary (or a separate `predictedSalary`),
  grade, employment, remote flag, and city chips.
- Company logos are served from `habrastorage.org`.

## Infrastructure behavior

- Every response carries `Server: QRATOR`; the site is fronted by **Qrator**, a WAF/DDoS layer
  distinct from the login CAPTCHA. No interstitial or throttle was observed at low volume, and the
  user-agent did not change the outcome. Transport details, status/error mapping, and pacing are in
  [Transport and errors](api/transport-and-errors.md).
- The Habr Account login step (`account.habr.com`) is gated by **Yandex SmartCaptcha**; it is
  risk-based and escalates from a checkbox to an image (distorted-text) challenge with a
  proof-of-work. The solve contract and a verified pure-HTTP solve are in [CAPTCHA](captcha.md).
  `career.habr.com` showed no challenge under ordinary navigation.
- Rate limits, retry headers, and pacing are not yet characterized. Any challenge encountered must
  be handled per the [CAPTCHA rule](research-playbook.md#captcha-handling-rule).

## Change canaries

Revalidate these small signals when behavior appears to drift:

| Area | Canary |
| --- | --- |
| Identity | `GET /api/frontend_v1/users/me` returns `user.alias` |
| Listing API | `GET /api/frontend/vacancies?q=python&type=all` returns `{list, meta}` with `meta.totalResults` |
| Listing caps | `per_page=100` returns ≤50 items; offsets ≥1000 return an empty `list`; a page beyond `meta.totalPages` is `404` |
| Filters | `qid`/`remote`/`with_salary`/`skills[]`/`city_id`/`employment_type` still change `totalResults`; `sort` ∈ `relevance\|date\|salary_desc\|salary_asc` |
| Suitable | Authenticated `type=suitable` is resume-scoped; anonymous `type=suitable` is ignored |
| Detail | `/vacancies/<id>` embeds a `"vacancy":{...}` JSON with `description`, and a `JobPosting` `ld+json` block |
| Resume | `GET /profile` renders the owned resume; `user.alias` equals the public `/<alias>` slug |
| RSS | `/vacancies/rss` still returns 50 items and ignores `page`/`per_page`/`q` |
| CSRF | Pages still expose `meta[name=csrf-token]` and forms a hidden `authenticity_token` |
| SSO | Login still routes through `/users/auth/tmid` and `account.habr.com/oauth/authorize`, the login form POSTs `email`/`password`/`smart-token`, and a valid login answers JSON `{"success":true,"rurl":".../oauth/authorize/done/<hash>"}` |
| CAPTCHA enforcement | A token-less login POST still answers `{"success":false,"errors":{"smart-token":"..."}}` |
| CAPTCHA | `account.habr.com` login still loads Yandex SmartCaptcha with sitekey `ysc1_zgWuDVpgrG9kwB8QEfIkuWseZyEnRzHLCAPF2dwh1db6e985` |
| CAPTCHA escalation | `POST smartcaptcha.cloud.yandex.ru/check` still answers `{status:"failed",captcha:{type:"checkbox"\|"image"},pow:{complexity:10}}`; `pow` still verifies as `sha256(prefix ++ nonce)` with `complexity` leading zero bits |
| Session | A logged-in session still sets `_career_session` (career) and `.habr.com` `s<hex>` SSO cookies |
| Session refresh | Dropping `_career_session` but keeping `remember_user_token` still returns the identity and re-issues `_career_session` |
| WAF | Responses still carry `Server: QRATOR`; `qrator_msid2` cookie still issued |
| Anonymous reads | `GET /vacancies`, `/vacancies/<id>`, `/vacancies/rss` still `200` with content when anonymous |
| Anonymous identity | `GET /api/frontend_v1/users/me` anonymous still `200 {}` |
| Protected redirect | `GET /responses` anonymous still `302` to `/users/auth_required` |
| Error shapes | Unknown API path still `404 {"error":"Not found"}`; token-less `POST` still `422 {"status":422,...}` |
| CSRF carriers | `X-CSRF-Token` header and `authenticity_token` form field both still accepted |
