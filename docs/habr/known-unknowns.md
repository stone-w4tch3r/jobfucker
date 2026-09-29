# Known Unknowns

Integration boundaries that are not established well enough to implement as facts. **Single home for
open questions** — canonical pages carry only verified behavior and link here. Not a backlog or
research diary. A feature depending on one of these items requires focused verification first, then
the item moves to a canonical page and is removed from here.

## Authentication and session

Mapped (see [Authentication](authentication.md)): unauthenticated redirect, OAuth authorize URL and
parameters, login form fields and POST target, the JSON `success`/`rurl` login response, the
post-login callback chain, the cookie inventory (`_career_session`, `remember_user_token`,
`.habr.com` `s<hex>` SSO cookies), session refresh via `remember_user_token`, the logout contract
(`POST /users/sign_out`, form-field and `X-CSRF-Token` carriers), and CSRF enforcement. The **full
login was completed browserlessly over pure HTTP**, including the captcha, and the captcha is
enforced on every fresh credential login. Still unknown:

- Session expiry durations and the exact TTL of `_career_session` / `remember_user_token`.
- Purpose of `meta.logoutToken` (not used by the HTML logout form); whether any endpoint consumes it.
- Whether mutating requests ever return a distinguishable expired-session signal instead of a plain
  redirect / `{}` identity.
- Whether a captcha can ever be skipped on a fresh login for a very trusted client (never observed).
- Anonymous request behavior at volume (no challenge observed in single requests).

## Search and listings

Mapped (see [Search and listings](api/search.md)): the JSON endpoint, query/filter params, sorting,
paging, page-size cap, accessible-position cap, `type=suitable` semantics, and RSS limits. Still
unknown:

- The accepted value shape of `company_ids[]` and the specialization filter (`divisions` / `s`);
  both were attempted and did not filter as expected.
- Archived/hidden vacancies: whether they appear in listings and how they count toward totals.
- The accessible-position cap is bounded by `meta.totalPages` (observed ~950 for an 1118-item pool);
  whether it varies per query or account is untested. Declare `max_search_items = 1000`
  conservatively.
- Whether `type=suitable` requires an owned resume and how the board selects it.
- Rate/captcha behavior on the listing endpoint at fetch volume.

## Vacancy detail

Mapped (see [Response models](api/response-models.md#detail-page)): the inline `"vacancy"` JSON with
the full `description`, and the fallback `JobPosting` JSON-LD. Still unknown:

- Archived / closed vacancy representation and the corresponding detail response.
- External-application ("отклик на другом сайте") representation at detail level.
- Screening-test or questionnaire presence flag, if any.
- Company size / salary semantics beyond the stated-salary fields.

## Resumes

Mapped (see [Response models](api/response-models.md#owned-resume)): the owned resume is the profile
page `/profile` (public `/<alias>`); no numeric resume id was found; `/api/frontend_v1/resumes` is
the public specialist directory. **One account = one resume** (single profile-resume); the apply
request carries no resume id and the observable identifier is the alias. Still unknown:

- Resume `updated_at` and any structured resume endpoint not yet found.

## Apply and responses

Mapped (see [Applications and responses](applications-and-responses.md)): the apply endpoint
(`POST /api/frontend/vacancies/<id>/responses`, multipart, optional `body` letter), letter
attach/edit (`PATCH …/responses/<rid>`), withdrawal (`DELETE …/responses/<rid>`), the full
`ApplyResult` map (applied / duplicate / anonymous / throttle / not-found / CSRF), the response
object, the global ~10 s minimum interval between responses, and the seeker reconciliation surfaces
(`/responses` HTML + `/api/frontend_v1/chat/conversations`) and the **150 responses/month** cap (not
daily; per account, not per IP; deletes still count). Still unknown:

- Monthly-cap reset boundary (calendar month vs rolling window) and reset time/timezone — **accepted
  as non-blocking**: a rare edge case, handled by a client tracking its own creations and pacing away
  from the cap rather than relying on the reset.
- External / "отклик на другом сайте" applications and their `response.kind`.
- Archival / closed-vacancy apply signal (no archived vacancy provokable from listings).
- Screening tests / questionnaires at apply, if any.
- Full `response.kind` enum and the meaning of `isQuick`, `result`,
  `vacancyRecommendationAccuracyPercent`.
- Response status vocabulary beyond `Не прочитано`, which needs employer-side action.
- `/conversations` item shape (no dialog on the experiment account).
- Whether the apply UI ever offers a cover-letter modal before creating the response (the observed
  flow POSTs the quick response first and edits the letter afterwards).

## Challenges and infrastructure

- Yandex SmartCaptcha on the Habr Account login step is captured end-to-end: checkbox pass,
  risk-based escalation to an **image (distorted-text) challenge**, the `pow` proof-of-work, and a
  verified **pure-HTTP solve** (OCR + `pow` → `spravka`, accepted by the login form) — see
  [CAPTCHA](captcha.md#challenge-ladder-how-complexity-rises). Still unknown: the block/rate
  threshold for repeated failures, whether `pow.complexity` ever rises above `10`, whether the audio
  task type is as solvable, whether the checkbox ever auto-passes with no click, the `spravka` TTL and
  whether it can be reused for a retry, and whether a challenge ever appears on `career.habr.com`
  itself (search/apply at volume) rather than only on the Habr Account login step.
  Follow the [CAPTCHA rule](research-playbook.md#captcha-handling-rule).
- Qrator WAF behavior under load: whether it issues an interstitial or cookie-refresh challenge, and
  its trigger.
- Rate-limit behavior and retry headers: a `429` was observed once under a request burst
  (2026-09-29) with no `Retry-After`; the trigger threshold and any retry header are unestablished
  (see [Transport and errors](api/transport-and-errors.md)).
- Error taxonomy: core shapes are mapped in
  [Transport and errors](api/transport-and-errors.md), but `5xx` bodies, any `429`/`Retry-After`, and
  the Qrator block page are unestablished. The `{"httpCode":…,"errorCode":…}` envelope is verified
  only under `/api/frontend_v1/responses*`; which other controllers use it is unmapped.

## Resolving an unknown

1. Define the smallest claim and success/failure signals.
2. Use the `habr-exp` experiment account for mutations or challenge provocation.
3. Keep secrets and raw captures outside version control (`/tmp`), then sanitize.
4. Update the canonical wiki page and tests; remove the item from this page.
