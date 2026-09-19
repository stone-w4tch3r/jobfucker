"""Auto-refresh the Habr Career id catalogs under ``docs/habr/aux/``.

The board publishes no bulk dictionary: the only public source is a 25-result autocomplete at
``/api/frontend/suggestions/{skills,locations}?term=<prefix>``. Catalogs are therefore built by a
prefix trie crawl (expand a prefix with the next character while it stays capped at 25 and still
discovers new entries).

Scope of the location catalog (single supported logic path):

- **countries** — every country row (tiny, and the country id is a usable search filter);
- **regions** — Russia only (``ct_444``);
- **cities** — Russia only (``ct_444``); foreign branches are never expanded.

Rationale: the vacancy filter accepts ``locations[]=c_<id>`` / ``r_<id>`` / ``ct_<id>`` and
``city_id=<bare id>``, so region/country granularity already covers most search configs, and the
board's geo table is a worldwide settlement list that is not worth mirroring. ``in_use_city_ids``
is kept as a small demand signal (city ids referenced by current vacancies, restricted to the RU
cities we know).

Sync is additive (union merge) because a crawl can miss entries at the 25-result cap; the scope
filter is what keeps the file from growing worldwide again. If a wider scope is ever needed, widen
``should_expand_locations`` (e.g. drop the ``RU_COUNTRY_KEY`` check for worldwide, or add a country
argument) and the matching filters in ``merge_locations`` — nothing else depends on the scope.
A demand-only catalog (in-use ids + resolve-on-demand via ``suggestions/locations?term=``) would
replace the crawl entirely; it was deliberately not built.

Usage:
    uv run poe catalog_habr sync --kind skills|locations|in-use|all [--write|--check]
        [--resume] [--budget N] [--concurrency N]

``--kind in-use`` is the cheap path (~20 requests), suitable for a frequent refresh; the full
catalogs take minutes and are a monthly job. Exit codes: 0 clean, 1 drift under ``--check``,
2 error (load/validation/crawl abort).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import anyio
import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from rusty_results.prelude import Err, Ok, Result

# =============================================================================
# Constants
# =============================================================================

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SKILLS_RELATIVE_PATH: Final[Path] = Path("docs/habr/aux/skills.yaml")
LOCATIONS_RELATIVE_PATH: Final[Path] = Path("docs/habr/aux/locations.yaml")

BASE_URL: Final[str] = "https://career.habr.com"
SKILLS_ENDPOINT: Final[str] = "/api/frontend/suggestions/skills"
LOCATIONS_ENDPOINT: Final[str] = "/api/frontend/suggestions/locations"
COUNTRIES_ENDPOINT: Final[str] = "/api/frontend/suggestions/countries"
VACANCIES_ENDPOINT: Final[str] = "/api/frontend/vacancies"

HTTP_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    ),
    ("Accept", "application/json, text/javascript, */*; q=0.01"),
    ("X-Requested-With", "XMLHttpRequest"),
    ("Accept-Language", "ru-RU,ru;q=0.9"),
)

# First characters of a trie level; covers Cyrillic, Latin, digits and the symbols some skills start with.
ALPHABET: Final[str] = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяabcdefghijklmnopqrstuvwxyz0123456789.-+#"

RESULT_CAP: Final[int] = 25
HTTP_OK: Final[int] = 200
HTTP_SERVER_ERROR: Final[int] = 500
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
REQUEST_DELAY_SECONDS: Final[float] = 0.1
FETCH_ATTEMPTS: Final[int] = 3
CHECKPOINT_EVERY_REQUESTS: Final[int] = 500

SKILL_MAX_DEPTH: Final[int] = 4
# Russian settlement names are short, so a deep prefix is rare; the cap only guards pathological loops.
RU_MAX_DEPTH: Final[int] = 6

DEFAULT_SKILL_BUDGET: Final[int] = 25000
DEFAULT_LOCATION_BUDGET: Final[int] = 40000
DEFAULT_CONCURRENCY: Final[int] = 8

IN_USE_PAGE_SIZE: Final[int] = 50
IN_USE_MAX_PAGES: Final[int] = 20

# Anchors that must survive any sync; their absence means the crawl/schema broke, not that the board removed them.
ANCHOR_SKILL_ID: Final[int] = 446  # Python
ANCHOR_CITY_ID: Final[int] = 678  # Москва
ANCHOR_COUNTRY_ID: Final[int] = 444  # Россия
RU_COUNTRY_KEY: Final[str] = "ct_444"
RU_COUNTRY_ID: Final[int] = 444

CatalogKind = Literal["skills", "locations", "in-use", "all"]
CrawlKind = Literal["skills", "locations"]


# =============================================================================
# Boundary models (validated at the HTTP/YAML edges)
# =============================================================================


class _FrozenModel(BaseModel):
    """Base for boundary DTOs: immutable, tolerant of unknown added fields."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class SkillSuggestion(_FrozenModel):
    """One skill autocomplete row."""

    value: int
    title: str
    alias: str


