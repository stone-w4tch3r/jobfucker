# Tests (employer screening tests at apply)

How hh.ru employer screening tests («Тест при отклике») actually work at the HTTP level, and how
to take them programmatically. Verified live 2026-09-11/12 on the `hh-exp` dev account
(5 research applies total: 2 on 2026-09-11, 3 on 2026-09-12). Research protocol and probe history:
[HH-TESTS-PLAYBOOK.md](HH-TESTS-PLAYBOOK.md).

TL;DR: a test is **not a separate flow** — questions are embedded in the apply page HTML and the
answers ride the one apply POST. The whole flow is a stateless pair of plain web requests
(`GET` page → parse JSON → `POST` multipart) authorized by web cookies only.

---

## 1. Where tests live

### API side carries almost nothing

`GET https://api.hh.ru/vacancies/{id}` (Bearer) exposes only:

- `has_test: bool` — vacancy has a test at all;
- `test: {"required": bool} | null`.

No questions, no count, no timer. All test content is website-only.

### Website apply page: the `vacancyTests` blob

Open the apply page `https://hh.ru/applicant/vacancy_response?vacancyId={id}` **with web cookies**
(`hhul`, `hhrole=applicant`, `hhtoken`, `_xsrf`; Bearer does not authorize `hh.ru`). The page HTML
embeds the full test inside the `<template id="HH-Lux-InitialState">` element — a JSON blob
(~200–270 KB, `textContent` of the template; ~216 KB typical):

- `applicantVacancyResponseStatuses[vacancyId].test` → `{hasTests, testId, required}`
- `applicantVacancyResponseStatuses[vacancyId].shortVacancy.userTestId` (== testId), `.userTestPresent`
- `applicantVacancyResponseStatuses[vacancyId].alreadyApplied`, `.responseImpossible`
- `applicantVacancyResponseStatuses[vacancyId].resumes[resumeId]._attributes.hash` → the
  `resume_hash` needed for the POST (not the resume id!)
- `applicantVacancyResponseStatuses[vacancyId].unusedResumeIds` → applicable resume ids
- **`vacancyTests[vacancyId]`** — the test itself:

```json
{
  "uidPk": "389524035",
  "guid": "B117D210-6BEB-0D82-443C-144E2F5A46C5",
  "name": "Запрос СТДР",
  "description": "<html intro text>",
  "required": "true",
  "startTime": "1789147702",
  "tasks": [
    {"id": 389524036, "description": "<html question>",
     "multiple": "false", "open": "true",
     "candidateSolutions": [{"id": "389524037", "text": "Да"},
                            {"id": "389524038", "text": "Нет"}]},
    {"id": 389524039, "description": "<html question>", "multiple": "false",
     "open": "true", "candidateSolutions": []}
  ]
}
```

All booleans and ids are **stringly typed** in the blob (`"true"`, numeric ids as strings inside
`candidateSolutions`, numeric `id` on tasks).

Raw-HTML gotcha: in the fetched HTML the blob is **HTML-escaped** (`&#34;` for `"`) and the
template tag carries extra attributes (`<template style="display:none" id="…">`). Extract with
`re.search(r'<template[^>]*id="HH-Lux-InitialState">(.*?)</template>', html, re.S)` +
`html.unescape` before `json.loads`. In a live browser the SPA consumes the template within
moments of hydration — extracting from raw HTML (or very early) is the reliable way.

### Task kind matrix (observed)

`open:"true"` adds the «Свой вариант» free-text option; `multiple` semantics are NOT
"checkboxes" in any observed case.

| kind | blob shape | UI rendering | frequency (18 live tests, 47+ tasks) |
| --- | --- | --- | --- |
| free text | `candidateSolutions: []`, `open:"true"` | textarea `task_{id}_text` | dominant (26) |
| single choice | `multiple:"false"`, `open:"false"`, options | radios `task_{id}` | common (16) |
| choice + own variant | `multiple:"false"`, `open:"true"`, options | radios + extra radio value `open` enabling `task_{id}_text` textarea | occasional (5) |
| checkbox (`multiple:"true"` + options) | — | — | **never observed in the wild** |

Tests are small: 1–10 tasks (median ~3). All 18 probed tests were native HH forms; no external
vendor test hosts encountered.

### After a successful apply

