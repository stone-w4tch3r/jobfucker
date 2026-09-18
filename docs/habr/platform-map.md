# Platform Map

Habr Career exposes several HTTP surfaces. Only the first two are integration-relevant.

| Surface | Base | Authentication | Typical content | Use |
| --- | --- | --- | --- | --- |
| Website | `https://career.habr.com/` | Session cookie + Rails CSRF `authenticity_token` | Server-rendered HTML, schema.org JSON-LD | Search listing, vacancy detail, profile, responses, apply UI |
| Frontend JSON API | `https://career.habr.com/api/frontend_v1/` | Same-origin session cookie | JSON | Identity, notifications, subscriptions, page-view; **no vacancy search/detail** found |
| RSS | `https://career.habr.com/vacancies/rss` | Observed 200 with a session; anonymous behavior unverified | RSS 2.0 | Machine-readable listing feed |
| SSO / Habr Account | `https://career.habr.com/users/auth/tmid` | Existing Habr Account session | Redirects + login/register pages | Login and registration |
| Static assets | `https://assets.habr.com/career/...` | None | JS/CSS/fonts | Not integration-relevant |
| Analytics | `https://effect.habr.com/a`, `https://stats.habr.com` | Session-scoped | JSON/beacon | Ignore |

`/v1/...` is disallowed by `robots.txt` but returned `404 {"error":"Not found"}` when probed as a
logged-in user; it is not an established API surface.

## Authentication boundaries

- The website session cookie is the credential for both HTML pages and same-origin
  `/api/frontend_v1/` XHR. A separate bearer token has not been observed.
- Mutations require the Rails CSRF token. It appears as a hidden `authenticity_token` field in forms
  and as `<meta name="csrf-token">` on pages. Whether an `X-CSRF-Token` header is also accepted is
  not established.
- Login is delegated to Habr Account SSO. The OAuth authorize step, the `account.habr.com` login
  form, and the browserless login are mapped in [Authentication](authentication.md); the fresh-login
  step is gated by a Yandex SmartCaptcha that is enforced on every fresh credential login
  ([CAPTCHA](captcha.md)). The post-login cookie set is observed.

## Anonymous behavior

Not yet established. Anonymous search rendering is likely (public listing exists in static HTML),
but capability and any challenge behavior must be measured with a clean session before being
treated as fact. Do not infer anonymous access from a logged-in capture.

## Operation routing (candidate surface per contract method)

Statuses: `Verified` (observed end-to-end), `Known-unverified` (surface located, contract not
exercised), `Unknown`.

| Contract method | Candidate surface | Status | Notes |
| --- | --- | --- | --- |
| `get_identity` | `GET /api/frontend_v1/users/me` | Verified | `user.alias` / `fullName` / `email` |
| `authorize` | SSO `/users/auth/tmid` → `account.habr.com` OAuth authorize + login form | Verified | Full browserless pure-HTTP login (captcha via vision OCR + `pow`); cookie set observed (see [Authentication](authentication.md)) |
| `search_vacancies` | `GET /vacancies?page=N&type=all\|suitable` (HTML) | Known-unverified | Server-rendered cards; detail enrichment via JSON-LD |
| `list_vacancies` | Same HTML listing, or `/vacancies/rss` | Known-unverified | RSS shape partially observed |
| `get_resumes` | Own profile (`/{alias}`) / `/profile/specialization` | Unknown | `/api/frontend_v1/resumes` is public specialist search, **not** owned resumes |
| `apply_to_vacancy` | Vacancy detail "Откликнуться" control (JS button, no form) | Unknown | Guest path is `/users/auth/tmid/vacancy/{id}/response`; logged-in submit endpoint unknown |
| `aclose` | n/a | n/a | No background resources observed |
| `service_info.per_auth_daily_cap` | Unknown | Unknown | No limit signal observed yet |
| `service_info.max_search_items` | Unknown | Unknown | No listing cap observed yet |

## Media and detail enrichment

- The vacancy detail page carries a `schema.org/JobPosting` JSON-LD block with `title`, `datePosted`,
  and the full `description` HTML. Prefer this over scraping card markup for detail fields.
- Listing cards carry title, company, date, skills chips, salary (or a predicted-salary block),
  grade, remote flag, and city chips.
- Company logos are served from `habrastorage.org`.

## Infrastructure behavior

- Traffic passes through **Qrator** (`qrator_msid2` cookie, ~15 min lifetime), a WAF/DDoS layer
  distinct from the login CAPTCHA.
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
| Listing | `GET /vacancies?page=1` renders `.vacancy-card` items |
| RSS | `GET /vacancies/rss?page=1&per_page=25` returns RSS 2.0 items |
| Detail | Vacancy page contains a `JobPosting` `ld+json` block |
| CSRF | Pages still expose `meta[name=csrf-token]` and forms a hidden `authenticity_token` |
| SSO | Login still routes through `/users/auth/tmid` and `account.habr.com/oauth/authorize`, the login form POSTs `email`/`password`/`smart-token`, and a valid login answers JSON `{"success":true,"rurl":".../oauth/authorize/done/<hash>"}` |
| CAPTCHA enforcement | A token-less login POST still answers `{"success":false,"errors":{"smart-token":"..."}}` |
| CAPTCHA | `account.habr.com` login still loads Yandex SmartCaptcha with sitekey `ysc1_zgWuDVpgrG9kwB8QEfIkuWseZyEnRzHLCAPF2dwh1db6e985` |
| CAPTCHA escalation | `POST smartcaptcha.cloud.yandex.ru/check` still answers `{status:"failed",captcha:{type:"checkbox"\|"image"},pow:{complexity:10}}`; `pow` still verifies as `sha256(prefix ++ nonce)` with `complexity` leading zero bits |
| Session | A logged-in session still sets `_career_session` (career) and `.habr.com` `s<hex>` SSO cookies |
| WAF | `qrator_msid2` cookie still issued |
