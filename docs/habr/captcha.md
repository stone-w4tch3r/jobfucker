# CAPTCHA

Habr Career account login is gated by **Yandex SmartCaptcha** on the Habr Account
(`account.habr.com`) login step. This page records the challenge shape and the observed **trigger → solve → resume** flow. The
solving step was performed by the user in a headed browser while the agent recorded traffic. The
flow is documented but not yet automated: CAPTCHA handling remains human-in-the-loop until a
reliable trigger model and a programmatic solve exist (see
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

## What is not documented yet

- Whether the SmartCaptcha is mandatory on every fresh login or only on risk-based sessions.
- Whether the checkbox can auto-pass without interaction (the agent browser never got a token;
  the human click always did).
- Whether an invisible/advanced challenge appears under risk escalation.
- The token TTL and whether a token can be reused for a retry of the same login.
- Whether the same challenge ever appears on `career.habr.com` itself (e.g. on search/apply at
  volume) rather than only on the Habr Account login step.

## Handling rule

Follow the [CAPTCHA rule](research-playbook.md#captcha-handling-rule): stop, capture, then open the
challenge in a headed browser and hand solving to the user while observing the full flow. Do not
try to automate the SmartCaptcha until the trigger and completion contracts are documented.
