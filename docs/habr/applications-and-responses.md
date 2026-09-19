# Applications and Responses

The mutation surface: how a Habr Career user responds to a vacancy, how the cover letter is attached
or edited, how responses are withdrawn, and what can be read back. All facts here were observed
end-to-end with the `habr-exp` account against live `career.habr.com`.

> Freshness: probes run 2026-09-19 over pure HTTP from a logged-in browser session and an anonymous
> `curl` jar. The account used was the experiment account; ~150 real responses were created (the
> whole monthly quota) and one deleted, which both proved the apply contract and drained the monthly
> cap. Values (csrf tokens, response ids) drift per session; use the shapes.

## Apply-mode signal

The listing item and the detail inline `vacancy` object both carry `response.kind`:

| `response.kind` | Meaning |
| --- | --- |
| `direct` | not yet responded; "Откликнуться" available |
| `applied` | the account already has a response to this vacancy |

Observed by scanning ~670 listing items across several queries; only `direct` and `applied` appeared.
The listing item also carries `quickResponseHref: "/api/frontend/quick_responses"`; that path itself
answers `404 {"error":"Not found"}` when called directly, so it is a UI hint, not the apply endpoint.

## Apply request

```http
POST https://career.habr.com/api/frontend/vacancies/<vacancy_id>/responses
X-CSRF-Token: <csrf>
Accept: application/json
Content-Type: multipart/form-data; boundary=<boundary>

--<boundary>
Content-Disposition: form-data; name="body"

<cover letter text, optional>
--<boundary>--
```

- **Method/endpoint:** `POST /api/frontend/vacancies/<id>/responses` (verified).
- **Body is `multipart/form-data`.** A cover letter is sent as the field `body`; an empty body
  (`--boundary--`, no fields) applies without a letter. Both were verified.
  - A one-shot apply **with** a letter in the same POST was verified and returns the letter in the
    response, so no separate edit call is required.
- **CSRF:** required. The `X-CSRF-Token` header was used by the site and verified; the
  `authenticity_token` form field is also accepted (verified). Missing token → `422`.
- **Resume:** no resume parameter was observed and there is no resume picker in the UI. The account's
  single profile resume is used implicitly. The `resume_id` question (alias vs an undocumented id)
  stays open because the request never carries one.
- No `Accept: text/html` variant and no `Referer` requirement were observed.

### Attach / edit the cover letter after applying

```http
PATCH https://career.habr.com/api/frontend/vacancies/<vacancy_id>/responses/<response_id>
X-CSRF-Token: <csrf>
Content-Type: multipart/form-data

fields: body=<letter text>   # optional extras observed: user[salary], user[currency]
```

- Verified: `PATCH …/responses/<id>` with `body` returns `200 {response:{…}}` and populates
  `message` / `messageHtml`.
- The site sends `user[salary]` / `user[currency]` alongside `body` from the same modal; those are
  profile-salary inputs, not required for the letter.
- The "Дополнить отклик" / "Редактировать" flow opens the editor; the PATCH fires on save.

### Withdraw a response

```http
DELETE https://career.habr.com/api/frontend/vacancies/<vacancy_id>/responses/<response_id>
X-CSRF-Token: <csrf>
```

- Verified: `200 {"status":"success"}`.
- A withdrawn response disappears from `/responses` and appears under `/responses/deleted`.
- Re-applying after a delete was verified to succeed and creates a **new** response id.
- `response.canDelete` / `response.canUpdate` in the payload signal whether the actions are allowed.

## Response object

`POST` and `PATCH` both answer `{"response":{…}}` (shape sanitized):

```json
{
  "response": {
    "id": 5006020,
    "email": null,
    "message": "<cover letter plain text or null>",
    "messageHtml": "<p>…</p> or null",
    "publishedAt": {"title": "19 сентября 2026 в 08:14", "date": "2026-09-19T08:14:40+03:00"},
    "isQuick": false,
    "vacancyRecommendationAccuracyPercent": null,
    "result": null,
    "canUpdate": true,
    "canDelete": true,
    "author": { "id": "<alias>", "title": "<full name>", "...": "resume snapshot" }
  }
}
```

- `message` / `messageHtml` are the cover letter in plain / HTML form (`null` when none).
- `isQuick` was `false` for both empty and lettered responses; its enum was not fully established.
- `result` was `null` in every observed response; meaning unknown.
- `author` is the account's resume snapshot (skills, experience, contacts, …), the same data the
  employer sees.

## ApplyResult mapping (verified)

| Case | HTTP | Body | Client outcome |
| --- | --- | --- | --- |
| Applied (with or without letter) | 200 | `{"response":{"id":…,…}}` | `Applied(response.id)` |
| Already responded | 401 | `{"error":{"message":"Вы уже откликнулись на эту вакансию"}}` | duplicate / skipped |
| Not authenticated | 401 | `{"error":"Войдите, прежде чем продолжить."}` | `AuthError` (re-authorize) |
| Rate limit (<10 s since last response) | 400 | `{"message":"Вы не можете откликаться чаще, чем раз в 10 секунд"}` | throttle; wait, retry once |
| Monthly cap reached | 400 | `{"message":"Можно оставлять не более 150 откликов в месяц"}` | `LimitExceeded`; stop the run |
| Vacancy not found | 404 | `{"status":404,"error":"Not Found"}` | `NotFoundError` |
| Missing/stale CSRF | 422 | `{"status":422,"error":"Unprocessable Entity"}` | config error; refresh token once |

