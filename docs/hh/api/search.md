# Vacancy Search

## Search modes

Expose two explicit operations. They share parameters and response parsing but have different
meaning:

| Mode | Request | Seed |
| --- | --- | --- |
| Catalog search | `GET /vacancies` | Whole catalog, optionally constrained by `text` |
| Resume-scoped search | `GET /resumes/{resume_id}/similar_vacancies` | HH similarity/relevance for an owned resume |

Both modes share the search envelope and a broad common filter surface, but the catalog endpoint
received the exhaustive parameter matrix while resume-scoped search received a representative
subset. Do not model resume-scoping as a filter. `resume` and `resume_id` on `/vacancies` are
silently ignored and can accidentally broaden a search to the whole catalog; reject them locally.

On the website, resume-scoped search is `/search/vacancy?resume=<id>&from=resumelist`. It maps to
the dedicated API operation even though the browser path is the same as catalog search. See
[Website behavior](../website.md#search-results-page).

## Encoding and validation

- Lists are repeated query parameters: `area=1&area=40`. Comma-joined values are rejected.
- Booleans are lower-case strings: `true` and `false`.
- `page` is zero-based. `per_page` defaults to 20 and is at most 100. Enforce 1–100 as the client
  invariant; upstream accepted `0` but returned an unusable empty page.
- Validate enum values and cross-field constraints locally.
- A response with `errors` is a failure even when HTTP status is 200. Successful search has both
  `items` and `found`.
- `text` supports HH query language such as `(Python OR TypeScript) NOT стажер` unless
  `no_magic=true`.

## Paging

The server limits accessible results to 2,000 and pre-caps `pages` accordingly:

```text
for page in 0 .. first_response.pages - 1:
    fetch page with the same per_page and filters
    stop if items is empty
```

For example, a million-result query with `per_page=20` reports at most `pages=100`. A request whose
offset crosses the cap can return `400 bad_argument` with “you can't look up more than 2000 items”.
Keep that error mapped even though an ordinary loop over reported `pages` avoids it.

## Parameter catalog

The catalog below is authoritative for `/vacancies`. A 45-case matrix plus combination probes
confirmed that resume-scoped search accepts the same major families: text, sorting, paging,
work/employment, salary, dates, geography, employer/role, and facets. This was not an exhaustive
parity proof. `describe_arguments`, `responses_count_enabled`, `excluded_text`, and
`accept_temporary` were directly verified on resume-scoped search on 2026-08-29: all four are
accepted, and `excluded_text` and `accept_temporary` filter for real. `saved_search_id` behaves
differently by mode: on resume-scoped search a nonexistent or not-owned ID returns
`400 bad_argument`, while on `/vacancies` it is silently ignored.

### Core query and sorting

| Parameter | Type | Values/constraints |
| --- | --- | --- |
| `text` | string | Free text and HH query language; optional |
| `no_magic` | boolean | `true` disables query-language interpretation |
| `search_field` | repeated enum | `name`, `company_name`, `description`; meaningful only with `text` |
| `order_by` | enum | `relevance`, `publication_time`, `salary_desc`, `salary_asc`, `distance` |
| `sort_point_lat`, `sort_point_lng` | float pair | Both required with `order_by=distance`; forbidden without it |
| `page` | integer | Zero-based |
| `per_page` | integer | Client: 1–100, default 20. Upstream also accepted 0 as an empty-page edge case |
| `clusters` | boolean | `true` includes search facets in `clusters` |
| `describe_arguments` | boolean | `true` populates `arguments` with active filters and removal URLs |
| `responses_count_enabled` | boolean | `true` adds response counters to search items |

Text correction is not an error, and the shapes differ by mode. Catalog search produced
`suggests: {value, found}` with `fixes: null`; resume-scoped search produced
`fixes: {original, fixed}` with `suggests: null`. Decode both nullable objects independently.

### Work arrangement

| Parameter | Cardinality | Values |
| --- | --- | --- |
| `schedule` | repeated | `fullDay`, `shift`, `flexible`, `remote`, `flyInFlyOut` |
| `work_format` | repeated | `ON_SITE`, `REMOTE`, `HYBRID`, `FIELD_WORK` |
| `employment` | repeated | `full`, `part`, `project`, `volunteer`, `probation` |
| `employment_form` | repeated | `FULL`, `PART`, `PROJECT`, `FLY_IN_FLY_OUT` |
| `experience` | one | `noExperience`, `between1And3`, `between3And6`, `moreThan6` |
| `education` | one | `not_required_or_not_specified`, `special_secondary`, `higher` |
| `part_time` | repeated | `employment_project`, `employment_part`, `temporary_job_true`, `from_four_to_six_hours_in_a_day`, `only_saturday_and_sunday`, `start_after_sixteen` |
| `work_schedule_by_days` | repeated | See complete set below |
| `working_hours` | repeated | `HOURS_2` through `HOURS_12`, `HOURS_24`, `FLEXIBLE`, `OTHER` |
| `accept_temporary` | boolean | Restrict to temporary-work vacancies |

`employment=temporary` is invalid. `employment_form` is the newer parallel vocabulary; both old
and new working parameters remain functional.

`work_schedule_by_days` values:

```text
SIX_ON_ONE_OFF, FIVE_ON_TWO_OFF, FOUR_ON_THREE_OFF, FOUR_ON_TWO_OFF,
THREE_ON_THREE_OFF, THREE_ON_TWO_OFF, TWO_ON_TWO_OFF, TWO_ON_ONE_OFF,
ONE_ON_THREE_OFF, ONE_ON_TWO_OFF, WEEKEND, FLEXIBLE, OTHER, FOUR_ON_FOUR_OFF
```

### Salary

| Parameter | Cardinality | Values/constraints |
| --- | --- | --- |
| `salary` | one integer | Requested minimum/nearby salary fork; unstated-salary vacancies remain unless `only_with_salary=true` |
| `currency` | one enum | `RUR`, `USD`, `EUR`; `KZT` was also accepted; default for salary is RUR |
| `only_with_salary` | boolean | Require a stated salary |
| `salary_frequency` | repeated | `DAILY`, `WEEKLY`, `TWICE_PER_MONTH`, `MONTHLY`, `PER_PROJECT` |
| `salary_mode` | repeated | `MONTH`, `SHIFT`, `HOUR`, `FLY_IN_FLY_OUT`, `SERVICE` |

HH converts currencies at its own rates. Keep the original structured salary from result payloads;
do not assume the search threshold equals an exact lower bound.

### Publication time

| Parameter | Type | Constraint |
| --- | --- | --- |
| `period` | positive integer | Days; omit for “any time”; cannot coexist with dates |
| `date_from` | `YYYY-MM-DD` | Cannot coexist with `period` |
| `date_to` | `YYYY-MM-DD` | Requires `date_from`; cannot coexist with `period` |

The website dictionary represents “any time” as `0`, but the API rejects `period=0`. Omit the
parameter instead. Values 1, 3, 7, 30, and 31 were accepted; the upper bound is not established.

### Geography

| Parameter | Cardinality | Values/constraints |
| --- | --- | --- |
| `area` | repeated integer | HH area IDs; examples: Moscow `1`, Kazan `88`, Belarus `16` |
| `metro` | repeated string | `line.station`, for example `1.118` |
| `district` | repeated integer | City district IDs |
| `bottom_left_lat`, `bottom_left_lng`, `top_right_lat`, `top_right_lng` | four floats | All four required; a partial box is silently ignored |

Dead bbox names `top_lat`, `bottom_lat`, `left_lng`, and `right_lng` produce an error. Invalid
metro-like IDs may silently produce zero results. Area/role IDs come from public suggests;
metro IDs can be taken from vacancy-detail `address.metro_stations[].station_id`.

### Employer, profession, labels, exclusions

| Parameter | Cardinality | Values/constraints |
| --- | --- | --- |
| `professional_role` | repeated integer | Role IDs; examples: programmer `96`, data scientist `165` |
| `industry` | repeated integer | Industry IDs; example: IT `7` |
| `employer_id` | repeated integer | Include employer IDs |
| `excluded_employer_id` | repeated integer | Exclude employer IDs |
| `excluded_text` | string | Comma-separated words to exclude |
| `label` | repeated enum | See complete set below |
| `saved_search_id` | string | Account-owned saved-search ID; nonexistent/not-owned IDs are silently ignored |

`label` values:

```text
with_address, not_from_agency, accept_kids, accredited_it, low_performance,
internship, night_shifts, with_salary, accept_labor_contract
```

Legacy labels such as `from_web_search`, `from_mobile`, `response`, `reject`, `top`, `premium`,
and `has_updates` are rejected.

For company-name search use `text=<name>&search_field=company_name`; `company=<name>` alone is
inert.

## Parameters to reject locally

These values are accepted or superficially plausible but can silently return an unfiltered search:

| Parameter/form | Problem |
| --- | --- |
| `resume`, `resume_id` on `/vacancies` | Ignored; use the resume-scoped endpoint |
| `company` alone | Ignored; use `text` + `search_field=company_name` |
| `premium` | Accepted but no filtering effect observed |
| `compensation_per_mode` | Inert |
| `ored_clusters` alone | Inert |
| Partial four-corner bbox | Inert |
| `search_field` without `text` | Inert |
| Unknown/non-owned `saved_search_id` | Inert on `/vacancies`; `400 bad_argument` on resume-scoped search |
| Comma-joined list values | Rejected; use repeated params |

Silent broadening is more dangerous than a hard failure, so client-side validation is required.

## Search envelope

```json
{
  "items": [],
  "found": 3552,
  "page": 0,
  "pages": 20,
  "per_page": 100,
  "clusters": null,
  "arguments": null,
  "fixes": null,
  "suggests": null,
  "alternate_url": "https://hh.ru/search/vacancy?..."
}
```

With `describe_arguments=true`, `arguments` is an array of objects containing `argument`, `value`,
`value_description`, and `disable_url`. With `clusters=true`, `clusters` contains facet groups.

Search result items contain snippets, not the full description. Fetch
`GET /vacancies/{id}` before scoring or other content-sensitive work. See
[Response models](response-models.md#vacancy-search-item).

## Source dictionaries

The anonymous server-rendered search page embeds `vacancySearchDictionaries`, including the enum
sets above plus `minItemsOnPage=0`, `maxItemsOnPage=100`, `maxSearchResult=2000`, and
`privateParamPrefix="L_"`. Do not send `L_` private/experimental parameters. Public IDs and text
suggestions are available through the endpoints in [Endpoint catalog](endpoints.md).
