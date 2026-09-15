# Transport and Errors

## Request profile

Use one persistent HTTP client per HH profile. It owns cookies, token state, throttling, and retry
state. Keep website cookies even though ordinary API requests are Bearer-only; login, authorize,
CAPTCHA, and web-only endpoints need them.

Recommended headers:

```http
User-Agent: Mozilla/5.0 (...) Chrome/... Safari/537.36
X-HH-App-Active: true
Authorization: Bearer <USER...>   # api.hh.ru authenticated calls only
```

Header names are case-insensitive. An access token observed from HH begins with `USER`; validate
that as a corruption/configuration check and return a typed failure rather than asserting.

An Android-looking UA of the form `ru.hh.android/7.…` was less reliable through DDoS-Guard when
used by generic HTTP clients. A Chrome-like UA is the default for API, website, and OAuth traffic.

## Pacing and retries

- Keep at least `0.345 s` between ordinary requests.
- Wait a random `1–3 s` before an application POST or message-send POST.
- Retry transient connection/TLS failures and selected upstream `5xx` failures with bounded
  exponential backoff and jitter.
- Do not blindly retry business errors, authentication failures, invalid parameters, or an apply
  POST whose outcome is unknown.
- Keep redirects disabled by default. A `302` is part of OAuth and CAPTCHA success detection, and a
  `3xx` application response represents an external flow.

The DDoS layer can terminate a TLS connection without an HTTP response. Preserve that distinction
as `transport_unavailable`; do not manufacture an HH status or envelope.

## Request encoding

| Method/use | Encoding |
| --- | --- |
| API `GET`, `DELETE` | Query parameters |
| API `POST`, `PUT` | Form body unless endpoint explicitly requires JSON |
| `POST /negotiations` | `application/x-www-form-urlencoded` |
| Website login | Multipart form |
| OAuth token exchange/refresh | `application/x-www-form-urlencoded` |
| Standalone CAPTCHA submission | Query parameters on `POST`, plus XSRF headers |

Empty bodies are valid. Decode as an endpoint-specific unit value; in particular, application
success is HTTP `201` plus zero body bytes.

## Success classification

Do not implement `2xx == success` globally.

```text
1. Classify network failure.
2. Parse JSON when the endpoint promises JSON; retain content type and a bounded body excerpt.
3. Run challenge detection on every response.
4. Recognize the endpoint's success schema/status.
5. If a body contains `errors`, classify it as failure even when status is 2xx.
6. Map remaining status/envelope pairs to typed errors.
```

Search is the important trap: invalid enum values can return HTTP `200` with an ordinary API error
envelope. Search success requires all of:

- a 2xx response;
- no `errors` array;
- `items` and `found` fields of the expected types.

## API error envelope

```json
{
  "description": "optional human message",
  "error_description": "optional alternate message",
  "bad_argument": "optional parameter name",
  "bad_arguments": [{"name": "optional", "description": "optional"}],
  "errors": [
    {
      "type": "machine category",
      "value": "optional machine value",
      "captcha_url": "present for captcha_required"
    }
  ],
  "request_id": "optional request identifier"
}
```

Fields are optional and additive. Preserve `request_id`, status, `type`, `value`, and
`bad_argument` for diagnostics. When the body has no `request_id`, use the response's
`X-Request-ID` header; it is present on successful as well as failed API responses. Choose a
display message in this order:

1. `error_description`
2. `description`
3. joined `errors[].type[:value]`
4. a safe serialized summary

## OAuth error envelope

OAuth uses a separate contract:

```json
{
  "error": "invalid_grant",
  "error_description": "token not expired"
}
```

Keep OAuth errors distinct because the machine code drives recovery. Examples:

| Error | Meaning |
| --- | --- |
| `invalid_request`, invalid `redirect_uri` | Authorize URL does not use exactly `hhandroid://oauthresponse` |
| `invalid_request`, `bad redirect url` | Token exchange omitted/mismatched `redirect_uri` |
| `invalid_grant`, `token not expired` | Client attempted refresh before access-token expiry |

