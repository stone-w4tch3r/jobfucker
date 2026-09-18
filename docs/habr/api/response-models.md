# Response Models

Structures observed on the live read surfaces, and how they map to the
[client contract](../../specs/client-contract.md) models.

> Freshness: captured 2026-09-18 from `GET /api/frontend/vacancies`, `GET /vacancies/<id>`,
> `GET /profile`, and `/vacancies/rss`. Field sets are additive; treat missing/extra keys as
> non-breaking.

## Listing item

One element of `list[]` from [the search endpoint](search.md#endpoint):

```json
{
  "id": 1000168684,
  "href": "/vacancies/1000168684",
  "title": "ML-инженер (Python · PyTorch · fine-tuning)",
  "remoteWork": true,
  "salaryQualification": {"title": "Средний (Middle)", "href": "/vacancies?qid=4"},
  "publishedDate": {"date": "2026-09-15T10:23:50+03:00", "title": "15 сентября"},
  "company": {"id": 1000125680, "alias_name": "ulgroup", "href": "/companies/ulgroup",
              "title": "Урал Логистика", "accredited": false,
              "logo": {"src": "..."}, "rating": null},
  "employment": "full_time",
  "salary": {"from": null, "to": null, "currency": null, "formatted": ""},
  "predictedSalary": {"from": 238000, "to": 325000, "currency": "rur",
                      "formatted": "от 238 000 до 325 000 ₽"},
  "divisions": [{"title": "ML разработчик", "href": "/vacancies/spec/ai/ml-engineer"}],
  "skills": [{"title": "Python", "href": "/vacancies/programmist_python"}],
  "locations": [{"title": "Москва", "href": "/vacancies?city_id=678"}],
  "qualification": "Middle",
  "archived": false,
  "hidden": false,
  "quickResponseHref": "/api/frontend/quick_responses"
}
```

Field notes:

| Field | Use |
| --- | --- |
| `id` | numeric vacancy id (stringify for `ServiceVacancyId`) |
| `href` | canonical website path `/vacancies/<id>` |
| `title` | vacancy title |
| `publishedDate.date` | ISO-8601 with offset (`published_at`) |
| `company.title` / `company.alias_name` | company display name / slug |
| `salary.{from,to,currency,formatted}` | stated salary; `null`/empty when not stated (`currency` lowercased, e.g. `usd`) |
| `predictedSalary` | board's prediction when no stated salary; **separate** from `salary` |
| `remoteWork` | remote flag |
| `qualification` / `salaryQualification.title` | grade ("Middle"/"Senior"/...) |
| `skills[].title` | structured skill names (`key_skills`) |
| `divisions[]`, `locations[]` | specialization / city chips |
| `employment` | `full_time` \| `part_time` |
| `archived`, `hidden` | availability flags |
| `quickResponseHref` | apply endpoint hint (Session 3); `response.kind` on detail gives the apply mode |

No description/snippet is present in the listing — fetch the detail page for it.

## Detail page

`GET /vacancies/<id>` is an HTML page containing an inline JSON object under `"vacancy":{...}`. It is
the same shape as a listing item **plus** `description` (full HTML), and it is the most stable
structured detail source:

```json
"vacancy": {
  "id": 1000167594,
  "title": "Team Lead команды разработки",
  "description": "<p>...</p>",
  "salary": {"from": null, "to": null, "currency": null, "formatted": ""},
  "response": {"kind": "direct"},
  "...": "same keys as a listing item"
}
```

- `description` is escaped HTML (`\u003c...`) and must be normalized to text for
  `Vacancy.description` (the contract requires full normalized text, not a snippet).
- `response.kind` is the apply-mode signal (`direct` observed); other kinds are Session 3.
- The page also carries a schema.org `JobPosting` `ld+json` block with a smaller field set:
  `datePosted`, `title`, `description`, `identifier.value` (= vacancy id), `validThrough`,
  `hiringOrganization.{name,logo,sameAs}`, `jobLocation[]`, `jobLocationType`, `employmentType`.
  Prefer the inline `vacancy` object (richer); use JSON-LD as a fallback canary.

Detail fetch is one request per vacancy; there is no bulk/variant endpoint.

## Authenticated identity

`GET /api/frontend_v1/users/me` → `{user, meta, userCompanies}`; `user.alias` / `fullName` / `email`.
Anonymous returns `200 {}`. See [Authentication](../authentication.md#authenticated-identity).

## Owned resume

- The account's resume is its **profile page**: `GET /profile` (authenticated) renders the resume
  (about, companies, education, skills) and the public form lives at `GET /<alias>`.
- No JSON endpoint listing owned resumes was found (`/api/frontend_v1/resumes` is the public
  specialist directory, not owned resumes).
- No numeric resume id appears in the page; the inline `"resume":{...}` object is content-only
  (sections with `edit` links). The observable resume identifier is the **account alias**
  (`user.alias`, e.g. `<account-alias>`), which is also the public URL slug. Treat `resume_id` as the alias
  until the apply request proves otherwise (Session 3).
- Whether an account can hold more than one resume is not established; the UI observed exposes a
  single profile-resume.

## RSS item

Fixed latest-50 feed, not a search surface ([Search](search.md#rss)):

```xml
<item>
  <title>...</title>
  <description>Компания «X» ищет ... Москва (Россия). Полный рабочий день. Требуемые навыки: #a, #b.</description>
  <author>Company Name</author>
  <pubDate>Fri, 18 Sep 2026 17:02:35 +0300</pubDate>
  <link>https://career.habr.com/vacancies/1000168447</link>
  <guid>1000168447</guid>
  <image>https://habrastorage.org/...</image>
</item>
```

`guid` is the vacancy id, `link` the detail URL, `description` a plain-text summary (company, city,
employment, hashtag skills). Useful as a cheap sanity canary, not as a client data source.

## Contract mapping

| Contract model | Board source |
| --- | --- |
| `VacancyShort` | listing item: `id`, `title`, `href`→`url`, `company.title`, `salary`, `locations[0].title`→`area`, `publishedDate.date`→`published_at`; snippet fields have no board equivalent (leave `None`) |
| `Vacancy` | detail inline `vacancy`: listing fields + normalized `description`; `skills[].title`→`key_skills`; `has_hh_test` has no Habr analogue (leave `None`) |
| `ResumeInfo` | profile-resume: `resume_id`=`user.alias`, `title` from profile/`fullName`, `updated_at` not established |
| `ServiceIdentity` | `/api/frontend_v1/users/me`: `alias`→`external_id`, `fullName`→`display_name`, `email` |
| `found` | listing `meta.totalResults` |
| `ui_url` | constructed by the client from the executed params |

`Salary` mapping: `salary.{from,to,currency}` → `Salary.from_/to/currency`; gross is not reported
(the board does not distinguish), so fix `gross` to the product default. `predictedSalary` must not
be silently merged into `salary`.

## Change canaries

| Area | Canary |
| --- | --- |
| Listing shape | A listing item still has `id`, `title`, `href`, `salary`, `skills`, `publishedDate.date` |
| Detail | `/vacancies/<id>` still embeds a `"vacancy":{...}` JSON with `description` |
| JSON-LD | The page still has one `JobPosting` `ld+json` block with `identifier.value` = the id |
| Resume | `/profile` still renders the resume; `user.alias` still equals the public slug |
| RSS | An item still has `guid` (id), `link`, `author`, `pubDate` |

## Not established yet

- Whether `updated_at` is available for the resume.
- Archived/hidden items: whether they appear in listings and how `response.kind` differs.
- The exact `response.kind` enum and its apply implications (Session 3).
