# CAPTCHA

Last validated: 2026-09-01 (browser-engine standalone solve on pipeline 2, headless, first-answer
clear; experiment account).

## Observed challenge

The current observed challenge is HH.ru's own text CAPTCHA: a 250×90 PNG, refresh button, text
input, and submit action. It has two distinct integration contexts:

- after repeated bad credential attempts, embedded in the login form;
- after a sustained burst of authenticated vacancy-detail reads, returned by the API as
  `403 captcha_required` with a standalone `captcha_url`.

No reCAPTCHA or Yandex SmartCaptcha challenge was observed, but detection must remain kind-agnostic
because the provider or challenge type can change.

## API signal

```json
{
  "errors": [
    {
      "type": "captcha_required",
      "value": "captcha_required",
      "captcha_url": "https://hh.ru/account/captcha?state=…"
    }
  ],
  "request_id": "…"
}
```

The transport classifier must inspect every response, not only apply calls. See
[Transport and errors](api/transport-and-errors.md#challenge-classifier).

## Embedded login challenge

The login variant is not the standalone challenge page. Observed markers:

- the login XHR returns HTTP 200 with `errors[].description.isBot=true` (or top-level
  `hhcaptcha.isBot=true`) and a stable `captchaState`;
- the CAPTCHA is presented as a modal over the password step (`data-qa="modal-overlay"`) containing
  `img[data-qa="account-captcha-picture"]` and `input[data-qa="account-captcha-input"]`;
- the form still contains `username`, `password`, `_xsrf`, `isBot`, and `captchaText`;
- its submit selector is `account-login-submit`, not `account-captcha-submit`;
- wrong answers set `captchaError=true`, clear the input, and issue a new picture key.

The browserless answer protocol is established: re-submit the **login form** with the original
credentials plus `captchaKey`, `captchaText`, and `captchaState` in one multipart POST. Do **not**
route it through the standalone `/account/captcha` protocol below — that standalone solve does not
clear a login-scoped challenge. Full signal shapes, key-liveness caveats, and the raw-HTTP playbook
are in [Login CAPTCHA](captcha-login.md).

## Standalone solve protocol (browser engine)

Standalone challenges are cleared by the browser engine
(`src/jobfucker/clients/hh/browser.py`): patchright stealth Chromium, headless,
opened for the challenge window only. The HTTP client keeps doing all other work.

1. The coordinator validates the `captcha_url` (same-origin
   `https://hh.ru/account/captcha`, exactly one `state` query parameter — the
   validator rejects any other origin, port, userinfo, or path) and snapshots
   the persistent website cookie jar into the browser context. The challenge is
   account-scoped, so the browser session must carry the same cookies.
2. An init script hides `navigator.webdriver` (belt and suspenders over
   patchright's own C-level automation-signal patches).
3. The engine opens the challenge URL, waits for
   `img[data-qa="account-captcha-picture"]` to actually load (`naturalWidth >
   0` — the element may render before its `src` resolves), and fetches the
   original PNG bytes through the browser context (same jar, same trust; a
   rendered screenshot is only the fallback).
4. The PNG goes to the board-neutral solver interaction
   (`PNG bytes → success(text) | failure(reason)`) — AI vision or terminal,
   unchanged and provider-agnostic. The solver never owns HTTP/session logic.
5. The answer is filled into `input[data-qa="account-captcha-input"]` and
   submitted via `[data-qa="account-captcha-submit"]`. The site's own page JS
   issues the GIB trust headers for the submission; the client never fabricates
   them (see §5a for why they cannot be copied).
6. Verdict (the page submits the answer as an XHR):
   - challenge POST response status `302` → challenge cleared (authoritative);
   - status `403` (`hhcaptcha.isBot` + `captchaError`) → rejected answer; the
     page issues a fresh picture key, so the next attempt reloads the state;
   - no POST observed (page drift) → fall back to the page signals:
     same-origin redirect whose query carries `hhtmFrom=account_captcha` →
     cleared; visible `data-qa="account-captcha-error"` marker → rejected;
     anything else (foreign-origin marker, timeout, page drift) → treated as
     not-cleared and retried within the attempt bound.
   The status listener exists because the old single URL/marker check raced the
   SPA navigation and the transient error marker (2026-09-10 incident).

There is no pure-HTTP submission for standalone challenges: §5a shows the
answer text is never evaluated without the browser-trust layer. On exhaustion
the typed error carries the sanitized `captcha_url` so the human fallback
(terminal rendering, printed URL) stays available.

### 5a. Trust layer (GIB fingerprint gate)

Validated 2026-09-01. The answer text is only evaluated for requests from a trusted browser
context; everything else is rejected as `wrong_answer` regardless of the answer.

Observed evidence, one experiment account, one day:

- The browser XHR additionally sends JS-issued headers `X-GIB-FGSSCgib-w-hh` (40 chars) and
  `X-GIB-GSSCgib-w-hh` (~288 chars), plus `Referer` (challenge URL), `X-hhtmSource:
  account_captcha`, and `Content-Type: application/x-www-form-urlencoded`.
- The browser jar holds JS-issued long-lived cookies the pure-HTTP client never receives:
  `fgsscgib-w-hh`, `gsscgib-w-hh`, `cfidsgib-w-hh`, `__zzatgib-w-hh` (365-day expiry) and
  `__ddg1_`. They are issued/refreshed by `GET /api/fl/idgib-w-hh` and
  `POST /api/fl?u=<uuid>&cfidsgib-w-hh=<payload>` (payload computed by obfuscated site JS).
- A real Chrome with `navigator.webdriver=true` (headed or headless automation), same cookies,
  correct answers (4/4 vision consensus, human-verified image) → always `403 wrong_answer`
  ("Неверный текст"), on fresh states and fresh keys.
- The same automated browser with a page-level `webdriver` spoof (init script overriding
  `Navigator.prototype.webdriver` and locale) → first correct answer clears the challenge with
  the `hhtmFrom=account_captcha` redirect.
- Replaying the captured browser POST via curl with the full harvested cookie jar + GIB headers
  → ddos-guard interstitial (`302`, no `Location`) and `hhrole` demoted to `anonymous`; a retry
  from the demoted jar is `403`. Cookie/header transfer alone did not restore trust.
- Same replay through Chrome-impersonating curl (`curl_cffi`, Chrome TLS/HTTP2 fingerprint) with
  the harvested jar + GIB headers → identical interstitial and demotion. TLS impersonation does
  not rescue stale tokens; the X-GIB header values are regenerated by site JS per request and do
  not survive capture-replay.

Client implication: pure-HTTP standalone solving is impossible — the gate
evaluates only a trusted browser context. Standalone recovery therefore runs
the stealth browser engine (§5): patchright (webdriver/CDP-patched Chromium)
opened headless for the challenge window. Validated live on the experiment
account (2026-09-01): the headless engine cleared the challenge with the first
correct answer (`hhtmFrom=account_captcha` redirect, ~15 s browser time), and
the plain-Bearer HTTP session was unlocked afterwards with no cookie copy.
Harvesting browser cookies back into the HTTP client remains dead — the
unlock is tied to the account/Bearer state, not the jar.

### 6. Retry original operation

Retry the exact blocked request once per successful solve, within a global solve/retry bound. No
API cookie copy is required: the observed unlock was tied to the account/Bearer state, and the
original request succeeded with the plain Bearer session after a separate website solve.

## Attempt policy

- Use `service.hh.captcha_max_attempts` (default 4, allowed 1–10) as the maximum number of fresh
  image answers for one challenge state.
- A wrong answer reloads the challenge state in the browser and takes a fresh picture; never
  resubmit the same picture answer.
- A successful solve permits one replay of the original request.
- If the replay produces a new CAPTCHA, count it against a workflow-level bound.
- Never automatically guess one-time codes or passwords as CAPTCHA answers.
- On exhaustion, return a typed error containing the sanitized challenge kind and `captcha_url`,
  and offer a human fallback.

## Detection versus solving

Keep these separate:

```text
transport/login classifier → Challenge(kind and context, url/state, evidence)
challenge coordinator → chooses supported solver/fallback and bounds attempts
standalone browser engine → real-page image extraction + real-page submission
embedded login protocol → browserless multipart credential-form replay
generic solver → PNG-to-text interaction
```

Known HH text markers are documented in [Website DOM](website.md#captcha-dom). reCAPTCHA markers
must be recognized as a different kind even if no automated solver exists. Unknown challenges
should preserve status, content type, final URL, and safe marker excerpts for diagnosis.

### Solver debug images

With DEBUG logging enabled, every PNG handed to a solver (standalone browser
path and embedded login path) is dumped as `captcha-<label>-<ns>.png` to
`LOGGING_CAPTCHA_IMAGES_PATH` when set, otherwise to
`<system temp>/jobfucker-captcha-images`. Off at normal log levels; the write
is best-effort and never breaks recovery. Dumped frames feed the offline
benchmarks in [captcha-benchmarking](../captcha-benchmarking/doc.md).

## Trigger and pacing implications

A read-heavy burst triggered CAPTCHA after dozens of vacancy-detail calls at roughly the legacy
0.345-second cadence. Apply bursts did not prove immune; they merely did not trigger in the sample.
Re-validated 2026-09-01: detail enrichment trips the challenge again after roughly 100 detail
GETs; the trigger point varies (1st, ~11th, ~95th detail GET across runs) — it is a rate/quota
gate, not a fixed counter. The gate behaves as a quota window: after the 4 client attempts are
exhausted OR the challenge is solved in a browser, the next run passes roughly 50–100 detail
GETs before the gate re-trips; the gate release does not verify answers. Therefore:

- pace every endpoint;
- bound enrichment concurrency instead of launching parallel detail requests;
- handle challenges globally;
- do not encode a trigger threshold as a rule—HH risk scoring is dynamic.