## Business-error mapping

Classify by envelope before broad status mapping:

| Signal | Meaning | Recovery |
| --- | --- | --- |
| `captcha_required` + `captcha_url` | Recoverable challenge | Run [CAPTCHA](../captcha.md), then retry original request within a bound |
| `already_applied` | Application already exists | Idempotent skipped/already-applied outcome |
| `test_required` | Vacancy test must be completed | Skip in clients that do not implement tests |
| `resume_not_found` | Resume does not belong to token/account or ID is bad | Configuration/user error; do not retry |
| `limit_exceeded` | Board application cap reached | Stop the batch; leave unattempted vacancies pending |
| `bad_argument` | Parameter rejected | Local validation error; do not retry |
| `bad_request` | Cross-field constraint violated | Local validation error; do not retry |
| `not_found` / HTTP 404 | Resource absent | Typed not-found |
| `forbidden` without a challenge/business marker | Missing/expired authorization | Refresh only when access token is genuinely expired; otherwise reauthorize |
| `bad_gateway` / 502 | HH upstream failure | Bounded retry when operation is safe |

Broad fallback mapping:

- remaining `4xx` → rejected request, preserving status/envelope;
- remaining `5xx` → upstream failure;
- expected JSON but non-JSON/HTML → protocol drift unless challenge markers identify a CAPTCHA.

Route HTTP 401 to the same authorization coordinator as a non-challenge 403. Refresh is still
allowed only when local token state says the access token has genuinely expired. The 401 branch is
defensive; it was not exercised in the live verification matrix.

## Challenge classifier

Challenge detection belongs in the shared transport and runs before ordinary status mapping.

| Signal | Kind |
| --- | --- |
| API envelope has captcha-like `errors[].type`/`value` and `captcha_url` | Standalone HH text CAPTCHA |
| Login response has `captchaState`/`captchaError` or `errors[].description.isBot=true` | Embedded login CAPTCHA; do not use the standalone submit route |
| HTML contains `account-login-form` plus `account-captcha-picture` | Embedded login CAPTCHA |
| HTML/JSON has standalone `account-captcha-submit` or `hhcaptcha` state outside login | Standalone HH text CAPTCHA |
| Body contains reCAPTCHA script/iframe/site-key or `g-recaptcha-response` markers | reCAPTCHA/unknown solver |
| Structurally abnormal HTML or unmapped challenge-like 4xx | Unknown challenge |

Ordinary `already_applied`, `test_required`, `resume_not_found`, and `limit_exceeded` envelopes are
not CAPTCHAs. An unknown challenge must fail explicitly with captured metadata, not enter a retry
loop.

Do not classify the generic word `recaptcha` or the login SPA's dormant top-level
`recaptcha`/`hhcaptcha` configuration as a challenge. The normal successful login shell contains
those fields even when no widget, script, challenge state, or challenge URL is active.

## Cookies and XSRF

Persist only HH-family cookies. A suitable domain allow-list is equivalent to:

```regex
^(?!israel\.)(?:.*?\.)?hh\.(ru|kz|uz|by|net|com)\.?$
```

This excludes unrelated tracker cookies such as `.mts.ru`.

For website state-changing calls, use `_xsrf` from the cookie jar. If absent, fetch `https://hh.ru/`
and extract the server-provided `xsrfToken`, preferring the value that equals the cookie. Send it in
`X-Xsrftoken`; XHR-style calls also use `X-Requested-With: XMLHttpRequest` and an appropriate
`Referer`/`X-Hhtmfrom`/`X-Hhtmsource` when required.

See [Platform map](../platform-map.md#authentication-boundaries) for cookie/Bearer boundaries and
[Authentication](../authentication.md#session-cookies) for the meaning of individual cookies.