Notes:

- The two `401` bodies have **different shapes** — `error` is a string for anonymous and an object
  (`{message}`) for the duplicate case. Parse both defensively; do not rely on status alone.
- The `400` throttle and the `400` monthly cap have the **same shape** (`{"message":"…"}`); the
  discriminator is the message text. A client enforcing its own counter can avoid relying on it.
- Neither `400` carries `Retry-After` / `X-RateLimit-*` headers.

## Limits and pacing

- **Minimum interval: ~10 s between responses, enforced per account and globally** (two different
  vacancies posted back-to-back produced the `400` throttle). The observed message states the limit.
- **Monthly cap: 150 responses per month, per account.** Reaching it answers
  `400 {"message":"Можно оставлять не более 150 откликов в месяц"}`. Verified by draining the
  experiment account to exactly 150 responses created in a day; the next response was refused.
  - **Deleted responses still count.** At the cap the account had `Основные (149)` +
    `Удалённые (1)` = 150 creations, so withdrawing a response does **not** free quota.
  - The cabinet exposes no remaining-count; the board only refuses at the boundary.
  - Whether the counter resets on a calendar month (Moscow time) or a rolling window is **not
    established** — the message only says "в месяц". A client must track its own creations and pace
    the month.
- **Contract mapping:** this board caps **per month**, not per day. For
  `service_info.per_auth_daily_cap`, treat the effective ceiling as `<150 / days-in-month>` (or track
  monthly directly); do not expose a naive 150/day. See the operation-routing note in
  [Platform map](../platform-map.md#operation-routing-candidate-surface-per-contract-method).
- Deletes are not throttled by the same interval in the observed flow (delete → immediate re-apply
  succeeded, while quota was still available).

## Reconciliation reads

- **Seeker applications:** `GET /responses` is **server-rendered HTML**, no JSON endpoint. It lists
  rows (position, company, city, date, status) with a tab pair `Основные` (`/responses`) and
  `Удалённые` (`/responses/deleted`). The only status observed is `Не прочитано` ("unread").
- **Dialogs:** `GET /api/frontend_v1/chat/conversations?page=1` returns JSON
  `{"conversations":[…],"meta":{totalResults,currentPage,totalPages,perPage,showTelegramToggle,…}}`
  (`conversations` empty for this account). No dialogs existed, so the item shape is not established.
- **No seeker-side responses JSON endpoint was found.** `/api/frontend/vacancies/<id>/responses` is
  POST-only; `GET` returns `404`.

## Official API is employer-side (not a seeker apply path)

`career.habr.com/info/api` documents an **OAuth 2.0 API for employer CRM integration**: register an
app at `/profile/applications`, authorize at `/integrations/oauth/authorize`, exchange the code at
`/integrations/oauth/token`, then read responses on **your company's** vacancies
(`GET /api/v1/integrations/vacancies?access_token=…`) including full candidate resumes. Its stated
purpose is inbound CRM; it is **not** a documented way for a seeker to search or apply. Do not
mistake it for a board-client substitute.

## Change canaries

| Area | Canary |
| --- | --- |
| Apply endpoint | `POST /api/frontend/vacancies/<id>/responses` with a CSRF token returns `200 {"response":{"id":…}}` |
| Apply letter field | The same POST with multipart field `body=<text>` returns that text in `response.message` |
| Update letter | `PATCH …/responses/<response_id>` with `body` returns `200` and updates `message`/`messageHtml` |
| Withdraw | `DELETE …/responses/<response_id>` returns `200 {"status":"success"}` and moves the row to `/responses/deleted` |
| Duplicate | Re-applying answers `401 {"error":{"message":"Вы уже откликнулись на эту вакансию"}}` |
| Anonymous apply | A no-session POST answers `401 {"error":"Войдите, прежде чем продолжить."}` |
| Throttle | Two responses <10 s apart answer `400 {"message":"…раз в 10 секунд"}` with no `Retry-After` |
| Monthly cap | A 150th response is accepted; the 151st answers `400 {"message":"…не более 150 откликов в месяц"}`; deleted responses count toward the 150 |
| Apply mode | Listing/detail item still carries `response.kind` ∈ `direct`/`applied` |
| Conversations | `GET /api/frontend_v1/chat/conversations` still returns `{conversations, meta}` |

## Not established yet

- Monthly-cap reset boundary (calendar month vs rolling window) and the exact reset time/timezone.
- External / "отклик на другом сайте" applications: a `response.kind` for them was never observed and
  no external-apply vacancy was found.
- Archival / closed-vacancy apply signal: not provoked (no archived vacancy appeared in listings).
- Screening tests / questionnaires at apply: no test field was seen on listing/detail.
- Full `response.kind` enum (`guest` appears for non-XHR profiles; see
  [Response models](api/response-models.md#listing-item)) and the meaning of `isQuick`, `result`,
  `vacancyRecommendationAccuracyPercent`.
- Response status vocabulary beyond `Не прочитано` (invited / declined / read states), which needs an
  employer-side action to observe.
- `/conversations` item shape (no dialog existed on the experiment account).
