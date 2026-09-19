"""Offline tests for the Habr Career catalog tool (parse/merge/render/validate logic).

No network: the crawl is exercised manually; these tests pin the deterministic parts that a
refresh run depends on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from tools.catalog import habr as habr_module
from tools.catalog.habr import (
    ALPHABET,
    IN_USE_PAGE_SIZE,
    LOCATIONS_RELATIVE_PATH,
    SKILLS_ENDPOINT,
    SKILLS_RELATIVE_PATH,
    CatalogKind,
    CityRecord,
    CrawlConfig,
    CrawlKind,
    LocationsDoc,
    RawSuggestion,
    RegionRecord,
    SkillRecord,
    SkillsDoc,
    SyncConfig,
    city_id_from_href,
    crawl,
    load_skills_document,
    merge_locations,
    merge_skills,
    parse_location_payload,
    parse_skill_payload,
    parse_yaml_document,
    render_locations,
    render_skills,
    should_expand_locations,
    should_expand_skills,
    sync_locations,
    sync_skills,
    validate_locations,
    validate_skills,
)


def _skills(*ids: int) -> SkillsDoc:
    return SkillsDoc(
        skills={skill_id: SkillRecord(name=f"skill-{skill_id}", alias=f"skill-{skill_id}") for skill_id in ids}
    )


def _empty_locations() -> LocationsDoc:
    return LocationsDoc(countries={}, country_slugs={}, regions={}, cities={}, in_use_city_ids={})


def test_parse_skill_payload_maps_list_alias() -> None:
    result = parse_skill_payload('{"list": [{"value": 446, "title": "Python", "alias": "python"}]}')
    assert not result.is_err
    assert result.unwrap() == [RawSuggestion(key="446", title="Python", alias="python")]


def test_parse_skill_payload_rejects_missing_fields() -> None:
    assert parse_skill_payload('{"list": [{"title": "no id"}]}').is_err


def test_parse_location_payload_reads_entity_fields() -> None:
    body = (
        '{"list": [{"value": "c_678", "title": "Москва", '
        '"subtitle": "Россия, Москва и Московская область", "region": "r_14068", "country": "ct_444"}]}'
    )
    result = parse_location_payload(body)
    assert not result.is_err
    item = result.unwrap()[0]
    assert item.key == "c_678"
    assert item.region == "r_14068"
    assert item.country == "ct_444"


def test_parse_yaml_document_rejects_wrong_shape() -> None:
    assert parse_yaml_document("skills: 5", SkillsDoc).is_err


def test_merge_skills_is_additive_by_default() -> None:
    merged = merge_skills(_skills(1), [RawSuggestion(key="446", title="Python", alias="python")])
    assert set(merged.skills) == {1, 446}


def test_render_skills_is_deterministic_and_round_trips() -> None:
    document = _skills(446)
    text = render_skills(document)
    assert text == render_skills(document)
    assert parse_yaml_document(text, SkillsDoc).unwrap() == document


def test_render_locations_round_trips() -> None:
    document = LocationsDoc(
        countries={444: "Россия"},
        country_slugs={"russia": "Россия"},
        regions={14068: RegionRecord(name="Москва и Московская область", country_id=444)},
        cities={678: CityRecord(name="Москва", region_id=14068, country_id=444)},
        in_use_city_ids={678: "Москва"},
    )
    assert parse_yaml_document(render_locations(document), LocationsDoc).unwrap() == document


def test_validate_skills_requires_anchor() -> None:
    assert validate_skills(SkillsDoc(skills={})).is_err
    assert not validate_skills(_skills(446)).is_err


def test_validate_locations_requires_anchors() -> None:
    assert validate_locations(_empty_locations()).is_err
    document = LocationsDoc(
        countries={444: "Россия"},
        country_slugs={},
        regions={},
        cities={678: CityRecord(name="Москва")},
        in_use_city_ids={},
    )
    assert not validate_locations(document).is_err


def test_should_expand_skills_only_when_capped_and_discovering() -> None:
    capped = [RawSuggestion(key=str(index), title="x", alias="y") for index in range(25)]
    assert should_expand_skills(capped, 0, 1)
    assert not should_expand_skills(capped, 0, 0)
    assert not should_expand_skills(capped[:3], 0, 1)
    assert not should_expand_skills(capped, 4, 1)


def test_should_expand_locations_only_follows_russian_branches() -> None:
    russian = [RawSuggestion(key="c_1", title="x", country="ct_444")]
    foreign_capped = [RawSuggestion(key=f"c_{i}", title="y", country="ct_2116") for i in range(25)]
    assert should_expand_locations(russian, 5, 1)
    assert not should_expand_locations(russian, 6, 1)
    assert not should_expand_locations(foreign_capped, 0, 1)
    assert not should_expand_locations(russian, 0, 0)


def test_merge_locations_links_city_and_region_to_country() -> None:
    crawled = [
        RawSuggestion(
            key="c_678",
            title="Москва",
            region="r_14068",
            country="ct_444",
            subtitle="Россия, Москва и Московская область",
        ),
        RawSuggestion(key="r_14068", title="Москва и Московская область", subtitle="Россия"),
    ]
    merged = merge_locations(_empty_locations(), crawled, {"russia": "Россия"}, {678: "Москва"})

    assert merged.countries[444] == "Россия"
    assert merged.regions[14068].country_id == 444
    assert merged.cities[678] == CityRecord(name="Москва", region_id=14068, country_id=444)
    assert merged.in_use_city_ids == {678: "Москва"}
    assert merged.country_slugs == {"russia": "Россия"}


def test_merge_locations_keeps_in_use_when_not_harvested() -> None:
    existing = LocationsDoc(
        countries={444: "Россия"},
        country_slugs={},
        regions={},
        cities={678: CityRecord(name="Москва", country_id=444)},
        in_use_city_ids={678: "Москва"},
    )
    merged = merge_locations(existing, (), {}, None)
    assert merged.in_use_city_ids == {678: "Москва"}


def test_merge_locations_drops_foreign_and_keeps_russian() -> None:
    existing = LocationsDoc(
        countries={444: "Россия", 2111: "Франция"},
        country_slugs={},
        regions={14068: RegionRecord(name="Москва и Московская область", country_id=444)},
        cities={
            678: CityRecord(name="Москва", region_id=14068, country_id=444),
            3740: CityRecord(name="Париж", region_id=7629, country_id=2111),
        },
        in_use_city_ids={},
    )
    crawled = [
        RawSuggestion(key="c_14123", title="Абаза", region="r_14096", country="ct_444", subtitle="Россия, Хакасия"),
        RawSuggestion(
            key="c_3741", title="Анси", region="r_7629", country="ct_2111", subtitle="Франция, Франция - все регионы"
        ),
        RawSuggestion(key="r_14096", title="Хакасия", subtitle="Россия"),
        RawSuggestion(key="r_7629", title="Франция - все регионы", subtitle="Франция"),
        RawSuggestion(key="ct_2111", title="Франция"),
    ]
    merged = merge_locations(existing, crawled, {}, None)

    assert set(merged.cities) == {678, 14123}
    assert set(merged.regions) == {14068, 14096}
    # Country rows are scope-free: both countries stay, and the French name is captured.
    assert merged.countries[444] == "Россия"
    assert merged.countries[2111] == "Франция"


def test_merge_locations_filters_unknown_in_use_ids() -> None:
    existing = LocationsDoc(
        countries={444: "Россия"},
        country_slugs={},
        regions={},
        cities={678: CityRecord(name="Москва", country_id=444)},
        in_use_city_ids={678: "Москва"},
    )
    merged = merge_locations(existing, (), {}, {678: "Москва", 3740: "Париж"})
    assert merged.in_use_city_ids == {678: "Москва"}


def test_city_id_from_href() -> None:
    assert city_id_from_href("/vacancies?city_id=678") == 678
    assert city_id_from_href("/vacancies?q=python") is None
    assert city_id_from_href("/vacancies?city_id=abc") is None


# =============================================================================
# Crawl mechanics against a scripted transport (no network)
# =============================================================================


def _checkpoint_in(directory: Path) -> Callable[[CrawlKind], Path]:
    """Typed stand-in for ``checkpoint_path`` so tests write checkpoints into ``tmp_path``."""

    def checkpoint_path(kind: CrawlKind) -> Path:
        return directory / f"{kind}.json"

    return checkpoint_path


def _suggestion_handler(next_id: list[int]) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that answers every skills term with one fresh, non-capped row."""

    def handler(request: httpx.Request) -> httpx.Response:
        row = {"value": next_id[0], "title": f"skill-{next_id[0]}", "alias": f"skill-{next_id[0]}"}
        next_id[0] += 1
        return httpx.Response(200, json={"list": [row]})

    return handler