class SkillPayload(_FrozenModel):
    """`/api/frontend/suggestions/skills` response body."""

    items: list[SkillSuggestion] = Field(alias="list")


class LocationSuggestion(_FrozenModel):
    """One location autocomplete row (`c_`, `r_`, or `ct_` entity)."""

    value: str
    title: str
    subtitle: str | None = None
    region: str | None = None
    country: str | None = None


class LocationPayload(_FrozenModel):
    """`/api/frontend/suggestions/locations` response body."""

    items: list[LocationSuggestion] = Field(alias="list")


class CountrySuggestion(_FrozenModel):
    """One row of `/api/frontend/suggestions/countries` (slug + display name)."""

    value: str
    title: str


# The countries endpoint returns a bare JSON array, so it needs a list adapter.
COUNTRY_ADAPTER: Final[TypeAdapter[list[CountrySuggestion]]] = TypeAdapter(list[CountrySuggestion])


class ListingLocation(_FrozenModel):
    """One `locations[]` row of a vacancy listing item."""

    title: str
    href: str


class ListingItem(_FrozenModel):
    """The part of a vacancy listing item this tool needs."""

    locations: list[ListingLocation] | None = None


class ListingPayload(_FrozenModel):
    """`/api/frontend/vacancies` response body (trimmed)."""

    items: list[ListingItem] = Field(alias="list")


class RawSuggestion(_FrozenModel):
    """Vendor-neutral autocomplete row shared by both catalogs."""

    key: str
    title: str
    alias: str | None = None
    region: str | None = None
    country: str | None = None
    subtitle: str | None = None


class CrawlState(_FrozenModel):
    """Resumable crawl checkpoint."""

    queue: list[tuple[str, int]]
    entries: list[RawSuggestion]
    requests: int


class SkillRecord(_FrozenModel):
    """Rendered skill entry."""

    name: str
    alias: str


class SkillsDoc(_FrozenModel):
    """`docs/habr/aux/skills.yaml` as a typed document."""

    skills: Mapping[int, SkillRecord]


class CityRecord(_FrozenModel):
    """Rendered city entry."""

    name: str
    region_id: int | None = None
    country_id: int | None = None


class RegionRecord(_FrozenModel):
    """Rendered region entry."""

    name: str
    country_id: int | None = None


class LocationsDoc(_FrozenModel):
    """`docs/habr/aux/locations.yaml` as a typed document."""

    countries: Mapping[int, str]
    country_slugs: Mapping[str, str]
    regions: Mapping[int, RegionRecord]
    cities: Mapping[int, CityRecord]
    in_use_city_ids: Mapping[int, str]


