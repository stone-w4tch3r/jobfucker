# CAPTCHA

Habr Career account login is gated by **Yandex SmartCaptcha** on the Habr Account
(`account.habr.com`) login step. This page records the challenge shape, the **complexity ladder**
(checkbox → image), and the observed **trigger → solve → resume** flow. The checkbox pass was first
solved by a human, then reproduced automatically by a stealth headless browser
([Automation feasibility](#automation-feasibility-stealth-headless)); the **advanced image challenge
was provoked and solved fully automatically, including over pure HTTP**
([Auto-solve feasibility](#auto-solve-feasibility-verified)). A human fallback is still advisable
because the challenge is risk-based and the block threshold is unknown
(see [the research playbook](research-playbook.md#captcha-handling-rule)).

> Freshness: captured 2026-09-18 from fresh unauthenticated browser sessions and browserless HTTP
> against `career.habr.com/users/auth/tmid` / `account.habr.com`.

## Where it appears

| Step | Surface |
| --- | --- |
| Entry | `GET https://career.habr.com/users/auth/tmid` |
| OAuth authorize | `GET https://account.habr.com/oauth/authorize/?response_type=code&client_id=career-<uuid>&redirect_uri=https://career.habr.com/users/auth/tmid/callback_oauth&state=bslogin&action=login` |
| Login page (challenge lives here) | `GET https://account.habr.com/ru/ident/<state-token>` |
| Login submit | `POST https://account.habr.com/ru/ident/in/<state-token>` |

The challenge is part of the **Habr Account login form**, not a separate interstitial. Whether a
valid Habr Account session (e.g. the `habr-exp` scope) bypasses it entirely is observed: yes, the
authenticated `habr-exp` session reaches `career.habr.com` without any career-side challenge. The
question is whether a **fresh login** always requires it.

## Engine

Yandex SmartCaptcha, checkbox widget.

| Fact | Value |
| --- | --- |
| Engine | Yandex SmartCaptcha |
| Mode | Checkbox ("Я не робот"), risk-based escalation to an image (distorted-text) task |
| Sitekey | `ysc1_zgWuDVpgrG9kwB8QEfIkuWseZyEnRzHLCAPF2dwh1db6e985` |
| Theme / lang / host | `theme=light`, `hl=ru`, `host=account.habr.com` |
| Loader script | `https://smartcaptcha.cloud.yandex.ru/captcha.js?render=onload&onload=yc_loaded` |
| Vendor script | `https://smartcaptcha.cloud.yandex.ru/vendors.<hash>.js` |
| Shield script | `https://smartcaptcha.cloud.yandex.ru/shield.<hash>.js` |
| Checkbox iframe | `https://smartcaptcha.cloud.yandex.ru/checkbox.ru.<hash>.html?sitekey=...&theme=light&hl=ru&host=account.habr.com&href=<page>&test=false&webview=false&hideChallengeContainer=false` |
| Backend iframe | `https://smartcaptcha.cloud.yandex.ru/backend.<hash>.html?...` (hidden) |
| Telemetry | `https://mc.yandex.ru/watch/<id>...`, `https://mc.yandex.ru/metrika/tag_phono.js` |

The `<state-token>` is a per-login opaque path segment; the SmartCaptcha iframe URLs embed the full
login page URL in `href`/`page-url`.

## DOM contract (login page)

| Element | Selector / attributes | Notes |
| --- | --- | --- |
| Widget container | `div.smart-captcha[data-captcha="yandex"][data-sitekey="..."]` | `data-testid="smartCaptcha-container"`, inline `height: 120px` |
| Checkbox iframe | `iframe[title="SmartCaptcha checkbox"]` | Contains the `checkbox "Я не робот"` |
| Backend iframe | `iframe[title="SmartCaptcha backend"]` | Hidden, `display:none` parent |
| Spinner overlay | `.SmartCaptcha-Overlay.SmartCaptcha-Overlay_show_spinner` | Shown while the widget initializes |
| Token field | `input[name="smart-token"][type="hidden"]` | Populated by the widget after a pass |
| Fallback | `.js-captcha-fallback` (`hidden` when the widget loads) | Text: "Анти-робот проверка не загрузилась. Без неё продолжить не получится." |
| Help modal | `#captcha-help-modal` with `.js-captcha-help-link` | User-facing help |

Observed state at capture time: checkbox `checked=false`, `input[name=smart-token]` empty, overlay
spinner still present after 11 s. The widget rendered but did not self-pass in the agent browser.

## Login form contract

| Field | Type | Value |
| --- | --- | --- |
| `email` | email | account email |
| `password` | password | account password |
| `smart-token` | hidden | SmartCaptcha token (empty until solved) |

Form action `POST /ru/ident/in/<state-token>`. No `authenticity_token` field on this form (it is a
Habr Account form, not the Rails career app).

## Solve flow (observed 2026-09-18)

A human solved the checkbox in the headed browser while the agent recorded traffic. Sequence:

1. User clicks the "Я не робот" checkbox inside `checkbox.ru.<hash>.html`.
2. Telemetry pings fire to `https://mc.yandex.ru/watch/<id>...` (including a click
   `pointer-click` ping and a `checkbox.submit` marker).
3. The widget issues:

   ```text
   POST https://smartcaptcha.cloud.yandex.ru/check
        ?host=account.habr.com&sitekey=<sitekey>&href=<login-page-url>
   ```

   Response: HTTP 200, `{"status":"ok","spravka":"<token>"}`. The `spravka` is a ~400-char
   base64-ish string whose decoded head looks like `t=<unix-ts>;i=<ip>;D=<digest>...`.
4. The widget writes `spravka` into the hidden field `input[name="smart-token"]`
   (observed length 400).
5. On form submit, `POST /ru/ident/in/<state-token>` is issued as an **XHR** and returns HTTP 200
   with an empty body; the page then navigates client-side.

The captcha token is single-use, bound to the login page and sitekey, and embeds the client IP and
a timestamp. It must be used in the same form submission that produced it.

## Challenge ladder (how complexity rises)

SmartCaptcha is risk-based: the same `/check` endpoint returns either a pass token or a challenge
whose **type** depends on how trustworthy the request looks. Observed levels, cheapest to hardest:

| Level | `captcha.type` | Trigger observed | What the client must do |
| --- | --- | --- | --- |
| 0 — pass | *(absent, `status:"ok"`)* | Trusted browser: normal desktop Chrome UA, coherent fingerprint, real click | Nothing; `spravka` written to `smart-token` |
| 1 — checkbox | `checkbox` | A `/check` POST without browser telemetry (bare HTTP), or an unverifiable checkbox attempt | Produce the widget's interaction proof (canvas `picasso` + encrypted `rdata`) or escalate to image |
| 2 — image | `image` | Detectable browser (`HeadlessChrome` UA, `navigator.webdriver`), or a failed/unverifiable checkbox attempt | OCR the text image, solve `pow`, POST the answer |

Escalation is server-driven and monotonic per attempt: posting a `checkbox` challenge key with a
solved `pow` but **no answer** made the server return a fresh `type:"image"` challenge. There is no
observed "harder than image" level, and `pow.complexity` stayed `10` across every challenge observed.

Consequences for a client:

- A browserless caller does not need to fake the checkbox. It can walk the ladder deliberately:
  minimal `/check` → `checkbox` key → `/check` with `key` + `pow` (no answer) → `image` challenge.
- A detectable browser gets the image challenge **immediately**, so a bad fingerprint costs an OCR
  round instead of a silent pass.

## Advanced (image) challenge contract

When `captcha.type == "image"` the response carries a single distorted-text image (Cyrillic words,
curved/noisy), not an object-selection grid. A second task type exists: the `voice`/`voiceintro`
URLs and the widget's "Изменить тип задания" button switch the challenge to audio.

Response shape (sanitized):

```json
{
  "status": "failed",
  "unique_key": "<digits>",
  "captcha": {
    "type": "image",
    "key": "<opaque ~380 chars>",
    "image": "https://smartcaptcha.yandexcloud.net/load-captchaimg?<b64url>",
    "voice": "https://smartcaptcha.yandexcloud.net/load-captcha-voice?<b64url>",
    "voiceintro": "https://smartcaptcha.yandexcloud.net/load-captcha-voiceintro?<b64url>",
    "d": "",
    "k": ""
  },
  "pow": { "prefix": "<hex>", "complexity": 10 }
}
```

- `captcha.image` is a **loader**: base64url-decode the query segment (split on `,`) to get the real
  URL `https://img.smartcaptcha.yandexcloud.net/image?key=<...>`. The image fetched from that URL
  with no cookies and no browser returned `200 image/jpeg` and was solvable, so the image is bound
  to IP + challenge lifetime, not to a cookie jar.
- A wrong answer returns `status:"failed"` with a **new** `captcha.key` and a new image; the
  challenge refreshes rather than locking. No lockout was reached in ~4 wrong submissions.

Advanced-challenge DOM (inside `iframe[title="SmartCaptcha advanced"]`,
`advanced.ru.<hash>.html`):

| Element | Selector / text | Notes |
| --- | --- | --- |
| Frame | `iframe[title="SmartCaptcha advanced"]` | Appears alongside the checkbox frame |
| Answer input | textbox `"Введите текст с картинки"` | Placeholder `"Строчные или прописные буквы"` — answer is case-insensitive |
| Submit | button `"Отправить"` | |
| Refresh | button `"Обновить задание"` | Issues a new challenge |
| Switch to audio | button `"Изменить тип задания"` | Audio alternative |
| Dismiss | button `"закрыть"` | |
| Footer | `unique_key` + unix timestamp | Telemetry/debug |

## Proof-of-work (`pow`)

Every challenge (and every answer submission) carries a `pow` object: `{prefix, complexity}`.
Observed `complexity` = `10`; `prefix` is the hex encoding of an ASCII string
`t=<unix-ts>;p=<uuid>;c=<complexity>;d=<32-hex>;`.

The client must find a 16-byte `nonce` such that:

```text
sha256( bytes.fromhex(prefix) ++ nonce )  has >= complexity leading zero bits
```

The nonce is transmitted as a hex string. Verified against a captured browser submission:
`sha256(prefix_ascii ++ nonce_bytes)` had exactly 10 leading zero bits. Cost at `complexity:10` is
~2^10 hashes (single-digit milliseconds); `powCalcTime` in the observed browser submission was
31 ms.

The answer submission carries the solution in a `pdata` form field: base64url of
`{"powNonce": "<hex16>", "powCalcTime": <ms>, "powPrefix": "<hex>"}`.

## Detection and fail-fast

Signals a client can check without parsing the widget's internals:

| Signal | Pass | Escalated |
| --- | --- | --- |
| `/check` response | `status:"ok"` + `spravka` | `status:"failed"` + `captcha` object |
| `captcha.type` | absent | `"checkbox"` or `"image"` |
| Hidden field | `input[name=smart-token]` non-empty | still empty |
| DOM | no advanced frame | `iframe[title="SmartCaptcha advanced"]` present |

Fail-fast rule: after a checkbox attempt, if no `smart-token` appears within a short timeout **or**
`/check` returns `status != "ok"`, treat it as escalation. Do not retry the same challenge in a
loop; either run the documented image-solve path or hand off to a human. A wrong image answer
returns a *new* challenge, so a bounded retry (fresh challenge each time) is safe, but unbounded
hammering risks an unknown block.

## Auto-solve feasibility (verified)

The advanced image challenge was solved **fully automatically over pure HTTP** on 2026-09-18:

1. `GET https://career.habr.com/users/auth/tmid` (redirects to the login page; no captcha on GET) to
   obtain the login-page URL used as `href`.
2. `POST /check?host=account.habr.com&sitekey=<sitekey>&href=<login-url>` with only
   `sitekey, lang=ru, test=false, webview=false` → `captcha.type:"checkbox"` + `pow`.
3. Re-POST `/check` with `key` + solved `pdata` and **no answer** → `captcha.type:"image"` + image
   URL + a fresh `pow`.
4. Download `captcha.image`, OCR the text with a vision model.
5. `POST /check` with `key`, `rep=<ocr text>`, `pdata`, `sitekey, lang, test, webview` →
   `{"status":"ok","spravka":"..."}`.
6. `POST account.habr.com/ru/ident/in/<state>` with `email`, `password`, `smart-token=<spravka>` →
   `{"success":true,"rurl":"https://account.habr.com/oauth/authorize/done/<hash>"}`; follow `rurl`
   with the same cookie jar.

Result: the full login completed **browserlessly**, no browser in any step; the jar authenticated
`GET /api/frontend_v1/users/me` (alias returned). The image challenge is solvable without the
widget's `rdata` (encrypted telemetry), `picasso` (canvas proof), or `tdata` (pointer/keyboard
telemetry) — those fields are present in a real browser submission but were not required by the
server for a minimal `key + rep + pdata` POST.

Captcha presence: on every fresh credential login observed, the login page carried the captcha
placeholder and the server rejected a token-less POST with
`{"success":false,"errors":{"smart-token":"Необходимо пройти капчу"}}`. A fresh login therefore
always costs a captcha round (checkbox type for a bare client, image type for a detectable one).
An existing Habr Account session skips the login form and the captcha entirely.

Notes on the solve path:

- **OCR quality is the limiting factor, not the protocol.** The vision model read the distorted text
  on several attempts and misread others; a wrong answer just yields a fresh challenge, so a bounded
  retry converges.
- **The audio task type** (`voice`, `voiceintro`) is the alternate challenge; it would need
  speech-to-text instead of OCR. Not exercised.
- **Do not over-read the result:** the block threshold, any per-IP rate limit, and whether
  `complexity` ever rises are unknown. Keep volume low and treat repeated failures as a stop signal.

## Automation feasibility (stealth headless)

Tested 2026-09-18 with the agent-browser wrapper (CloakBrowser patched Chromium, coherent
fingerprint; `navigator.webdriver` not true), fresh transient **headless** session, no human input.

Method: open `career.habr.com/users/auth/tmid`, then programmatically click the checkbox by its
snapshot ref (agent-browser inlines the cross-origin iframe). No image/advanced challenge appeared.

| Attempt | Checkbox click | `smart-token` | Full login |
| --- | --- | --- | --- |
| 1 | automated | populated (len 400) | Yes — callback chain ran, `/api/frontend_v1/users/me` → 200 |
| 2 | automated | populated (len 396) | not repeated |

Click mechanics observed: pointer-click telemetry to `mc.yandex.ru` → `POST
smartcaptcha.cloud.yandex.ru/check?host=...&sitekey=...&href=...` → `{"status":"ok","spravka":"..."}`
→ `spravka` written to `input[name=smart-token]` → login POST → callback → career session. In one
attempt the widget also issued an autonomous background `POST /check` on page load before any click.

Conclusion: on this account and browser, an automated **click-only** pass is sufficient in 2/2
attempts, and a fully automated headless login worked end-to-end. This does not imply a browserless
(pure-HTTP) solve exists.

Caveats — do not over-read this result:

- SmartCaptcha is **risk-based**. Two successes do not establish that a challenge will never
  escalate to an image/advanced challenge on repeated or faster logins, a different IP, or a
  different fingerprint.
- Only checkbox mode was seen; escalation behavior (image puzzle, blocked) is untested.
- Qrator is a second, independent gate.
- Use sparingly: low-frequency logins on an owned account only. Treat `status != "ok"` or a missing
  token as escalation and hand off to a human.

## Engine comparison: patchright vs CloakBrowser

jobfucker drives browsers with **patchright** (`clients/hh/browser.py`, `PatchrightDriver`). Tested
2026-09-18 whether patchright's bundled headless Chromium is enough for the Habr login SmartCaptcha,
or whether CloakBrowser is required, using one probe script (`/tmp/kilo/habr_captcha_probe.py`,
patchright async API, same `locale="ru-RU"` + `navigator.webdriver` spoof as jobfucker):

| Engine / mode | UA | plugins | `window.chrome` | `/check` | `smart-token` | login |
| --- | --- | --- | --- | --- | --- | --- |
| patchright bundled 151, old headless | `HeadlessChrome/151` | 0 | false | 200, not ok | none | **no** |
| patchright bundled 151, old headless + UA override | `Chrome/151` | 0 | false | ok | 400/396 | **yes (3/3)** |
| patchright bundled 151, headed | `Chrome/151` | 5 | true | ok | 396 | yes |
| patchright bundled 151, `--headless=new` | `HeadlessChrome/151` | 5 | true | **`failed`, `captcha.type=image`** | none | **no** |
| patchright bundled 151, `--headless=new` + UA override | `Chrome/151` | 5 | true | ok | 396 | yes |
| patchright + CloakBrowser 146, headless | `Chrome/146 (Windows)` | 5 | true | ok | 396 | yes (2/2) |

Conclusions:

- **CloakBrowser is not required.** The decisive tell is the `HeadlessChrome` user-agent string.
  Overriding it to a normal desktop Chrome UA makes patchright's own bundled headless Chromium pass
  (3/3 old headless; 1/1 `--headless=new`), even though old headless still reports 0 plugins and no
  `window.chrome`. SmartCaptcha here does not gate on plugins or `window.chrome`.
- **A wrong UA is not rejected — it escalates to an image challenge.** `--headless=new` with the
  default UA returned `/check` body
  `{"status":"failed","unique_key":"…","captcha":{"type":"image","key":"…"}}`. Detecting headless
  switches the widget from click-only to an image puzzle.
- `navigator.webdriver` was `false` in every run; patchright's JS patches hold. The failure is purely
  the UA/fingerprint surface.
- CloakBrowser (patched binary, coherent Windows fingerprint) also passes without a UA override and
  is the more robust option, but it is optional for this challenge.

Minimal recipe for headless patchright (no CloakBrowser):

```python
browser = await p.chromium.launch(headless=True)  # or args=["--headless=new"]
context = await browser.new_context(
    locale="ru-RU",
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
)
```

If CloakBrowser is preferred for robustness, patchright can drive it through the same API — the
`PatchrightDriver`/`BrowserDriver` seam already allows an engine selection (binary path + args):

```python
browser = await p.chromium.launch(
    headless=True,
    executable_path="<cloakbrowser binary>",   # cloakbrowser.download.ensure_binary()
    args=["--no-sandbox", "--fingerprint=<seed>", "--fingerprint-platform=windows"],
)
```

Gotcha that caused a false negative in the first probe run: the widget's
`.SmartCaptcha-Overlay_show_spinner` overlay must clear before clicking the checkbox. Clicking while
the spinner is present does not produce a token. Wait for the overlay to disappear, then click.

Escalation handling: if `/check` returns `status != "ok"` (e.g. an image captcha), do not retry in a
loop. Treat it as escalation and hand off per the playbook; a UA/fingerprint mismatch is the likely
cause.

## What is not documented yet

- Whether the captcha can ever be skipped on a fresh login for a very trusted client; every observed
  fresh credential login required it (server-enforced).
- Whether the checkbox ever auto-passes with no click (not observed; a click was always needed).
- The block/escalation threshold: how many failed attempts (per IP, per account) trigger a block or
  a harder challenge, and whether Qrator blocks first.
- Whether `pow.complexity` ever rises above `10`, and whether the audio task type is OCR-equivalent
  in difficulty.
- The token TTL and whether a token can be reused for a retry of the same login.
- Whether the same challenge ever appears on `career.habr.com` itself (e.g. on search/apply at
  volume) rather than only on the Habr Account login step.

## Handling rule

Follow the [CAPTCHA rule](research-playbook.md#captcha-handling-rule): stop, capture, then either
run the documented solve path or open the challenge in a headed browser and hand solving to the user
while observing the full flow. The escalation trigger and completion contracts are now documented
(checkbox → image, `pow`, minimal HTTP solve), so a bounded automated solve is allowed on the
experiment account at low volume; treat repeated failures or a new `captcha.type` as a stop signal
and hand off to a human.

## Recommendation: captcha strategy for the client (not final)

> Status: **recommendation, not a decision.** This is the proposed approach for the future
> `jobfucker.clients.habr` package, derived from the observations above. It is not implemented and
> not a spec; revisit it when the client is built (Session 4).

Proposed strategy: **browser-first checkbox, vision fallback, session reuse as the main defense.**

1. **Reuse the session first.** An existing Habr Account session skips the login form and captcha
   entirely ([Authentication](authentication.md#browserless-login-verified)). Persist cookies
   (`_career_session`, `remember_user_token`, `.habr.com` SSO) and only touch the captcha on a fresh
   login. This is the cheapest and most reliable option and should be the normal path.
2. **Primary solver — browser click.** Drive patchright headless with a normal desktop Chrome UA and
   click the checkbox; jobfucker already owns this browser stack
   ([`PatchrightDriver`](../../src/jobfucker/clients/hh/browser.py)). No AI cost, fastest when it
   works. CloakBrowser is an optional robustness upgrade, not required.
3. **Detect escalation, don't guess.** Treat a non-`ok` `/check`, an empty `smart-token` past a short
   timeout, or a present `iframe[title="SmartCaptcha advanced"]` as escalation
   ([Detection and fail-fast](#detection-and-fail-fast)).
4. **Fallback solver — browserless vision.** On escalation (or when the iframe click breaks), walk
   the ladder over HTTP, OCR the image through the existing vision boundary, solve `pow`, POST the
   answer ([Auto-solve feasibility](#auto-solve-feasibility-verified)). Bounded retries: each wrong
   answer yields a fresh challenge; stop on repeated failure.
5. **Human handoff last.** Unknown `captcha.type`, rising `pow.complexity`, repeated failures, or a
   Qrator interstitial go to the headed-browser pair protocol
   ([CAPTCHA rule](research-playbook.md#captcha-handling-rule)).

Why hybrid rather than either alone:

| | Browser checkbox | Browserless vision |
| --- | --- | --- |
| Cost | browser only, no AI | one vision call per attempt |
| Failure mode | bad fingerprint silently escalates to image | OCR misread → retry |
| Fragility | fingerprint/version drift, spinner race, iframe click | undocumented `/check` ladder, image legibility |

- Browser-only is insufficient: under a flagged IP or drifted fingerprint it silently becomes the
  image case with no solver.
- Vision-only is not preferred: OCR is the reliability bottleneck, adds a paid call per login, and
  relies on the server tolerating a minimal `rdata`/`picasso`-less POST.
- The two compose: the browser path's failure mode is exactly the fallback path's input.

Open risks for the eventual implementation (all unresolved, see
[What is not documented yet](#what-is-not-documented-yet)): block/rate thresholds, whether
`pow.complexity` rises, image legibility drift, the audio task type (would need STT), and Qrator as
a separate gate.
