# Login CAPTCHA

The CAPTCHA embedded in the credential login flow. Same image mechanics as the standalone
challenge ([CAPTCHA](captcha.md)), but a **different submission contract**: the answer rides on the
login POST itself, not on `/account/captcha`.

## Trigger and risk model

The legacy OAuth-context login form (`/account/login?oauth=true&response_type=code`, marker
`login-input-username`) returns the challenge as a normal HTTP 200 JSON page model, not as a
`403 captcha_required`:

```json
{
  "hhcaptcha": {
    "isBot": true,
    "captchaState": "<448-char opaque token>",
    "captchaError": false
  },
  "loginError": null,
  "userType": "anonymous"
}
```

- A plain wrong password without a challenge returns `loginError: {code: "mismatch", …}` and
  `hhcaptcha.isBot: false`. In a fresh browser this took four wrong-password attempts before the
  challenge appeared.
- The risk signal is shared at the network level: after the browser had been challenged, a **fresh
  curl cookie jar from the same IP was flagged `isBot: true` on its very first attempt**. Do not
  assume a fresh session resets the per-IP risk.
- `captchaState` is **stable for the whole challenge session** (every response returns the same
  value until the challenge is cleared). Only `captchaKey` rotates.
- A correct answer clears the challenge for that login context; a completely separate later login
  can be challenged again.

## Picture mechanics (identical to standalone)

1. `POST https://hh.ru/captcha?lang=RU` (XHR headers, empty body) → `{"key":"<uuid>"}`.
2. `GET https://hh.ru/captcha/picture?key=<uuid>` → PNG, 250×90.

Both require `X-Xsrftoken: <_xsrf>` and `X-Requested-With: XMLHttpRequest`; the picture GET needs
no XSRF. See the [standalone protocol](captcha.md#standalone-pure-http-solve-protocol) for
detail.

### Key liveness caveats (verified)

- A captcha picture key is effectively **one-shot with a short TTL**. Once the browser `<img>`
  loads a key, re-fetching the same key commonly 404s; un-issued keys expire on their own and the
  DOM actively rotates the picture. The rendered frame can be captured reliably by recording the
  traffic with a full-content HAR (`agent-browser network har start --content all`) **at the
  moment the challenge loads**, then decoding the embedded base64 body of the `captcha/picture`
  GET.
- Rotation means the previous key becomes invalid: stop using it the moment a fresh `POST
  /captcha?lang=RU` has issued a new key.
- When automating the pure-HTTP path, issue the key, immediately fetch the picture, solve fast, and
  submit. Do not hold a picture across slow operations.

## Submission contract (the difference from the standalone)

Do **not** POST the answer to `/account/captcha` — that is the standalone challenge protocol and
does not clear the login challenge. Instead repeat the **login POST** with the original credentials
**plus** three extra fields:

```http
POST /account/login?backurl=<…>&oauth=true&response_type=code
Content-Type: multipart/form-data
Accept: application/json
X-Xsrftoken: <_xsrf>
X-Requested-With: XMLHttpRequest
X-hhtmFrom:
X-hhtmSource: account_login

_xsrf            = <_xsrf cookie value>
failUrl          = /account/login?backurl=<…>&oauth=true&response_type=code
accountType      = EMPLOYER
remember         = yes
username         = <email or phone>
password         = <password>
password         = <password>        (sent twice by the legacy form)
isBot            = false
captchaKey       = <current uuid from POST /captcha?lang=RU>
captchaText      = <answer to the picture>
captchaState     = <captchaState from the challenge response>
```

The browser also sends `loginTrustFlags` and `X-GIB-*` fingerprint headers, but they were **not
required**: the verified pure-HTTP run sent the fields above without them and succeeded.

Result interpretation (all HTTP 200 JSON):

| Response `hhcaptcha` | Meaning | Action |
| --- | --- | --- |
| `isBot: true, captchaError: false` | Challenge issued | solve |
| `isBot: true, captchaError: true` + new `captchaKey` | Wrong answer | issue fresh key, retry within attempt bound |
| `isBot: false, captchaError: null, captchaKey: null` | Challenge accepted | continue to OAuth |

Challenge acceptance and credential validity are still decoupled: the response may carry
`loginError: {code: "mismatch"}` even after a correct captcha. The answer is consumed by the
submission, so a wrong password after a successful solve means starting a fresh challenge on the
next attempt. The successful pure-HTTP run combined the correct answer **and** the correct
password in one login POST; `userType` switched to `applicant` and the `hhtoken` cookie was set.

## CAPTCHA shape

- Same HH text CAPTCHA as standalone: 250×90 PNG, Cyrillic, **lowercase**, and typically **two
  word-like tokens separated by a space**. The words are not necessarily real dictionary words —
  treat them as arbitrary letter sequences.
- In the DOM it is a modal over the password step (`data-qa="modal-overlay"` → `img[data-qa=
  "account-captcha-picture"]`, `input[data-qa="account-captcha-input"]` name `captchaText`,
  `[data-qa="captcha-renew-text"]`, submit is the modal-footer button).
- Wrong answers set `data-qa="account-captcha-error"` and the modal auto-issues a fresh key.

## Solver guidance

- Send the answer exactly as shown in the picture: lowercase, keep the space between the two
  tokens. A consensus of several independent vision reads over the exactly-rendered frame is
  noticeably more reliable than a single read, especially because the tokens look like (but are
  not) real words. Keep the attempt budget bounded and rotate to a fresh picture rather than
  re-reading a hard image.

## Minimal pure-HTTP playbook

```text
1. GET /account/login?backurl=/&oauth=true&response_type=code     → cookie jar + _xsrf
2. POST /account/login   (multipart, fields above, no captcha)    → 200: hhcaptcha.isBot=true + captchaState
3. POST /captcha?lang=RU (XHR headers, empty body)                → {"key":"<uuid>"}
4. GET /captcha/picture?key=<uuid>                                → PNG 250x90
5. solve the picture (vision consensus, lowercase, keep the space)
6. POST /account/login   (multipart + captchaKey/captchaText/captchaState, CORRECT password)
7. GET /oauth/authorize (with skip_choose_account=true&oauth=true&hhtmFrom=account_login)
                                                                    → 302 hhandroid://oauthresponse?code=…
8. POST /oauth/token     → Bearer token
9. GET api.hh.ru/me (Bearer)  → authenticated identity
```

Bound wrong-answer retries with the shared `service.hh.captcha_max_attempts` policy; on exhaustion
fall back to the human-interaction seam exactly as described in
[CAPTCHA](captcha.md#attempt-policy).
