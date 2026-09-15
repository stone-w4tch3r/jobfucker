# Known Unknowns

This page lists integration boundaries that are not established well enough to implement as facts.
It is not a backlog or research diary. A feature depending on one of these items requires focused
verification first.

## Token longevity

- Refresh-token single-use/rotation behavior has not been observed through a genuinely expired
  access token.
- The safe client assumption is to refresh only after expiry and atomically replace both returned
  tokens, never reusing the old refresh token.
- Token revocation/logout behavior and its interaction with website sessions has not been fully
  characterized.

## Mutating workflows

The following routes are known but their current request/response contracts are not verified
end-to-end:

- create, clone, edit, and publish resumes;
- send a follow-up negotiation message;
- decline/cancel a negotiation;
- hide/delete a chat through the website;
- blacklist a vacancy or employer;
- create a saved search;

Do not infer mutation payloads from read models. Verify each operation with a disposable/experiment
account or explicit user authorization and add the final contract to the relevant canonical page.

## Application limits

The `limit_exceeded` envelope is a supported stop signal, but the exact daily cap, reset boundary,
and whether it varies by account are not established here. Treat the server response as authority;
do not hard-code a guessed HH limit.

## Challenge variants

- Only HH's own text CAPTCHA has been observed in the current integration.
- reCAPTCHA/other markers are recognized by design but their solve protocols are not implemented.
  The standalone captcha page loads `recaptcha.net/api.js` and carries a `siteKey` in its payload;
  whether a reCAPTCHA token becomes mandatory under escalation is not established.
- Risk thresholds are dynamic. The observed number of reads before a challenge is not a stable
  limit. The gate acts as a quota window (~50–100 detail GETs after release; released by attempt
  exhaustion or a browser solve, without verifying answers) — see [CAPTCHA](captcha.md#trigger-and-pacing-implications).

## Captcha trust boundary (GIB)

The standalone answer POST is gated by a JS-issued browser-trust layer (GIB headers +
`*gib-w-hh` cookies from `/api/fl`), and `navigator.webdriver=true` alone causes blanket
`wrong_answer` rejections — see [CAPTCHA trust layer](captcha.md#5a-trust-layer-gib-fingerprint-gate).
Unresolved:

- Whether any cookie/header harvest restores pure-HTTP solving. Replayed GIB tokens fail under
  both plain and Chrome-impersonating curl (ddg interstitial + `anonymous` demotion); the X-GIB
  header values are JS-fresh per request, so harvest is unlikely to work without computing them.
- Whether the `/api/fl` fingerprint payload can be computed outside a browser at all.

## Website volatility

- Login DOM selectors and selected-control suffixes can change independently of the underlying
  HTTP login contract.
- The relationship between website role cookies and visual logged-in state is not a trustworthy
  auth contract.
- Resume-scoped website search requires an owner session today; silent fallback behavior may vary.

Automation must use the recognition rules in [Website behavior and DOM](website.md) and fail with
diagnostic evidence when neither known surface matches.

## Search identifiers and private parameters

- A complete district-ID directory source is not established.
- `/suggests/metro` did not work anonymously; use vacancy-detail metro station IDs until a stable
  lookup is verified.
- Positive behavior for an owned `saved_search_id` is not verified.
- Parameters prefixed `L_` are marked private/experimental by the website dictionary and are out of
  contract.

## Transport implementation choice

Chrome-impersonating curl profiles were somewhat more reliable in one matrix, but ordinary and
impersonating clients both experienced transient DDoS-Guard TLS drops. Fingerprint emulation is not
proven mandatory. Select an HTTP implementation based on measured reliability, cookie/XSRF
support, async integration, and maintainability; bounded retry remains required either way.

## Resolving an unknown

When verification is necessary:

1. Define the smallest claim and success/failure signals.
2. Use an experiment account for mutations or challenge provocation.
3. Keep secrets and raw captures outside version control.
4. Sanitize the durable request, response, DOM, and recovery contract.
5. Update the canonical wiki page and tests; remove the item from this page.