- Reopening the apply page shows «Вы откликнулись»; blob flips to
  `alreadyApplied: true`, `responseImpossible: true` and **the `vacancyTests` key disappears
  entirely** — test content is not recoverable post-apply.
- Answers are never visible to the applicant again (negotiation messages contain one empty
  applicant message; answers live employer-side only).

---

## 2. Submission contract (one POST = apply + test answers)

```http
POST https://hh.ru/applicant/vacancy_response/popup
Content-Type: multipart/form-data
```

Form fields (complete, verified across UI POST + manual POST + headless python POST):

| field | value | notes |
| --- | --- | --- |
| `_xsrf` | xsrf token (== `_xsrf` cookie value) | **mandatory**; wrong/missing → `403` error-page JSON |
| `uidPk` | test `uidPk` from blob | |
| `guid` | test `guid` | |
| `startTime` | test `startTime` (echoed unchanged) | server restamps it on every page open; see §5 |
| `testRequired` | blob `required` (`"true"`/`"false"`) | |
| `task_{id}` | chosen `candidateSolutions[].id` | radio answer |
| `task_{id}` = `open` | literal `open` | «Свой вариант» chosen; must pair with `task_{id}_text` |
| `task_{id}_text` | free text | for pure-text tasks, and alongside any radio task whose `open:"true"` (sent even empty when a predefined option is chosen) |
| `vacancy_id` | vacancy id | |
| `resume_hash` | resume hash from blob (hex) | not the resume id |
| `ignore_postponed` | `true` | |
| `incomplete` | `false` | `true` triggers resume-completeness validation — see errors |
| `mark_applicant_visible_in_vacancy_country` | `false` | |
| `country_ids` | `[]` | |
| `letter` | cover letter (empty ok) | |
| `lux` | `true` | |
| `withoutTest` | `no` | `yes` is always rejected — see §6 |
| `hhtmFromLabel`, `hhtmSourceLabel` | empty | |

Encoding per task kind:

| task kind | fields sent |
| --- | --- |
| free text | `task_{id}_text=<text>` |
| single choice | `task_{id}=<solutionId>` |
| choice + own variant, predefined option | `task_{id}=<solutionId>` + `task_{id}_text=` (empty) |
| choice + own variant, «Свой вариант» | `task_{id}=open` + `task_{id}_text=<text>` |
| checkbox | unobserved; expect repeated `task_{id}` fields (guess, unverified) |

**All tasks must be answered.** Server-side check: any unanswered task → `400 test-required`
(verified: 0 answers on 1- and 3-task tests, 1-of-3 partial — always the same error, regardless
of the test's `required` flag). Empty-submit is additionally blocked client-side (UI marks radios
`magritte-invalid`, no POST fires) — the server check is the one that matters for automation.

The browser UI also sends `X-Requested-With: XMLHttpRequest`, `X-Xsrftoken` and `X-GIB-*`
bot-protection headers. **All of them are optional** — verified headless python POST succeeded
with cookies + browser UA only (§8).

---

## 3. Response contract

### Success (HTTP 200, `application/json`)

```json
{"success": "true", "topic_id": "5569400435", "chat_id": "5622488205",
 "response_label": "nonreq-letter-has-test-without-letter-with-test",
 "vacancy_id": "111570490", "applicantActivity": null,
 "askJobSearchStatus": false,
 "responsesStreak": {"vacancyId": "111570490", "responsesCount": 49, "responsesRequired": 10},
 "responseStatus": {"test": {"hasTests": true, "testId": 179109, "required": false},
                    "negotiations": {"topicList": [{"id": "5569400435", "...": "..."}]}},
 "requiredAdditionalData": ["WORK_FORMAT","ADDRESS_COORDINATES","PREFERRED_WORK_AREAS","SALARY_WHITE","PHOTO"],
 "resumeFields": {"...": "..."}}
```

- `topic_id` == the negotiation id (confirmed via `GET api.hh.ru/negotiations?status=active`,
  item `state: "response"`; also visible in the web «Отклики» list).
- `response_label` variants observed: `nonreq-letter-req-test-without-letter`,
  `nonreq-letter-has-test-without-letter-with-test` (label grammar: letter requirement × test).