def _crawl_config(transport: httpx.AsyncBaseTransport, budget: int) -> CrawlConfig:
    return CrawlConfig(
        kind="skills",
        endpoint=SKILLS_ENDPOINT,
        budget=budget,
        concurrency=1,
        resume=False,
        transport=transport,
        request_delay=0.0,
    )


def test_crawl_completes_and_removes_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    outcome = asyncio.run(
        crawl(
            _crawl_config(httpx.MockTransport(_suggestion_handler([1000])), budget=1000),
            parse_skill_payload,
            should_expand_skills,
        )
    )
    assert outcome.completed
    assert outcome.stopped is None
    assert len(outcome.entries) == len(ALPHABET)
    assert not (tmp_path / "skills.json").exists()


def test_crawl_stops_on_throttle_and_keeps_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    outcome = asyncio.run(
        crawl(_crawl_config(httpx.MockTransport(handler), budget=1000), parse_skill_payload, should_expand_skills)
    )
    assert outcome.stopped == "stop:403"
    assert not outcome.completed
    assert (tmp_path / "skills.json").exists()


def test_crawl_budget_exhaustion_keeps_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    outcome = asyncio.run(
        crawl(
            _crawl_config(httpx.MockTransport(_suggestion_handler([1000])), budget=5),
            parse_skill_payload,
            should_expand_skills,
        )
    )
    assert outcome.stopped is None
    assert not outcome.completed
    assert outcome.requests == 5
    assert (tmp_path / "skills.json").exists()


