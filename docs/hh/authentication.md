# Authentication and OAuth

## Outcome

The applicant client can authenticate end-to-end without a browser, including the researched
embedded login challenge:

```text
GET /account/login?backurl=/&oauth=true&response_type=code → _xsrf + cookies
    │
multipart POST /account/login
    ├─ hhcaptcha.isBot=false → OAuth-capable hhtoken
    └─ hhcaptcha.isBot=true → key → PNG → solve → repeat login POST with CAPTCHA fields
    │
GET /oauth/authorize → 302 hhandroid://oauthresponse?code=…
    │
POST /oauth/token → access_token + refresh_token
    │
GET api.hh.ru/me → authenticated identity
```

A browser can drive either login UI as a fallback, but is not required for normal credential auth.

## OAuth client

The verified mobile-client defaults are:

```text
client_id     HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD
client_secret V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS
redirect_uri  hhandroid://oauthresponse
```

These are embedded third-party application credentials, not user secrets. They are an operational
dependency that HH can rotate or revoke. Prefer a separately registered application when one is
available, and never mix these constants with user credentials or persisted tokens.

## Browserless credential login

1. `GET https://hh.ru/account/login?backurl=/&oauth=true&response_type=code` with a persistent
   cookie jar.
2. Read `_xsrf` from the jar or the hidden form field.
3. Submit multipart form data:

   ```http
   POST https://hh.ru/account/login?backurl=%2F&oauth=true&response_type=code

   _xsrf=<cookie value>
   failUrl=/account/login?backurl=/&oauth=true&response_type=code
   accountType=EMPLOYER
   remember=yes
   username=<email or phone>
   password=<password>
   password=<password>
   isBot=false
   ```

4. Send the XSRF/XHR headers documented in [Login CAPTCHA](captcha-login.md), and retain all
   allowed HH cookies. The browser-only `loginTrustFlags` and fingerprint headers are not required.
5. Do not judge success from the response HTML or `hhrole`. Immediately attempt the OAuth authorize
   request. A 302 code redirect proves login succeeded.

The login POST may return an apparently anonymous SPA shell even while it has set an OAuth-capable
HttpOnly `hhtoken`. This is expected.

The successful JSON SPA shell can contain dormant top-level `recaptcha` and `hhcaptcha`
configuration, including a site-key field. Those names alone are not an active challenge. Require
an active challenge state, rendered widget/script marker, or challenge response field before
classifying reCAPTCHA; an `hhtoken` followed by the authorize-code redirect remains decisive.

If the login response contains `captchaState`, `errors[].description.isBot=true`, or an embedded
`account-captcha-picture` inside `account-login-form`, classify it as the embedded login challenge.
It is not the verified standalone `captcha_url` protocol. Use the recovery branch in
[Login CAPTCHA](captcha-login.md) — re-submit the login form with `captchaKey`, `captchaText`, and
`captchaState` — rather than posting the answer independently. If HH demands a one-time code,
delegate it through the application's human
interaction seam; do not bypass or guess it.

## Authorization code

Send this request with the authenticated website cookie jar and redirects disabled:

```http
GET https://hh.ru/oauth/authorize
  ?client_id=<client_id>
  &redirect_uri=hhandroid%3A%2F%2Foauthresponse
  &response_type=code
  &state=<cryptographically-random-state>
  &skip_choose_account=true
  &oauth=true
  &hhtmFrom=account_login
```

Omit `scope` when empty. Validate the returned `state` if HH includes it.

Success:

```http
HTTP/1.1 302 Found
Location: hhandroid://oauthresponse?code=<authorization-code>&state=<state>
```

The custom scheme may make a headed browser open an OS external-handler dialog. HTTP code capture
avoids that: read `Location` without following the redirect.

Without `skip_choose_account=true`, `oauth=true`, and `hhtmFrom=account_login`, an authenticated
session can receive a `200` account-choice page instead of the code redirect.

## Token exchange

```http
POST https://hh.ru/oauth/token
Content-Type: application/x-www-form-urlencoded

client_id=<client_id>
client_secret=<client_secret>
code=<authorization-code>
grant_type=authorization_code
redirect_uri=hhandroid://oauthresponse
```

`redirect_uri` is required in both authorize and exchange and must match exactly.

Response:

```json
{
  "access_token": "USER…",
  "refresh_token": "…",
  "expires_in": 1209599,
  "token_type": "bearer"
}
```

The observed lifetime was roughly fourteen days; trust `expires_in`, not that observation. Store an
absolute expiry timestamp using a monotonic-safe wall-clock policy and a small request-time skew.
Protect access tokens, refresh tokens, cookies, credentials, and captured authorization codes as
secrets.

## Refresh

Refresh only after the access token is actually expired:

```http
POST https://hh.ru/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
refresh_token=<refresh-token>
```

No client ID, client secret, or redirect URI was required for refresh. A pre-expiry refresh is
rejected with OAuth error `invalid_grant` / `token not expired`.

Single-use refresh-token rotation has not been verified through a real expiry. Implement the safe
assumption: when refresh succeeds, atomically replace both tokens and never reuse the old refresh
token. See [Known unknowns](known-unknowns.md#token-longevity).

## Authorization decision flow

```text
load token state
  ├─ access token exists and not expired → GET /me
  │    ├─ 200 → authorized
  │    └─ 401/403 auth rejection → reauthorize; do not refresh a still-valid token
  ├─ expired + refresh token → refresh once → persist atomically → GET /me
  └─ no usable token → credential login → authorize → exchange → GET /me
```

If `/me` returns a challenge, solve it before deciding the token is bad. Treat HTTP 401 and a
non-challenge HTTP 403 as possible authorization failures. Refresh only when the stored access
token is genuinely expired; otherwise fall back to full authorization. HTTP 401 was not observed
in the live matrix, so this is a defensive recovery rule rather than a claim about HH's current
preferred expiry status.

## Session cookies

| Cookie/state | Meaning |
| --- | --- |
| `hhtoken` | HttpOnly, OAuth-decisive website session cookie; minimally sufficient for authorize code capture |
| `_xsrf` | Required for website state-changing requests |
| `hhrole`, `hhul` | Web presentation/session hints; not proof of OAuth capability |
| DDoS-Guard cookies | Transport/anti-bot state; useful to retain but not sufficient for auth |

A cold browser navigation to the homepage can rotate a previously imported `hhtoken` into an
anonymous context. If importing a session into a browser, make the OAuth authorize URL the first
HH navigation. For the pure-HTTP client, validate directly with authorize or `/me`.

Cookie filtering and XSRF extraction are covered in
[Transport and errors](api/transport-and-errors.md#cookies-and-xsrf).

## Browser login surfaces

HH maintains a direct new-SPA form and a legacy OAuth-context form. They have different DOM
selectors and must be recognized from the page rather than guessed from a broadly similar URL.
See [Website behavior and DOM](website.md#login-surfaces).