- `responsesStreak` — applicant-side gamification counter, not part of the apply result.
- `requiredAdditionalData` — employer-requested profile fields. **No applicant-facing follow-up
  UI appeared within ~9h** on the dev account (chat and negotiations pages carry no prompt);
  treat as employer-side/informational until observed otherwise.

### Errors

Business errors are `400` + `{"error": "…"}` (envelope-before-status; these are business rules,
not auth):

| body | when | notes |
| --- | --- | --- |
| `{"error":"test-required"}` | any test task unanswered | fires for `required:"false"` tests too; fires regardless of `withoutTest` value |
| `{"error":"alreadyApplied"}` | duplicate apply | stale `uidPk`/`guid`/`startTime` still accepted for this check |
| `{"error":"resume-incomplete","redirectUrl":"/profile/resume?resume=<hash>"}` | `incomplete=true` and the resume is incomplete | checked **before** test completeness |

Auth-class failure: wrong or missing `_xsrf` → `403` with an error-page JSON (`css_links`, …) —
not the business envelope. Validation order observed: `_xsrf` → `resume-incomplete` (only when
`incomplete=true`) → `test-required` / `alreadyApplied`.

The pre-submit SPA also calls
`GET /applicant/vacancy_response/popup?isTest=no&lux=true&withoutTest=no&isCheckingResponseType=true&isChatWithoutResponse=false&vacancyId={id}`
— returns a modal HTML/JSON shell, **no test data**; extraction must use the full page blob.

---

## 4. Auth and headers

- Website flow needs **web cookies** (`hhul`, `hhrole=applicant`, `hhtoken`, `_xsrf`, plus
  `__ddg*` and `*gib-w-hh` bot-protection cookies) — no Bearer. Web cookies do NOT authorize
  `api.hh.ru`; Bearer does not authorize `hh.ru`.
- `_xsrf` form field must match the `_xsrf` cookie. Cookies exported from a browser session work
  directly in python `requests`.
- `X-Xsrftoken`, `X-Requested-With`, `X-GIB-*` request headers: **optional** (success verified
  without any of them).
- ddos-guard: raw (non-browser) clients get intermittent TLS drops and, under probe bursts,
  temporary full TCP-connect blackouts (minutes). Retry with backoff is mandatory; pacing
  ≥0.345 s between raw requests. The stealth-browser path keeps working while raw clients are
  blacked out.

---

## 5. Timer (`startTime`) semantics

- Every page open re-stamps `startTime` in the blob (and in the rendered hidden input) to
  "now" (unix seconds). There is no visible countdown on the observed tests.
- The POST echoes `startTime` back unchanged. A submit ~5 minutes after the anchor was accepted
  (HTTP 200, negotiation created).
- Whether the employer sees/penalizes large `startTime`→submit gaps is employer-side and
  unobservable. For automation, echo the `startTime` from a fresh page fetch.

---

## 6. Optional (`required:"false"`) tests and `withoutTest`

- Optional tests exist (e.g. Доктор Веб 111570490: `has_test=true`, `test.required=false`).
  **They still must be answered**: `withoutTest=yes`, `withoutTest=no`+no answers, and partial
  answers all return `400 test-required`. Answering all tasks (even with a stub text) applies
  normally.
- `withoutTest=yes` is rejected whenever any test exists at all — it is not a "skip the test"
  escape hatch.
- jobfucker resolution: the preflight over-skip is removed. The apply stage routes a
  `has_hh_test` vacancy through the `HhTestCapable` capability (fetch the page blob → solve →
  website multipart submit), so `has_test=true, test.required=false` vacancies are applyable.
  `has_test=true, test=null` in the API remains unobserved; the stage treats a fresh blob with no
  test as "no test" and falls back once to the plain API apply.

---

## 7. After-submit state

- Success page redirects to the vacancy page (`hhtmFrom=vacancy_response`).
- Re-apply impossible: `400 alreadyApplied`; apply page becomes idempotent «Вы откликнулись» view.
- Negotiation appears with `state: "response"` (API `GET /negotiations?status=active`) and in the
  web «Отклики» list.
- Test answers invisible to the applicant; employer-side only.

---

## 8. Headless recipe (verified end-to-end)

Full flow that consumed one real application from raw python (no browser):