def test_crawl_aborts_on_schema_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    outcome = asyncio.run(
        crawl(_crawl_config(httpx.MockTransport(handler), budget=1000), parse_skill_payload, should_expand_skills)
    )
    assert outcome.stopped is not None
    assert outcome.stopped.startswith("parse:")
    assert not outcome.completed


def test_crawl_resumes_from_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    first = asyncio.run(
        crawl(
            _crawl_config(httpx.MockTransport(_suggestion_handler([1000])), budget=5),
            parse_skill_payload,
            should_expand_skills,
        )
    )
    assert not first.completed
    assert len(first.entries) == 5

    resumed_terms: list[str] = []
    resume_handler = _suggestion_handler([5000])

    def handler(request: httpx.Request) -> httpx.Response:
        resumed_terms.append(str(request.url.params["term"]))
        return resume_handler(request)

    second = asyncio.run(
        crawl(
            CrawlConfig(
                kind="skills",
                endpoint=SKILLS_ENDPOINT,
                budget=1000,
                concurrency=1,
                resume=True,
                transport=httpx.MockTransport(handler),
                request_delay=0.0,
            ),
            parse_skill_payload,
            should_expand_skills,
        )
    )
    assert second.completed
    assert len(resumed_terms) == len(ALPHABET) - 5
    assert len(second.entries) == len(ALPHABET)


# =============================================================================
# Sync orchestration (offline)
# =============================================================================


