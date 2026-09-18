# Auxiliary id catalogs

Generated dictionaries of Habr Career filter ids, used when writing search configs for the future
client. They are **data, not behavior** — the semantics live in [Search and listings](../api/search.md).

| File | Contents |
| --- | --- |
| [skills.yaml](skills.yaml) | `skill id → {name, alias}` (the `skills[]=` filter values) |
| [locations.yaml](locations.yaml) | countries, regions, cities, and the city ids currently in use |

> Generated: 2026-09-18 by a prefix crawl of the public autocomplete endpoints. Ids belong to the
> board's geo database and may change or grow; treat the files as a snapshot and regenerate when
> needed.

## Provenance and method

The board exposes no bulk dictionary. The only public source is the 25-result autocomplete:

```http
GET /api/frontend/suggestions/skills?term=<prefix>
GET /api/frontend/suggestions/locations?term=<prefix>
GET /api/frontend/suggestions/countries
```

A prefix trie crawl (`term=<prefix>`, expand a prefix with each next character while the response is
capped at 25 and adds new entries) recovers everything reachable through those endpoints. Skills
were crawled to full depth; locations were crawled to depth ~3 worldwide plus a Russia-focused
deeper pass. The country id list was completed from city/region `subtitle` names plus the countries
endpoint, and Russia's federal-subject regions were resolved by querying each subject name.

## Coverage

| Section | Count | Notes |
| --- | --- | --- |
| `skills` | 1617 | full prefix-crawl; effectively complete |
| `countries` | 198 | ids observed as `ct_<id>`; `country_slugs` holds the endpoint's 25-slug list |
| `regions` | 512 | `r_<id>`; 84 are Russian federal subjects (`country_id: 444`) |
| `cities` | 16854 | `c_<id>`; 5953 are Russian (`country_id: 444`); the rest are foreign |
| `in_use_city_ids` | 51 | city ids referenced by current vacancies — the practical subset |

**Not exhaustive.** The geo database is a worldwide settlement list (villages included), far larger
than any autocomplete crawl can enumerate, and the autocomplete ignores country/limit scoping
params. The Russia crawl hit its budget with work left; foreign cities are a partial sample.
`in_use_city_ids` is the reliable small set for real pipelines.

## Semantics

- `cities` keys are the bare number of the `city_id` query parameter
  (`/vacancies?city_id=678`, i.e. autocomplete value `c_678`).
- `regions` keys are bare numbers of `r_<id>` entities (the autocomplete's region rows).
- `countries` keys are bare numbers of `ct_<id>` entities.
- `skills` keys are the `skills[]=<id>` values (the autocomplete returns them as plain integers).
- `regions[*].country_id` is only populated for Russian regions; foreign region → country linkage was
  not resolved.
- Ids are numeric and stable in the snapshot; do not hard-code them into code — reference this
  catalog when authoring a pipeline config, and prefer names when the UI shows them.

## Regenerating

The crawl is a plain authenticated-or-anonymous HTTP loop; keep it polite (single-digit
concurrency, ~0.2 s pacing) and stop on `403`/`429`. Sketch:

```python
queue = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюяabcdefghijklmnopqrstuvwxyz0123456789.-+#")
seen, found = set(), {}
while queue:
    term = queue.pop(0)
    items = get_json("/api/frontend/suggestions/skills", params={"term": term})["list"]
    new = [i for i in items if i["value"] not in seen]
    seen.update(i["value"] for i in items)
    found.update({i["value"]: i for i in items})
    if len(items) == 25 and new:          # capped and still discovering → deepen
        queue += [term + ch for ch in "абвг..."]
```

For locations, add the Russia filter (`country == "ct_444"`) to decide when to deepen, and harvest
`in_use_city_ids` from `GET /api/frontend/vacancies?type=all&per_page=50&page=N` `items[].locations`.