```python
import html, json, re, requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) …Chrome/146…"
s.cookies.update(exported_browser_cookies)          # incl. _xsrf, hhul, hhrole, hhtoken, __ddg*

# 1. fetch + parse (retry on ddos-guard TLS/connect drops!)
r = s.get(f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}", timeout=40)
blob = json.loads(html.unescape(
    re.search(r'<template[^>]*id="HH-Lux-InitialState">(.*?)</template>', r.text, re.S).group(1)))
test = blob["vacancyTests"][vid]
st = blob["applicantVacancyResponseStatuses"][vid]
resume_hash = st["resumes"][st["unusedResumeIds"][0]]["_attributes"]["hash"]

# 2. build form (ALL tasks answered; xsrf from cookie)
form = {"_xsrf": s.cookies…, "uidPk": test["uidPk"], "guid": test["guid"],
        "startTime": test["startTime"], "testRequired": test["required"],
        "vacancy_id": vid, "resume_hash": resume_hash, "ignore_postponed": "true",
        "incomplete": "false", "mark_applicant_visible_in_vacancy_country": "false",
        "country_ids": "[]", "letter": "", "lux": "true", "withoutTest": "no",
        "hhtmFromLabel": "", "hhtmSourceLabel": ""}
for t in test["tasks"]:
    sols, open_ = t["candidateSolutions"], t["open"] == "true"
    if sols and not open_:   form[f"task_{t['id']}"] = sols[0]["id"]
    elif sols and open_:     form[f"task_{t['id']}"] = "open"; form[f"task_{t['id']}_text"] = answer
    else:                    form[f"task_{t['id']}_text"] = answer

# 3. submit
r = s.post("https://hh.ru/applicant/vacancy_response/popup",
           files={k: (None, v) for k, v in form.items()},
           headers={"Referer": f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}"})
# 200 → r.json()["topic_id"]; 400 {"error": …} → §3 table
```

Working example of this exact flow (moved out of /tmp, one real application per successful run):
[hhtest-repro-example.py](hhtest-repro-example.py).

---

## 9. Provenance (research applies)

| date | vacancy | test | method | result |
| --- | --- | --- | --- | --- |
| 2026-09-11 | Литрес 137126250 | radio+text, 2 tasks | UI | 200, topic 5569272588 |
| 2026-09-11 | IBS 137204191 | 3× text | UI | 200, topic from apply2 HAR |
| 2026-09-12 | GO Digital 137072504 | 9 radios + 1 text | UI (+empty-submit probe) | 200; `task=open` encoding verified |
| 2026-09-12 | Доктор Веб 111570490 | 1 text, `required:false` | manual fetch POST | 200, topic 5569400435; withoutTest probes |
| 2026-09-12 | Selectel 136987865 | 1 text | **headless python** | 200, topic 5569674914; full no-header flow verified |

Negative probes (no applications consumed): `withoutTest=yes` ×2 → `test-required`; partial
answers ×2 → `test-required`; `incomplete=true` → `resume-incomplete`; re-apply →
`alreadyApplied`; wrong/missing `_xsrf` → `403`.

## 10. Remaining unknowns

1. Checkbox tasks (`multiple:"true"` + non-empty `candidateSolutions`) — 0 occurrences in 47+
   observed tasks; encoding unverified (expect repeated `task_{id}` fields).
2. Employer-side `startTime` gap penalties — unobservable from applicant side.
3. `has_test=true, test=null` in API — never encountered live.
4. External/vendor test hosts (`response_url`) — none encountered in the test-vacancy set.
5. `requiredAdditionalData` completion flow — nothing applicant-facing observed in ~9h.

## 11. Deferred solving (design note)

Solving can be split from submission: test identity is stable across days (same `uidPk`/`guid`/
task ids re-observed a day later), so background fetch + answer caching works:

1. Background: `GET` apply page → parse blob → solve → cache
   `{vacancy_id, test uidPk, task_id → answer}`. Free and unlimited.
2. Submit (immediately before POST): fresh `GET` re-stamps `startTime` and re-reads the blob.
   Verify cached task ids still match (employers can edit tests anytime — remap or re-solve on
   mismatch), then submit with the fresh blob's fields.

Never reuse a stale blob or old `startTime` at submit: only a ~5-min gap is verified; a fresh
GET costs one request. Treat `test-required` / `alreadyApplied` / missing `vacancyTests` at
submit time as skip (vacancy closed, test removed, or already applied).