def _sync_config(
    root: Path,
    *,
    kind: CatalogKind = "skills",
    write: bool = False,
    check: bool = False,
    resume: bool = False,
    budget: int = 1000,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SyncConfig:
    return SyncConfig(
        root=root,
        kind=kind,
        check=check,
        write=write,
        resume=resume,
        budget=budget,
        concurrency=1,
        transport=transport,
        request_delay=0.0,
    )


def _seed_skills(root: Path, *ids: int) -> Path:
    path = root / SKILLS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_skills(_skills_doc(*ids)), encoding="utf-8")
    return path


def _skills_doc(*ids: int) -> SkillsDoc:
    return SkillsDoc(
        skills={skill_id: SkillRecord(name=f"skill-{skill_id}", alias=f"skill-{skill_id}") for skill_id in ids}
    )


def test_sync_skills_adds_new_entries_and_keeps_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    path = _seed_skills(tmp_path, 446)
    config = _sync_config(tmp_path, write=True, transport=httpx.MockTransport(_suggestion_handler([9000])))

    report = sync_skills(config).unwrap()
    assert report.added == len(ALPHABET)
    written = load_skills_document(path).unwrap()
    assert 446 in written.skills
    assert 9000 in written.skills


def test_harvest_in_use_requires_a_finished_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    _seed_skills(tmp_path, 446)

    def handler(request: httpx.Request) -> httpx.Response:
        items = [
            {"locations": [{"title": "Москва", "href": "/vacancies?city_id=678"}]} for _ in range(IN_USE_PAGE_SIZE)
        ]
        return httpx.Response(200, json={"list": items})

    config = _sync_config(tmp_path, kind="in-use", transport=httpx.MockTransport(handler))
    result = sync_locations(config, only_in_use=True)
    assert result.is_err


def _locations_handler(
    rows: Sequence[Mapping[str, str]], in_use_ids: list[int]
) -> Callable[[httpx.Request], httpx.Response]:
    """Scripted board: location suggestions, country list, and one listing page."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/suggestions/countries"):
            return httpx.Response(200, json=[{"value": "russia", "title": "Россия"}])
        if path.endswith("/suggestions/locations"):
            return httpx.Response(200, json={"list": rows})
        if path.endswith("/vacancies"):
            items = [
                {"locations": [{"title": f"city-{city_id}", "href": f"/vacancies?city_id={city_id}"}]}
                for city_id in in_use_ids
            ]
            return httpx.Response(200, json={"list": items})
        return httpx.Response(404)

    return handler


def test_sync_locations_drops_foreign_entries_and_scopes_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(habr_module, "checkpoint_path", _checkpoint_in(tmp_path))
    path = tmp_path / LOCATIONS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_locations(
            LocationsDoc(
                countries={444: "Россия", 2111: "Франция"},
                country_slugs={},
                regions={},
                cities={
                    678: CityRecord(name="Москва", country_id=444),
                    3740: CityRecord(name="Париж", country_id=2111),
                },
                in_use_city_ids={3740: "Париж"},
            )
        ),
        encoding="utf-8",
    )
    rows: list[Mapping[str, str]] = [
        {"value": "c_678", "title": "Москва", "region": "r_14068", "country": "ct_444", "subtitle": "Россия, Москва"},
        {
            "value": "c_3741",
            "title": "Анси",
            "region": "r_7629",
            "country": "ct_2111",
            "subtitle": "Франция, Все регионы",
        },
        {"value": "ct_444", "title": "Россия"},
    ]
    transport = httpx.MockTransport(_locations_handler(rows, [678, 3740]))
    config = _sync_config(tmp_path, kind="locations", write=True, transport=transport)

    report = sync_locations(config, only_in_use=False).unwrap()
    written = parse_yaml_document(path.read_text(encoding="utf-8"), LocationsDoc).unwrap()

    assert set(written.cities) == {678}
    assert written.in_use_city_ids == {678: "city-678"}
    assert report.removed == 1
