# Search and Listings

The listing surface is a **JSON endpoint**, `GET /api/frontend/vacancies` (note: `/api/frontend/`,
not `/api/frontend_v1/`). The server-rendered `/vacancies` HTML page shows the same data; the RSS
feed is not a search surface.

> Freshness: probes run 2026-09-18 over pure HTTP with an exported authenticated cookie jar and a
> clean anonymous jar. Totals drift with live data; use the shapes and parameter semantics, not the
> example numbers.

## Endpoint

```http
GET https://career.habr.com/api/frontend/vacancies?<params>
Accept: application/json, text/javascript, */*; q=0.01
X-Requested-With: XMLHttpRequest
```

Response:

```json
{
  "list": [ { /* listing item, see response-models.md */ } ],
  "meta": {"totalResults": 465, "perPage": 25, "currentPage": 1, "totalPages": 19},
  "recommendedQuickVacancies": [ /* unrelated promo items, ignore */ ]
}
```

- Works anonymously (verified); the session cookie is not required for listings.
- `meta.totalResults` is the board-reported total (`found`); `meta.currentPage` echoes the requested
  page; `meta.totalPages` is derived.
- The site's own client calls this endpoint with a `X-CSRF-Token` header too, but that is not
  required for `GET` (verified without it).

## Query parameters

| Param | Meaning | Values / notes |
| --- | --- | --- |
| `q` | text query | free text; omitted = all vacancies |
| `type` | search mode | `all` \| `suitable` |
| `sort` | ordering | `relevance` (default) \| `date` \| `salary_desc` \| `salary_asc` |
| `page` | 0-based page | `page=1` is the first page; `page=0` is accepted and echoes `currentPage:0` |
| `per_page` | requested page size | echoed back, but **at most 50 items are returned** (see caps) |
| `qid` | qualification | `1`=Intern, `3`=Junior, `4`=Middle, `5`=Senior, `6`=Lead; `2` returns 0; omitted = any |
| `remote` | remote work | `true` |
| `with_salary` | only vacancies with a stated salary | `true` |
| `salary` | minimum "from" threshold | integer; interpreted in `currency` |
| `currency` | salary currency | `RUR` (default) \| `EUR` \| `USD` \| `UAH` \| `KZT` |
| `skills[]` | required skills | skill ids, repeatable arg; ids from `/api/frontend_v1/suggestions/skills?q=` |
| `city_id` | city filter | numeric id from a listing item's `locations[].href` (`/vacancies?city_id=678`) |
| `employment_type` | employment | `full_time` \| `part_time` |
| `company_ids[]` | company filter | **unverified** (see below) |
| `divisions` | specialization | **unverified** (see below) |

Repeatable filters use bracket syntax (`skills[]=446&skills[]=1241`). `qid` and `employment_type`
and `currency` are singletons.

Verified effects (examples): `qid=5`, `remote=true`, `with_salary=true`, `salary=200000&currency=RUR`,
`skills[]=446`, `city_id=678`, `employment_type=part_time`, `sort=date`/`salary_desc` all change
`totalResults` or ordering. `sort` values outside the enum are ignored (no `400`).

### Unverified filter params

- `company_ids[]=<numeric id>` returned `0` results even for a company that exists, and the scalar
  form `company_ids=<id>` returns `500`. The accepted value shape (alias? different id space?) is not
  established.
- `divisions` did not change results with a slug value; the specialization picker's `filters.s`
  serialization was not captured. Treat specialization filtering as unverified.
- `locations`/`locations[]` did not filter reliably (`locations[]=678` returned the cap, i.e. was
  ignored); use `city_id`.

## `type=suitable`

- Authenticated: resume-scoped "подходящие" list (small set, e.g. 13).
- **Anonymous: the filter is ignored** — the response is the full pool (capped). A client must hold a
  session for `type=suitable` to mean anything; never treat the anonymous response as suitable.
- Whether it requires an owned resume could not be tested negatively (the experiment account has a
  profile).

## Pagination, page size, and caps

- **Effective page-size cap is 50.** `per_page=100`/`1000` are echoed in `meta.perPage`, but `list`
  carries at most 50 items and `totalPages` is computed from 50
  (`465` → `10` pages at `per_page=100`; `19` pages at `per_page=25`).
- **Accessible-position cap ≈ 1000.** Requests whose window starts at offset ≥ 1000 return
  `200` with an empty `list` while `meta.totalResults` stays the true total. Observed last accessible
  position was 995 (`per_page=50&page=20` → 46 items; `per_page=25&page=40` → 21 items).
  `service_info.max_search_items` should therefore be `1000` (conservative).
- **`page` beyond `meta.totalPages` returns `404 {"error":"Not found"}`**, not an empty page. A
  paging walk must treat this as exhaustion (`NotFoundError` → stop), not as a hard failure.
- Page walk beyond the accessible cap but within `totalPages` returns `200` with an empty list.

## HTML listing

`GET /vacancies?<same params>` is server-rendered: 25 `.vacancy-card` items, pagination links
`?page=N`, no total count in the HTML. It is usable as a fallback but the JSON endpoint is strictly
better (structured fields, `totalResults`, configurable page size).

## RSS

`GET /vacancies/rss` returns RSS 2.0 but is a **fixed latest-50 feed**: `page`, `per_page`, and `q`
are ignored (verified). It is not viable for `list_vacancies`/`search_vacancies`; keep it out of the
client.

## Errors

| Signal | Meaning | Mapping |
| --- | --- | --- |
| `500 {"status":500,"error":"Internal Server Error"}` | malformed filter value (e.g. scalar `company_ids`, string `locations`) | `BadRequestError` — validate params locally, never send untyped values |
| `404 {"error":"Not found"}` | page beyond `meta.totalPages` | exhaustion / `NotFoundError` |
| `200` + empty `list` | window at/after the ~1000 cap | exhaustion |

## Contract mapping

- `list_vacancies()` → this endpoint; `meta.totalResults` → `found`; `list[]` → `VacancyShort`
  (see [response models](response-models.md#listing-item)).
- `search_vacancies()` → the same listing for ids/geometry, then a detail `GET /vacancies/<id>` per
  slice item for the full description (no bulk detail endpoint exists).
- `ui_url` is constructed by the client from the executed params (`/vacancies?q=...&type=...`); the
  API does not return one.
- `service_info.max_search_items` → `1000`.

## Change canaries

| Area | Canary |
| --- | --- |
| Endpoint | `GET /api/frontend/vacancies?q=python&type=all` returns `{list, meta}` with `meta.totalResults` |
| Page size | `per_page=100` returns at most 50 items and `totalPages` uses 50 |
| Cap | A page at offset ≥ 1000 returns `200` with an empty `list` |
| Over-page | A page beyond `meta.totalPages` returns `404 {"error":"Not found"}` |
| Filters | `qid=5`, `remote=true`, `with_salary=true`, `skills[]=<id>`, `city_id=<id>` change `totalResults` |
| Sort | `sort=date` reorders by `publishedDate`; `sort=salary_desc` puts salaried items first |
| Anonymous | Listing works without a session; anonymous `type=suitable` is ignored |
| RSS | `/vacancies/rss` still returns 50 items and ignores `page`/`per_page`/`q` |

## Not established yet

- `company_ids[]` and specialization (`divisions`/`s`) value shapes.
- Archived/hidden vacancy visibility and their effect on totals.
- The exact cap boundary (observed 995 last accessible position; declared 1000).
- Whether `type=suitable` requires an owned resume, and how many resumes an account can have.
- Rate/captcha behavior at fetch volume (see [transport](transport-and-errors.md)).
