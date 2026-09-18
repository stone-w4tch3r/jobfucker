# Authentication and Session

Habr Career uses **Habr Account SSO**. There is no local career password form. This page maps the
SSO chain, the login form, and the post-login cookie/session contract. Login is gated by a Yandex
SmartCaptcha (see [CAPTCHA](captcha.md)) that is **enforced on every fresh credential login**; the
full login, including the captcha, was completed **browserlessly over pure HTTP**.

> Freshness: chain and browserless login observed 2026-09-18 against live `account.habr.com` /
> `career.habr.com`.

## Outcome

```text
career.habr.com protected page
    │  no session
    ▼
GET /users/auth_required            (HTML interstitial, no form)
    │  "Войти в Хабр Аккаунт"
    ▼
GET /users/auth/tmid
    │  302
    ▼
account.habr.com/oauth/authorize/?response_type=code
    &client_id=career-<uuid>
    &redirect_uri=https://career.habr.com/users/auth/tmid/callback_oauth
    &state=bslogin&action=login
    │
    ▼
GET account.habr.com/ru/ident/<state-token>     ← login form + Yandex SmartCaptcha
    │  POST /ru/ident/in/<state-token>  (XHR, 200, JSON success/rurl)
    │  fields: email, password, smart-token
    ▼
GET account.habr.com/oauth/authorize/done/<hash>
    │  302
    ▼
GET career.habr.com/users/auth/tmid/callback_oauth?state=bslogin&code=<code>
    │  sets career session
    ▼
GET career.habr.com/ → GET career.habr.com/vacancies
    │
    ▼
career.habr.com session cookie (`_career_session`, `remember_user_token`, SSO `.habr.com` cookies)
```

## Unauthenticated routing

- `GET https://career.habr.com/responses` (and other protected pages) redirects to
  `https://career.habr.com/users/auth_required`.
- `/users/auth_required` is an HTML page, HTTP 200, with **no form**. It offers two links:
  - `https://career.habr.com/users/auth/tmid` (sign in)
  - `https://career.habr.com/users/auth/tmid/register` (register)
- Text: only authenticated users can view the page; sign in to Habr Account, which then links to
  the career profile.

## OAuth authorize

`GET https://career.habr.com/users/auth/tmid` redirects to:

```text
https://account.habr.com/oauth/authorize/?response_type=code
  &client_id=career-<uuid>
  &redirect_uri=https://career.habr.com/users/auth/tmid/callback_oauth
  &state=bslogin
  &action=login
```

- `response_type=code` — authorization-code flow, code delivered to the career callback.
- `client_id` is a static `career-<uuid>` client registration.
- `redirect_uri` is fixed to `/users/auth/tmid/callback_oauth`.
- `state=bslogin` and `action=login` are set by the career app for an interactive sign-in.
- With no Habr Account session, authorize renders the login form instead of issuing a code.

## Login form

Rendered at `GET https://account.habr.com/ru/ident/<state-token>` (a per-attempt opaque token in the
path).

| Field | Type | Notes |
| --- | --- | --- |
| `email` | email | required |
| `password` | password | required |
| `smart-token` | hidden | Yandex SmartCaptcha token, populated after the captcha passes (checkbox or image) |

- Submit: `POST https://account.habr.com/ru/ident/in/<state-token>`.
- No `authenticity_token` on this form — it is a Habr Account form, not the Rails career app.
- The page also offers external IdP buttons: GitHub, VK, Google, Facebook, Twitter, Yandex
  (`POST /ru/extidp/<provider>/prompt` with a hidden `token`).
- A Yandex SmartCaptcha placeholder is embedded in the form
  (`<div data-captcha="yandex" data-sitekey="<sitekey>">`, widget injected by JS) and must pass
  before submit; see [CAPTCHA](captcha.md). On success the hidden `smart-token` is filled with the
  widget `spravka`, the form POSTs as an XHR and returns JSON, then the page navigates client-side.

### Captcha enforcement

The captcha is **required on every fresh credential login** as observed; the server rejects a
credential POST without a valid token:

```json
{"success": false, "errors": {"smart-token": "Необходимо пройти капчу"}}
```

The same error is returned for a missing and for a bogus `smart-token`. The login page always
renders the captcha placeholder and a fallback that states "Анти-робот проверка не загрузилась. Без
неё продолжить не получится." No fresh-login attempt was observed without a captcha. An **existing
Habr Account session skips the login form and the captcha entirely** (SSO proceeds without it).

### Submit contract

