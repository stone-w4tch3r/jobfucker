# CAPTCHA

Habr Career account login is gated by **Yandex SmartCaptcha** on the Habr Account
(`account.habr.com`) login step. This page records the challenge shape and the observed **trigger → solve → resume** flow. The
solving step was first performed by the user in a headed browser while the agent recorded traffic,
then reproduced **fully automatically** by a stealth headless browser (see
[Automation feasibility](#automation-feasibility-stealth-headless)). Keep a human fallback: the
challenge is risk-based and escalation is untested (see
[the research playbook](research-playbook.md#captcha-handling-rule)).

> Freshness: captured 2026-09-18 from a fresh unauthenticated browser session against
> `career.habr.com/users/auth/tmid`.

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
| Mode | Checkbox ("Я не робот") |
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

- Whether the SmartCaptcha is mandatory on every fresh login or only on risk-based sessions.
- Whether the checkbox ever auto-passes with no click (not observed; a click was always needed).
- Escalation: whether an image/advanced challenge appears under repetition, IP change, or fingerprint
  change, and whether Qrator blocks first.
- The token TTL and whether a token can be reused for a retry of the same login.
- Whether the same challenge ever appears on `career.habr.com` itself (e.g. on search/apply at
  volume) rather than only on the Habr Account login step.

## Handling rule

Follow the [CAPTCHA rule](research-playbook.md#captcha-handling-rule): stop, capture, then open the
challenge in a headed browser and hand solving to the user while observing the full flow. Do not
try to automate the SmartCaptcha until the trigger and completion contracts are documented.