# =============================================================================
# Config and outcomes
# =============================================================================


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    """Everything a single prefix crawl needs."""

    kind: CrawlKind
    endpoint: str
    budget: int
    concurrency: int
    resume: bool
    transport: httpx.AsyncBaseTransport | None = None
    request_delay: float = REQUEST_DELAY_SECONDS


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    """Result of a prefix crawl.

    ``completed`` means the trie drained without a stop; a budget-limited or throttled run leaves
    the checkpoint in place so ``--resume`` can continue it.
    """

    entries: tuple[RawSuggestion, ...]
    requests: int
    stopped: str | None
    completed: bool


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """CLI-level sync request.

    ``transport`` is a test seam (``httpx.MockTransport``); production runs leave it ``None``.
    """

    root: Path
    kind: CatalogKind
    check: bool
    write: bool
    resume: bool
    budget: int
    concurrency: int
    transport: httpx.AsyncBaseTransport | None = None
    request_delay: float = REQUEST_DELAY_SECONDS


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Per-catalog outcome for the CLI summary."""

    kind: str
    path: Path
    added: int
    removed: int
    total: int
    drifted: bool
    stopped: str | None


class SyncArgs(_FrozenModel):
    """Validated ``sync`` subcommand arguments (converts the argparse namespace)."""

    kind: CatalogKind = "all"
    write: bool = False
    check: bool = False
    resume: bool = False
    budget: int = 0
    concurrency: int = DEFAULT_CONCURRENCY
    root: Path = REPO_ROOT


ParseFn = Callable[[str], Result[list[RawSuggestion], str]]
ExpandFn = Callable[[Sequence[RawSuggestion], int, int], bool]


# =============================================================================
# Boundary parsing
# =============================================================================


def parse_skill_payload(text: str) -> Result[list[RawSuggestion], str]:
    """Decode a skills autocomplete body; an empty body is a valid empty result."""
    if not text:
        return Ok([])
    try:
        payload = SkillPayload.model_validate_json(text)
    except ValidationError as exc:
        return Err(f"skills payload does not match schema: {exc}")
    return Ok([RawSuggestion(key=str(skill.value), title=skill.title, alias=skill.alias) for skill in payload.items])


def parse_location_payload(text: str) -> Result[list[RawSuggestion], str]:
    """Decode a locations autocomplete body; an empty body is a valid empty result."""
    if not text:
        return Ok([])
    try:
        payload = LocationPayload.model_validate_json(text)
    except ValidationError as exc:
        return Err(f"locations payload does not match schema: {exc}")
    return Ok(
        [
            RawSuggestion(
                key=item.value,
                title=item.title,
                region=item.region,
                country=item.country,
                subtitle=item.subtitle,
            )
            for item in payload.items
        ]
    )


def parse_yaml_document[DocumentT: _FrozenModel](text: str, model: type[DocumentT]) -> Result[DocumentT, str]:
    """Validate a YAML catalog document against its model."""
    try:
        validated = model.model_validate(
            yaml.safe_load(text)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; pydantic validates the value
        )
    except yaml.YAMLError as exc:
        return Err(f"invalid YAML: {exc}")
    except ValidationError as exc:
        return Err(f"document does not match schema: {exc}")
    return Ok(validated)


# =============================================================================
# Rendering (deterministic, diff-friendly)
# =============================================================================


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_skills(document: SkillsDoc) -> str:
    """Render skills.yaml text."""
    lines = [
        "# Generated auxiliary catalog of Habr Career skill ids.",
        "# Source: /api/frontend/suggestions/skills?term=<prefix> (full prefix-crawl).",
        "# Refresh: `uv run poe catalog_habr sync --kind skills --write`. See aux/README.md.",
        "",
        "skills:",
    ]
    lines.extend(
        f"  {skill_id}: {{name: {_quote(record.name)}, alias: {_quote(record.alias)}}}"
        for skill_id, record in sorted(document.skills.items())
    )
    return "\n".join(lines) + "\n"


def _render_region(region_id: int, record: RegionRecord) -> str:
    suffix = f", country_id: {record.country_id}" if record.country_id is not None else ""
    return f"  {region_id}: {{name: {_quote(record.name)}{suffix}}}"


def _render_city(city_id: int, record: CityRecord) -> str:
    parts = [f"name: {_quote(record.name)}"]
    if record.region_id is not None:
        parts.append(f"region_id: {record.region_id}")
    if record.country_id is not None:
        parts.append(f"country_id: {record.country_id}")
    return f"  {city_id}: {{{', '.join(parts)}}}"


def _append_section(lines: list[str], header: str, comment: str | None, rows: Iterable[str]) -> None:
    """Append a mapping section; an empty one renders as ``key: {}`` so it round-trips as a mapping."""
    materialized = list(rows)
    lines.append("")
    if materialized:
        lines.append(header)
        if comment is not None:
            lines.append(comment)
        lines.extend(materialized)
        return
    lines.append(f"{header} {{}}")
    if comment is not None:
        lines.append(comment)


def render_locations(document: LocationsDoc) -> str:
    """Render locations.yaml text."""
    lines = [
        "# Generated auxiliary catalog of Habr Career location ids.",
        "# Scope: all countries, Russian regions, Russian cities (see tools/catalog/habr.py).",
        "# Source: /api/frontend/suggestions/locations + /api/frontend/suggestions/countries",
        "#         + `/api/frontend/vacancies` listing scan (in_use_city_ids).",
        "# Refresh: `uv run poe catalog_habr sync --kind locations --write`. See aux/README.md.",
    ]
    _append_section(
        lines,
        "countries:",
        None,
        (f"  {country_id}: {_quote(name)}" for country_id, name in sorted(document.countries.items())),
    )
    _append_section(
        lines,
        "country_slugs:",
        "# From /api/frontend/suggestions/countries (slug -> name).",
        (f"  {_quote(slug)}: {_quote(name)}" for slug, name in sorted(document.country_slugs.items())),
    )
    _append_section(
        lines,
        "regions:",
        "# id -> {name, country_id}",
        (_render_region(region_id, record) for region_id, record in sorted(document.regions.items())),
    )
    _append_section(
        lines,
        "cities:",
        "# id (city_id) -> {name, region_id, country_id}",
        (_render_city(city_id, record) for city_id, record in sorted(document.cities.items())),
    )
    _append_section(
        lines,
        "in_use_city_ids:",
        "# City ids referenced by current vacancies (listing scan).",
        (f"  {city_id}: {_quote(name)}" for city_id, name in sorted(document.in_use_city_ids.items())),
    )
    return "\n".join(lines) + "\n"


def _integer_key(value: str) -> int | None:
    """Return the integer id of a submission key, or None when it is not plain ASCII digits."""
    return int(value) if value.isascii() and value.isdigit() else None


def merge_skills(existing: SkillsDoc, crawled: Sequence[RawSuggestion]) -> SkillsDoc:
    """Union the crawled skills onto the existing document (additive; the catalog only grows)."""
    merged: dict[int, SkillRecord] = dict(existing.skills)
    for item in crawled:
        skill_id = _integer_key(item.key)
        if skill_id is None:
            continue
        merged[skill_id] = SkillRecord(name=item.title, alias=item.alias or "")
    return SkillsDoc(skills=merged)


def merge_locations(
    existing: LocationsDoc,
    crawled: Sequence[RawSuggestion],
    country_slugs: Mapping[str, str],
    in_use: Mapping[int, str] | None,
) -> LocationsDoc:
    """Union crawled locations onto the existing document, keeping only the supported scope.

    Scope is countries (all) + Russian regions + Russian cities; foreign cities/regions are dropped
    even when already present, so the file cannot grow worldwide again. Existing Russian entries are
    kept (union) because a capped crawl can miss them. Country names prefer an authoritative ``ct_``
    title over a city-subtitle guess, collected separately so crawl order cannot change the result.
    """
    cities: dict[int, CityRecord] = {
        key: value for key, value in existing.cities.items() if value.country_id == RU_COUNTRY_ID
    }
    regions: dict[int, RegionRecord] = {
        key: value for key, value in existing.regions.items() if value.country_id == RU_COUNTRY_ID
    }
    countries: dict[int, str] = dict(existing.countries)
    subtitle_country_names: dict[int, str] = {}
    title_country_names: dict[int, str] = {}

    for item in crawled:
        number = _integer_key(item.key.split("_", 1)[1]) if "_" in item.key else None
        if number is None:
            continue
        if item.key.startswith("c_"):
            country_id = (
                _integer_key(item.country.split("_", 1)[1])
                if item.country is not None and "_" in item.country
                else None
            )
            if country_id is not None and item.subtitle:
                # Country names are collected for every country, even one whose cities we drop.
                subtitle_country_names.setdefault(country_id, item.subtitle.split(",", 1)[0].strip())
            if country_id != RU_COUNTRY_ID:
                continue
            region_id = (
                _integer_key(item.region.split("_", 1)[1]) if item.region is not None and "_" in item.region else None
            )
            cities[number] = CityRecord(name=item.title, region_id=region_id, country_id=country_id)
        elif item.key.startswith("r_"):
            if item.subtitle != "Россия":
                continue
            regions[number] = RegionRecord(name=item.title, country_id=RU_COUNTRY_ID)
        elif item.key.startswith("ct_"):
            title_country_names[number] = item.title

    # Subtitle guesses first, authoritative `ct_` titles last.
    for country_id, name in subtitle_country_names.items():
        countries.setdefault(country_id, name)
    countries.update(title_country_names)

    slugs: dict[str, str] = dict(existing.country_slugs)
    slugs.update(country_slugs)
    # An in-use id we cannot name is a dangling reference; keep only the cities we know.
    harvested_in_use = dict(in_use) if in_use is not None else dict(existing.in_use_city_ids)
    in_use_cities: Mapping[int, str] = {key: value for key, value in harvested_in_use.items() if key in cities}
    return LocationsDoc(
        countries=countries,
        country_slugs=slugs,
        regions=regions,
        cities=cities,
        in_use_city_ids=in_use_cities,
    )


def validate_skills(document: SkillsDoc) -> Result[None, str]:
    """Refuse to write a skills catalog that lost its anchor."""
    if ANCHOR_SKILL_ID not in document.skills:
        return Err(f"anchor skill {ANCHOR_SKILL_ID} missing — aborting instead of writing a broken catalog")
    return Ok(None)


def validate_locations(document: LocationsDoc) -> Result[None, str]:
    """Refuse to write a locations catalog that lost its anchors."""
    if ANCHOR_CITY_ID not in document.cities:
        return Err(f"anchor city {ANCHOR_CITY_ID} missing — aborting instead of writing a broken catalog")
    if ANCHOR_COUNTRY_ID not in document.countries:
        return Err(f"anchor country {ANCHOR_COUNTRY_ID} missing — aborting instead of writing a broken catalog")
    return Ok(None)


# =============================================================================
# Crawl
# =============================================================================


def should_expand_skills(items: Sequence[RawSuggestion], depth: int, new: int) -> bool:
    """Deepen a skills prefix while it is capped and still adding entries."""
    return len(items) >= RESULT_CAP and new > 0 and depth < SKILL_MAX_DEPTH


def should_expand_locations(items: Sequence[RawSuggestion], depth: int, new: int) -> bool:
    """Deepen only Russian branches — foreign branches are never expanded (single supported scope).

    To widen the scope, drop the ``RU_COUNTRY_KEY`` check (worldwide) or parameterize the country
    here and in ``merge_locations``; no other code depends on the scope.
    """
    if new == 0:
        return False
    return any(item.country == RU_COUNTRY_KEY for item in items) and depth < RU_MAX_DEPTH


def checkpoint_path(kind: CrawlKind) -> Path:
    """Temp checkpoint file for a resumable crawl (never inside the repo)."""
    return Path(tempfile.gettempdir()) / f"jobfucker-habr-catalog-{kind}.json"


async def _fetch(client: httpx.AsyncClient, endpoint: str, term: str) -> Result[str, str]:
    """Fetch one autocomplete page. ``Err("stop:<code>")`` means the board is throttling/blocking."""
    last_error = "transport: unknown error"
    for attempt in range(FETCH_ATTEMPTS):
        try:
            response = await client.get(endpoint, params={"term": term})
        except httpx.HTTPError as exc:
            last_error = f"transport: {exc}"
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code in (403, 429):
            return Err(f"stop:{response.status_code}")
        if response.status_code >= HTTP_SERVER_ERROR:
            # 5xx is transient; retry instead of abandoning a multi-minute crawl on one blip.
            last_error = f"server:{response.status_code}"
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code != HTTP_OK:
            return Ok("")
        return Ok(response.text)
    return Err(last_error)


async def _load_checkpoint(kind: CrawlKind) -> CrawlState | None:
    path = checkpoint_path(kind)
    try:
        text = await anyio.Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return CrawlState.model_validate_json(text)
    except ValidationError:
        return None


async def _save_checkpoint(kind: CrawlKind, state: CrawlState) -> None:
    path = checkpoint_path(kind)
    try:
        await anyio.Path(path).write_text(state.model_dump_json(), encoding="utf-8")
    except OSError:
        return


async def _delete_checkpoint(kind: CrawlKind) -> None:
    try:
        await anyio.Path(checkpoint_path(kind)).unlink()
    except OSError:
        return


async def crawl(
    config: CrawlConfig,
    parse: ParseFn,
    should_expand: ExpandFn,
) -> CrawlOutcome:
    """Walk the prefix trie of an autocomplete endpoint single-digit-concurrency at a time."""
    state = await _load_checkpoint(config.kind) if config.resume else None
    queue: list[tuple[str, int]] = list(state.queue) if state is not None else [(char, 0) for char in ALPHABET]
    found: dict[str, RawSuggestion] = {entry.key: entry for entry in state.entries} if state is not None else {}
    requests = state.requests if state is not None else 0
    stopped: str | None = None
    lock = asyncio.Lock()

    limits = httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency)
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=dict(HTTP_HEADERS),
        timeout=REQUEST_TIMEOUT_SECONDS,
        limits=limits,
        follow_redirects=True,
        transport=config.transport,
    ) as client:

        async def stop_with_checkpoint(reason: str) -> None:
            """Record the abort reason and keep the remaining frontier for ``--resume``."""
            nonlocal stopped
            async with lock:
                stopped = reason
                await _save_checkpoint(
                    config.kind, CrawlState(queue=list(queue), entries=list(found.values()), requests=requests)
                )

        async def worker() -> None:
            nonlocal requests
            while True:
                async with lock:
                    if stopped is not None or not queue or requests >= config.budget:
                        return
                    term, depth = queue.pop(0)
                    requests += 1
                fetched = await _fetch(client, config.endpoint, term)
                if fetched.is_err:
                    recovered = fetched.unwrap_err()
                    await stop_with_checkpoint(recovered if recovered.startswith("stop:") else f"fetch:{recovered}")
                    return
                parsed = parse(fetched.unwrap())
                if parsed.is_err:
                    # A schema change must abort, not silently truncate the catalog.
                    await stop_with_checkpoint(f"parse:{parsed.unwrap_err()}")
                    return
                items = parsed.unwrap()
                async with lock:
                    new = 0
                    for item in items:
                        if item.key not in found:
                            found[item.key] = item
                            new += 1
                    if items and should_expand(items, depth, new):
                        queue.extend((term + char, depth + 1) for char in ALPHABET)
                    if requests % CHECKPOINT_EVERY_REQUESTS == 0:
                        await _save_checkpoint(
                            config.kind,
                            CrawlState(queue=list(queue), entries=list(found.values()), requests=requests),
                        )
                await asyncio.sleep(config.request_delay)

        await asyncio.gather(*(worker() for _ in range(config.concurrency)))

    completed = stopped is None and not queue
    if completed:
        await _delete_checkpoint(config.kind)
    else:
        # Budget-limited or throttled: keep the frontier so --resume can continue.
        await _save_checkpoint(
            config.kind, CrawlState(queue=list(queue), entries=list(found.values()), requests=requests)
        )
    return CrawlOutcome(entries=tuple(found.values()), requests=requests, stopped=stopped, completed=completed)


async def harvest_in_use_city_ids(client: httpx.AsyncClient) -> Result[tuple[Mapping[int, str], bool], str]:
    """Collect the city ids referenced by current vacancies.

    The second element reports whether the scan finished; an incomplete scan must not replace the
    existing in-use set (that would silently shrink it).
    """
    found: dict[int, str] = {}
    completed = False
    for page in range(1, IN_USE_MAX_PAGES + 1):
        try:
            response = await client.get(
                VACANCIES_ENDPOINT, params={"type": "all", "per_page": IN_USE_PAGE_SIZE, "page": page}
            )
        except httpx.HTTPError as exc:
            return Err(f"transport: {exc}")
        if response.status_code != HTTP_OK:
            break
        try:
            payload = ListingPayload.model_validate_json(response.text)
        except ValidationError as exc:
            return Err(f"listing payload does not match schema: {exc}")
        for item in payload.items:
            for location in item.locations or []:
                city_id = city_id_from_href(location.href)
                if city_id is not None:
                    found[city_id] = location.title
        if len(payload.items) < IN_USE_PAGE_SIZE:
            completed = True
            break
    return Ok((found, completed))


def city_id_from_href(href: str) -> int | None:
    """Extract ``city_id`` from a listing location href."""
    marker = "city_id="
    if marker not in href:
        return None
    raw = href.split(marker, 1)[1].split("&", 1)[0]
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _resolve_budget(config: CrawlConfig) -> CrawlConfig:
    if config.budget > 0:
        return config
    default_budget = DEFAULT_SKILL_BUDGET if config.kind == "skills" else DEFAULT_LOCATION_BUDGET
    return CrawlConfig(
        kind=config.kind,
        endpoint=config.endpoint,
        budget=default_budget,
        concurrency=config.concurrency,
        resume=config.resume,
        transport=config.transport,
        request_delay=config.request_delay,
    )


async def crawl_catalog(config: CrawlConfig, parse: ParseFn, should_expand: ExpandFn) -> CrawlOutcome:
    """Resolve the budget, then crawl."""
    return await crawl(_resolve_budget(config), parse, should_expand)


# =============================================================================
# Sync
# =============================================================================


def read_existing_text(path: Path) -> Result[str, str]:
    """Read a catalog file; a missing file is an empty bootstrap, a read failure is an error."""
    try:
        return Ok(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Ok("")
    except OSError as exc:
        return Err(f"cannot read {path}: {exc}")


def _write_text_atomic(path: Path, text: str) -> Result[None, str]:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        return Err(f"cannot write {path}: {exc}")
    return Ok(None)


def _drift(path: Path, text: str, config: SyncConfig) -> Result[bool, str]:
    """Report whether the rendered text differs; write it when asked."""
    current = read_existing_text(path)
    if current.is_err:
        return Err(current.unwrap_err())
    drifted = current.unwrap() != text
    if config.write and drifted:
        written = _write_text_atomic(path, text)
        if written.is_err:
            return Err(written.unwrap_err())
    return Ok(drifted)


def load_skills_document(path: Path) -> Result[SkillsDoc, str]:
    """Load skills.yaml, treating a missing/empty file as an empty catalog (bootstrap)."""
    text = read_existing_text(path)
    if text.is_err:
        return Err(text.unwrap_err())
    if not text.unwrap().strip():
        return Ok(SkillsDoc(skills={}))
    return parse_yaml_document(text.unwrap(), SkillsDoc)


def load_locations_document(path: Path) -> Result[LocationsDoc, str]:
    """Load locations.yaml, treating a missing/empty file as an empty catalog (bootstrap)."""
    text = read_existing_text(path)
    if text.is_err:
        return Err(text.unwrap_err())
    if not text.unwrap().strip():
        return Ok(LocationsDoc(countries={}, country_slugs={}, regions={}, cities={}, in_use_city_ids={}))
    return parse_yaml_document(text.unwrap(), LocationsDoc)


def sync_skills(config: SyncConfig) -> Result[SyncReport, str]:
    """Refresh the skills catalog."""
    path = config.root / SKILLS_RELATIVE_PATH
    loaded = load_skills_document(path)
    if loaded.is_err:
        return Err(loaded.unwrap_err())
    existing = loaded.unwrap()

    outcome = asyncio.run(
        crawl_catalog(
            CrawlConfig(
                kind="skills",
                endpoint=SKILLS_ENDPOINT,
                budget=config.budget,
                concurrency=config.concurrency,
                resume=config.resume,
                transport=config.transport,
                request_delay=config.request_delay,
            ),
            parse_skill_payload,
            should_expand_skills,
        )
    )
    merged = merge_skills(existing, outcome.entries)
    valid = validate_skills(merged)
    if valid.is_err:
        return Err(valid.unwrap_err())

    drifted = _drift(path, render_skills(merged), config)
    if drifted.is_err:
        return Err(drifted.unwrap_err())
    added = len({key for key in merged.skills if key not in existing.skills})
    return Ok(
        SyncReport(
            kind="skills",
            path=path,
            added=added,
            removed=0,
            total=len(merged.skills),
            drifted=drifted.unwrap(),
            stopped=outcome.stopped,
        )
    )


def sync_locations(config: SyncConfig, *, only_in_use: bool) -> Result[SyncReport, str]:
    """Refresh the locations catalog, or only its volatile in-use city subset."""
    path = config.root / LOCATIONS_RELATIVE_PATH
    loaded = load_locations_document(path)
    if loaded.is_err:
        return Err(loaded.unwrap_err())
    existing = loaded.unwrap()

    harvested = asyncio.run(_harvest_locations(config, only_in_use=only_in_use))
    if harvested.is_err:
        return Err(harvested.unwrap_err())
    crawled, country_slugs, in_use, stopped = harvested.unwrap()

    merged = merge_locations(existing, crawled, country_slugs, in_use)
    if not only_in_use:
        valid = validate_locations(merged)
        if valid.is_err:
            return Err(valid.unwrap_err())

    drifted = _drift(path, render_locations(merged), config)
    if drifted.is_err:
        return Err(drifted.unwrap_err())
    added = len({key for key in merged.cities if key not in existing.cities})
    # Non-zero only when the scope filter drops foreign entries carried by an older file.
    removed = len([key for key in existing.cities if key not in merged.cities])
    return Ok(
        SyncReport(
            kind="in-use" if only_in_use else "locations",
            path=path,
            added=added,
            removed=removed,
            total=len(merged.cities),
            drifted=drifted.unwrap(),
            stopped=stopped,
        )
    )


async def _harvest_locations(
    config: SyncConfig, *, only_in_use: bool
) -> Result[tuple[tuple[RawSuggestion, ...], Mapping[str, str], Mapping[int, str] | None, str | None], str]:
    """Crawl locations (unless in-use-only), the country slug list, and the in-use city ids.

    ``in_use`` is ``None`` when the listing scan did not finish, so the caller keeps the existing set.
    """
    limits = httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency)
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=dict(HTTP_HEADERS),
        timeout=REQUEST_TIMEOUT_SECONDS,
        limits=limits,
        follow_redirects=True,
        transport=config.transport,
    ) as client:
        country_slugs: Mapping[str, str] = {}
        if not only_in_use:
            try:
                countries_response = await client.get(COUNTRIES_ENDPOINT)
            except httpx.HTTPError as exc:
                return Err(f"transport: {exc}")
            if countries_response.status_code == HTTP_OK:
                try:
                    countries = COUNTRY_ADAPTER.validate_json(countries_response.text)
                except ValidationError as exc:
                    return Err(f"countries payload does not match schema: {exc}")
                country_slugs = {country.value: country.title for country in countries}

        in_use_result = await harvest_in_use_city_ids(client)
        if in_use_result.is_err:
            return Err(in_use_result.unwrap_err())
        harvested_in_use, in_use_completed = in_use_result.unwrap()
        if only_in_use and not in_use_completed:
            return Err("in-use listing scan did not finish; keeping the previous set")
        in_use: Mapping[int, str] | None = harvested_in_use if in_use_completed else None

    if only_in_use:
        return Ok(((), country_slugs, in_use, None))

    outcome = await crawl_catalog(
        CrawlConfig(
            kind="locations",
            endpoint=LOCATIONS_ENDPOINT,
            budget=config.budget,
            concurrency=config.concurrency,
            resume=config.resume,
            transport=config.transport,
            request_delay=config.request_delay,
        ),
        parse_location_payload,
        should_expand_locations,
    )
    return Ok((outcome.entries, country_slugs, in_use, outcome.stopped))


# =============================================================================
# CLI
# =============================================================================


def run_sync(config: SyncConfig) -> int:
    """Run the requested sync(s) and return the process exit code."""
    kinds: tuple[CatalogKind, ...] = ("skills", "locations") if config.kind == "all" else (config.kind,)
    reports: list[SyncReport] = []
    for kind in kinds:
        if kind == "skills":
            outcome = sync_skills(config)
        elif kind == "in-use":
            outcome = sync_locations(config, only_in_use=True)
        else:
            outcome = sync_locations(config, only_in_use=False)
        if outcome.is_err:
            print(f"error: {outcome.unwrap_err()}", file=sys.stderr)
            return 2
        reports.append(outcome.unwrap())

    for report in reports:
        marker = "drift" if report.drifted else "clean"
        stopped = f" stopped={report.stopped}" if report.stopped else ""
        action = "wrote" if config.write and report.drifted else marker
        print(
            f"{report.kind}: {report.total} entries (+{report.added} -{report.removed}) "
            f"{report.path} [{action}]{stopped}"
        )
    stopped_reports = [report for report in reports if report.stopped]
    if stopped_reports:
        reasons = ", ".join(f"{report.kind}={report.stopped}" for report in stopped_reports)
        print(f"warning: crawl aborted ({reasons}); checkpoint kept for --resume", file=sys.stderr)
        return 2
    if config.check and any(report.drifted for report in reports):
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalog_habr",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync", help="refresh the auxiliary catalogs")
    sync.add_argument("--kind", choices=("skills", "locations", "in-use", "all"), default="all")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write changed catalogs")
    mode.add_argument("--check", action="store_true", help="exit 1 when the catalogs are stale")
    sync.add_argument("--resume", action="store_true", help="continue from the temp checkpoint if present")
    sync.add_argument("--budget", type=int, default=0, help="max requests per crawl (0 = per-kind default)")
    sync.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    sync.add_argument("--root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: Sequence[str]) -> int:
    """CLI entry point."""
    namespace = _build_parser().parse_args(argv)
    try:
        args = SyncArgs.model_validate(namespace, from_attributes=True)
    except ValidationError as exc:
        print(f"error: invalid arguments: {exc}", file=sys.stderr)
        return 2
    config = SyncConfig(
        root=args.root,
        kind=args.kind,
        check=args.check,
        write=args.write,
        resume=args.resume,
        budget=args.budget,
        concurrency=max(1, args.concurrency),
    )
    return run_sync(config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