The form is submitted by `cform` (jQuery AJAX) with `dataType:"json"` and the serialized form
fields (`email`, `password`, `smart-token`). Response contract:

| Response | Meaning |
| --- | --- |
| `{"success": true, "rurl": "<url>"}` | Login accepted; navigate to `rurl` |
| `{"success": false, "errors": {"<field>": "<msg>"}}` | Field validation (e.g. captcha) |
| `{"success": false, "error": "<msg>"}` | Generic error |

On `success:true` the client follows `rurl`, which is
`https://account.habr.com/oauth/authorize/done/<hash>`.

### Browserless login (verified)

The entire fresh login completes over pure HTTP, no browser, on 2026-09-18:

1. `GET https://career.habr.com/users/auth/tmid` with a cookie jar → login page
   `account.habr.com/ru/ident/<state>`.
2. Solve the SmartCaptcha browserlessly (checkbox → image escalation → OCR → `pow` → `spravka`);
   see [Auto-solve feasibility](captcha.md#auto-solve-feasibility-verified).
3. `POST https://account.habr.com/ru/ident/in/<state>` with
   `email`, `password`, `smart-token=<spravka>` (form-urlencoded, `X-Requested-With: XMLHttpRequest`)
   → `{"success":true,"rurl":"https://account.habr.com/oauth/authorize/done/<hash>"}`.
4. `GET rurl` with the same jar → career session established.
5. `GET https://career.habr.com/api/frontend_v1/users/me` → `user.alias` returned.

The only non-deterministic step is reading the distorted-text image, which needs a vision/OCR model;
everything else is deterministic HTTP. The `smart-token` field name is the only captcha-specific
part of the login POST.

## Post-login chain

The login JSON's `rurl` gives the first hop; the client then walks:

```text
GET https://account.habr.com/oauth/authorize/done/<hash>
  → GET https://career.habr.com/users/auth/tmid/callback_oauth?state=bslogin&code=<code>
  → GET https://career.habr.com/
  → GET https://career.habr.com/vacancies
```

The `code` is the OAuth authorization code exchanged by the career callback for the career session.
No career-side password or token field exists; the career session is established by this callback.
The `done/<hash>` value is not derivable from the form; it comes from the login response.

## Session cookies

Observed cookie inventory after a successful login (values redacted; `data.cookies` from
`agent-browser cookies get --json`):

| Name | Domain | Path | Lifetime | Flags |
| --- | --- | --- | --- | --- |
| `_career_session` | `career.habr.com` | `/` | session | HttpOnly, Lax |
| `remember_user_token` | `career.habr.com` | `/` | ~1 month | HttpOnly, Lax |
| `mid` | `career.habr.com` | `/` | ~13 months | Lax |
| `vtypev2` | `career.habr.com` | `/` | session | Lax |
| `check_cookies` | `career.habr.com` | `/` | session | Lax |
| `s61687262000a` | `.habr.com` | `/` | ~1 month | HttpOnly, Lax |
| `s69676e616c` | `.habr.com` | `/` | ~1 day | HttpOnly, Lax |
| `qrator_msid2` | `.habr.com` | `/` | ~15 minutes | HttpOnly |
| `_ga`, `_ga_*`, `_gid` | `.habr.com` | `/` | analytics | — |
| `_fbp`, `mp_*_mixpanel` | `.habr.com` | `/` | analytics | — |

Notes:

- `_career_session` is the Rails career session (session-scoped). `remember_user_token` is the
  persistent-login token; together they are what makes a logged-in agent session survive restarts.
- **An anonymous first request also sets `_career_session`** (cookie-store session for flash/CSRF),
  so the cookie's presence is not an authentication signal. The identity call's payload is.
- The `s<hex>` cookies on `.habr.com` are the SSO/account session carriers; they are HttpOnly and
  have shorter lifetimes.
- `qrator_msid2` is a Qrator WAF cookie (see below), not a login credential.
- Analytics cookies are not required for authentication.

## Session lifecycle (refresh and logout)

Observed 2026-09-18 over pure HTTP with the exported cookie jar; each case rebuilt the jar from the
same cookie set, so no server-side state was created.

| Case | Cookies sent | Result |
| --- | --- | --- |
| Drop `_career_session`, keep `remember_user_token` | remember + SSO | `200` full user; a **new `_career_session` is re-issued** |
| Keep `_career_session`, drop `remember_user_token` | session + SSO | `200` full user |
| Keep only `.habr.com` SSO cookies (`s<hex>`, `habr_uuid`) | SSO only | `200 {}` — **SSO cookies alone do not authenticate career** |
| Keep only `remember_user_token` | remember only | `200` full user; career session re-issued |

Conclusions:

- `remember_user_token` transparently re-establishes a career session; it is the persistence
  mechanism. `.habr.com` SSO cookies do not authenticate career by themselves.
- `_career_session` is a **Rails cookie-store session**: after `sign_out` the cookie value captured
  before logout still authenticated `GET /api/frontend_v1/users/me` until its own expiry. Treat
  logout as client-side cookie deletion plus `remember_user_token` revocation, **not** as
  server-side session invalidation. Never rely on logout to kill a leaked session cookie.
- Anonymous protected access is a redirect, not a `401`: `GET /responses` → `302`
  `/users/auth_required` (HTML, no form).

### Logout contract

- `POST /users/sign_out` with a hidden `_method=delete`; rendered as a Rails form in the account
  menu with a hidden `authenticity_token`.
- CSRF is required: no token → `422`; a valid `authenticity_token` **form field** → `302`; a valid
  `X-CSRF-Token` **header** (no form field) → `302`. Both carriers are accepted.
- `meta.logoutToken` from `GET /api/frontend_v1/users/me` is **not** part of the HTML logout — the
  page contains no `logoutToken`, and a form-token logout succeeded without it. Its use (if any) is
  unestablished.
- After logout the career cookies are cleared in the response and `remember_user_token` no longer
  refreshes a session; the pre-logout `_career_session` value remained replayable (cookie store).

## Infrastructure

- `qrator_msid2` (HttpOnly, ~15 min) indicates Habr/Habr Career traffic passes through **Qrator**, a
  WAF/DDoS-protection layer. Expect a possible interstitial or cookie refresh under suspicion; this
  is separate from the Yandex SmartCaptcha login gate.
- `account.habr.com` login loads Yandex SmartCaptcha; `career.habr.com` did not show a challenge for
  ordinary navigation in this session.

## Career-side session and CSRF

- Career HTML pages expose `<meta name="csrf-token">` and `<meta name="csrf-param"
  content="authenticity_token">`; Rails forms include a hidden `authenticity_token`.
- State-changing requests require the token and answer `422 {"status":422,"error":"Unprocessable
  Entity"}` without it. Both the form field and the `X-CSRF-Token` header are accepted
  ([Transport and errors](api/transport-and-errors.md#csrf)).
- Logout is a Rails form: `POST /users/sign_out` with `_method=delete` and `authenticity_token`
  (see [Logout contract](#logout-contract)).
- Same-origin JSON API calls (e.g. `GET /api/frontend_v1/users/me`) authenticate with the session
  cookie and require no token for reads; an anonymous call returns `200 {}`.
- The cookie set above was observed after a successful login; a browserless cookie jar established
  by the same chain also authenticated `GET /api/frontend_v1/users/me` (200).

## Authenticated identity

`GET /api/frontend_v1/users/me` returns (shape sanitized):

```json
{
  "user": {
    "alias": "<string>",
    "fullName": "<string>",
    "email": "<string>",
    "avatarUrl": "<url>",
    "jobSearchState": "search",
    "qualificationId": 3,
    "isAdmin": false,
    "isExpert": false
  },
  "meta": {"logoutToken": "<string>"},
  "userCompanies": []
}
```

`meta.logoutToken` is a secret; never persist or log it.

Anonymous callers receive `200 {}` (empty object), not `401` — authentication must be detected from
the payload (`user` present), never from the status code.

## Verification status

| Item | Status |
| --- | --- |
| Unauthenticated redirect to `/users/auth_required` | Verified |
| SSO authorize URL and parameters | Verified |
| Login form fields and POST target | Verified |
| SmartCaptcha gate on login | Verified (enforced; `errors.smart-token` without a token) |
| Login POST shape (XHR, JSON `success`/`rurl`) | Verified |
| Post-login callback and cookie set | Verified |
| Cookie inventory and lifetimes | Verified (values redacted) |
| Browserless (pure-HTTP) full login | Verified (captcha solved via vision OCR + `pow`) |
| Existing account session skips login/captcha | Verified |
| Session refresh via `remember_user_token` | Verified (re-issues `_career_session`) |
| SSO `.habr.com` cookies authenticate career | Verified negative (no session from SSO alone) |
| Logout contract (`POST /users/sign_out`, CSRF) | Verified (form field and header both accepted; `logoutToken` not needed) |
| CSRF enforcement on mutations | Verified (`422` without a token) |
| Anonymous `GET /api/frontend_v1/users/me` → `200 {}` | Verified |
| Session expiry durations / remember-token TTL | Unknown |
