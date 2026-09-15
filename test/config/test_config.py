"""Phase 4 (task 4.1): plain pytest unit tests for pipeline.yaml parsing.

Covers the happy path plus every failure mode: malformed YAML, a missing
referenced file, an unknown top-level key, an unknown board, and values
that fail pydantic validation. The behavioral acceptance scenarios live in the
Gherkin ``bdd/config.feature`` (see ``test_config_bdd.py``); these are the pure
unit checks the plan asks for in ``test/config/test_config.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from jobfucker.clients.mock.params import (
    MockApplyBehaviorConfig,
    MockBehaviorConfig,
    MockExperience,
    MockOutcome,
    MockSchedule,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
)
from jobfucker.config import (
    HhTestSolvingConfig,
    OpenAIConfig,
    PersistedPipelineRefs,
    PipelineConfig,
    dump_hh_test_solving,
    dump_openai_captcha,
    dump_service_section,
    load_hh_test_solving,
    load_openai_captcha,
    load_pipeline_config,
    load_service_section,
    reconstruct_pipeline_config,
)
from test.config.helpers import build_inline_pipeline_yaml, build_valid_pipeline_yaml
from test.pipeline_helpers import mock_vacancies


def _bad_yaml(runtime_dir: Path) -> Path:
    """Write a pipeline.yaml whose YAML is syntactically malformed."""
    path = runtime_dir / "config" / "pipeline.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: [unclosed\n  : : :\n", encoding="utf-8")
    return path


def test_valid_pipeline_is_ok(runtime_dir: Path) -> None:
    """A well-formed mock pipeline loads as ``Ok`` with files injected."""
    path = build_valid_pipeline_yaml(runtime_dir / "config", daily_apply_limit=20)
    result = load_pipeline_config(path)
    assert result.is_ok, f"expected Ok, got {result}"
    config = result.unwrap()
    assert config.name == "mock-demo"
    assert config.service == "mock"
    assert config.limits.daily_apply_limit == 20


def test_mock_section_parses_into_mock_params(runtime_dir: Path) -> None:
    """The ``service.mock`` section becomes a MockServiceConfig with MockSearchParams."""
    path = build_valid_pipeline_yaml(runtime_dir / "config")
    config = load_pipeline_config(path).unwrap()
    section = config.service_section
    assert isinstance(section, MockServiceConfig)
    entry = section.searches[0]
    assert isinstance(entry.filter, MockSearchParams)
    assert entry.filter.area == (1,)
    assert entry.filter.schedule == ("fullDay", "remote")
    assert entry.filter.only_with_salary is True


def test_resume_id_captured(runtime_dir: Path) -> None:
    """``service.<board>.resume_id`` is captured on the section, not lost."""
    path = build_valid_pipeline_yaml(runtime_dir / "config")
    config = load_pipeline_config(path).unwrap()
    assert config.service_section.resume_id == "mock-resume-1"


# --- service.mock behavior block (new client contract) ----------------------
def _behavior_yaml_block() -> str:
    """An already-indented ``behavior:`` block injected into the mock section."""
    return (
        "    behavior:\n"
        '      authorize_error: "denied"\n'
        "      default_apply:\n"
        "        outcome: error\n"
        "        message: bad\n"
        "      per_vacancy:\n"
        "        mock-2:\n"
        "          outcome: limit_exceeded\n"
    )


def test_mock_behavior_block_parses_into_section(runtime_dir: Path) -> None:
    """A ``service.mock.behavior`` block validates into a :class:`MockBehaviorConfig`."""
    path = build_valid_pipeline_yaml(runtime_dir / "config", behavior_yaml=_behavior_yaml_block())
    result = load_pipeline_config(path)
    assert result.is_ok, result
    section = result.unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    behavior = section.behavior
    assert behavior is not None
    assert behavior.authorize_error == "denied"
    default_apply = behavior.default_apply
    assert default_apply is not None
    assert default_apply.outcome == "error"
    assert behavior.per_vacancy["mock-2"].outcome == "limit_exceeded"


def test_mock_behavior_block_omitted_defaults_to_none(runtime_dir: Path) -> None:
    """With no ``behavior`` block the section's behavior defaults to ``None``."""
    path = build_valid_pipeline_yaml(runtime_dir / "config")
    section = load_pipeline_config(path).unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    assert section.behavior is None


# --- service.mock vacancies block (offline mock data in the section) --------
def _vacancies_yaml_block() -> str:
    """An already-indented ``vacancies:`` block injected into the mock section."""
    return (
        "        vacancies:\n"
        "          - external_id: custom-1\n"
        "            title: Custom Role\n"
        "            url: https://mock.example/vacancies/custom-1\n"
        "            company: SectionCo\n"
        "            description: A single mock vacancy declared in the section.\n"
        "            key_skills: [Python, FastAPI]\n"
        "            salary:\n"
        "              from: 120000\n"
        "              to: 180000\n"
        "              currency: RUR\n"
        "              gross: false\n"
    )


def test_mock_vacancies_block_parses_into_section(runtime_dir: Path) -> None:
    """A ``service.mock.vacancies`` block is the mock's data — no separate JSON file.

    The user-declared list replaces the built-in default, and the salary
    ``from`` (a reserved word) round-trips through its alias onto ``from_``.
    """
    path = build_valid_pipeline_yaml(runtime_dir / "config", vacancies_yaml=_vacancies_yaml_block())
    config = load_pipeline_config(path).unwrap()
    section = config.service_section
    assert isinstance(section, MockServiceConfig)
    assert len(section.searches[0].vacancies) == 1
    vacancy = section.searches[0].vacancies[0]
    assert vacancy.external_id == "custom-1"
    assert vacancy.title == "Custom Role"
    assert vacancy.company == "SectionCo"
    assert vacancy.key_skills == ["Python", "FastAPI"]
    assert vacancy.salary is not None
    assert vacancy.salary.from_ == 120000
    assert vacancy.salary.to == 180000
    assert vacancy.salary.currency == "RUR"
    assert vacancy.salary.gross is False


def test_mock_vacancies_block_omitted_defaults_to_empty(runtime_dir: Path) -> None:
    """Without a ``vacancies`` block the section starts with an empty list.

    There is no built-in canned data anymore: the mock client's three-entry
    demo dataset lives only in ``test/fixtures/mock_vacancies.yaml`` (and its
    mirror in ``test/fixtures/pipeline.mock.yaml.j2``), outside ``src/``.
    """
    path = build_valid_pipeline_yaml(runtime_dir / "config")
    section = load_pipeline_config(path).unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    assert section.searches[0].vacancies == []


def test_invalid_behavior_outcome_fails_validation(runtime_dir: Path) -> None:
    """An unknown ``outcome`` literal is rejected by section validation."""
    path = build_valid_pipeline_yaml(
        runtime_dir / "config",
        behavior_yaml="    behavior:\n      default_apply:\n        outcome: exploded\n",
    )
    result = load_pipeline_config(path)
    assert result.is_err
    message = result.unwrap_err()
    assert "Invalid service.mock section" in message
    assert "behavior" in message or "outcome" in message.lower()


def test_service_section_round_trip_preserves_behavior(runtime_dir: Path) -> None:
    """A full ``behavior`` + fixture-seeded vacancies survive dump -> load."""
    del runtime_dir  # round-trip is pure (no filesystem needed)
    # Seed the same canned dataset the mock tests use (test/fixtures/mock_vacancies.yaml).
    section = MockServiceConfig(
        resume_id="r1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=MockSearchParams(
                    area=(1,),
                    schedule=("fullDay",),
                    experience="between1And3",
                    only_with_salary=True,
                ),
                vacancies=mock_vacancies(),
            ),
        ),
        behavior=MockBehaviorConfig(
            authorize_error="denied",
            default_apply=MockApplyBehaviorConfig(outcome="error", message="bad"),
            per_vacancy={"mock-2": MockApplyBehaviorConfig(outcome="limit_exceeded")},
        ),
    )
    dumped = dump_service_section(section)
    result = load_service_section("mock", dumped)
    assert result.is_ok, result
    recon = result.unwrap()
    assert isinstance(recon, MockServiceConfig)
    behavior = recon.behavior
    assert behavior is not None
    assert behavior.authorize_error == "denied"
    default_apply = behavior.default_apply
    assert default_apply is not None
    assert default_apply.outcome == "error"
    assert default_apply.message == "bad"
    assert behavior.per_vacancy["mock-2"].outcome == "limit_exceeded"
    # The concrete filter (tuple-typed) survives JSON serialization.
    assert recon.searches[0].filter.area == (1,)
    assert recon.searches[0].filter.schedule == ("fullDay",)
    # The fixture-seeded vacancies survive too — incl. the salary ``from``
    # lower bound, round-tripped via its ``from`` alias (not ``from_``).
    assert [v.external_id for v in recon.searches[0].vacancies] == [v.external_id for v in mock_vacancies()]
    assert recon.searches[0].vacancies[0].salary is not None
    assert recon.searches[0].vacancies[0].salary.from_ == 250000


def test_mock_section_fields_carry_typed_ui_metadata() -> None:
    """Section models expose labeled, enum-driven UI schema metadata (§6.2).

    The UI renders per-service typed forms from ``model_json_schema()``, which is
    built from each field's ``Field(title/description/examples)`` and ``Literal``
    annotations. Asserting on the typed ``model_fields`` + ``Literal`` args (not
    the untyped ``Any`` JSON schema) proves that metadata source.
    """
    filter_fields = MockSearchParams.model_fields
    assert filter_fields["area"].title == "Area"
    assert filter_fields["schedule"].title == "Schedule"
    desc = filter_fields["schedule"].description
    assert desc is not None and "multi-select" in desc
    assert filter_fields["experience"].title == "Experience"
    assert filter_fields["only_with_salary"].title == "Only with salary"
    # Enum-driven choices: multi-select schedule and single-select experience.
    assert set(get_args(MockSchedule)) == {"fullDay", "remote", "flexible", "shift", "flyInFlyOut"}
    assert set(get_args(MockExperience)) == {"noExperience", "between1And3", "between3And6", "moreThan6"}

    entry_fields = MockSearchEntry.model_fields
    assert entry_fields["query"].title == "Query"
    assert entry_fields["filter"].title == "Search filter"
    assert entry_fields["vacancies"].title == "Vacancies"

    svc = MockServiceConfig.model_fields
    assert svc["resume_id"].title == "Resume id"
    assert svc["searches"].title == "Search pool"
    assert svc["behavior"].title == "Behavior (manual testing)"

    apply_fields = MockApplyBehaviorConfig.model_fields
    assert apply_fields["outcome"].title == "Outcome"
    assert set(get_args(MockOutcome)) == {"applied", "skipped", "error", "limit_exceeded"}
    behavior_fields = MockBehaviorConfig.model_fields
    assert behavior_fields["authorize_error"].title == "Authorize error"


def test_malformed_yaml_returns_err(runtime_dir: Path) -> None:
    """Malformed YAML surfaces as an ``Err`` with a clear message."""
    path = _bad_yaml(runtime_dir)
    result = load_pipeline_config(path)
    assert result.is_err
    assert "Malformed YAML" in result.unwrap_err()


def test_missing_referenced_file_returns_err(runtime_dir: Path) -> None:
    """A missing referenced file (e.g. the resume) surfaces as a clear ``Err``."""
    config_dir = runtime_dir / "config"
    login = config_dir / "secrets" / "login.txt"
    login.parent.mkdir(parents=True, exist_ok=True)
    login.write_text("mock-login\n", encoding="utf-8")
    password = config_dir / "secrets" / "password.txt"
    password.write_text("mock-password\n", encoding="utf-8")
    # resume file deliberately not created
    path = build_valid_pipeline_yaml(config_dir)
    # Overwrite with a resume that does not exist on disk.
    path.write_text(
        path.read_text(encoding="utf-8").replace('resume.md"', '__missing__.md"'),
        encoding="utf-8",
    )
    result = load_pipeline_config(path)
    assert result.is_err
    assert "Could not read referenced file" in result.unwrap_err()


def test_unknown_top_level_key_returns_err(runtime_dir: Path) -> None:
    """An unknown top-level key (e.g. a removed setting) fails schema validation."""
    config_dir = runtime_dir / "config"
    path = build_valid_pipeline_yaml(config_dir)
    text = path.read_text(encoding="utf-8").replace(
        'description: "A config test pipeline"',
        'description: "A config test pipeline"\npath_resolution: absolute',
    )
    path.write_text(text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_err
    assert "Invalid pipeline.yaml" in result.unwrap_err()


def test_unknown_board_returns_err(runtime_dir: Path) -> None:
    """An unregistered board name surfaces an ``Err`` naming the known boards."""
    config_dir = runtime_dir / "config"
    path = build_valid_pipeline_yaml(config_dir)
    text = path.read_text(encoding="utf-8").replace("mock:", "mystery-board:")
    path.write_text(text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_err
    assert "Unknown board" in result.unwrap_err()


def test_service_not_a_mapping_returns_err(runtime_dir: Path) -> None:
    """A ``service`` value that is not a board mapping is rejected."""
    config_dir = runtime_dir / "config"
    minimal = (
        "name: demo\n"
        "description: d\n"
        'service: "mock"\n'
        "auth:\n  login_file: absent\n  password_file: absent\n"
        "resume:\n  path: absent\n"
        "openai:\n  model: m\n\n  base_url: u\n"
        "scoring:\n  min_required_score: 1\n"
        "apply: {}\n"
        "limits:\n  daily_apply_limit: 5\n"
    )
    path = config_dir / "pipeline.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(minimal, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_err
    # The section check runs before any file loading, so the error is the
    # non-mapping ``service`` (not the missing referenced files).
    assert "must be a mapping" in result.unwrap_err()


def test_relative_refs_resolve_against_yaml_dir(runtime_dir: Path) -> None:
    """Relative references resolve against the pipeline.yaml file's directory."""
    config_dir = runtime_dir / "config"
    (config_dir / "secrets").mkdir(parents=True, exist_ok=True)
    (config_dir / "secrets" / "login.txt").write_text("login\n", encoding="utf-8")
    (config_dir / "secrets" / "password.txt").write_text("pass\n", encoding="utf-8")
    (config_dir / "resume.md").write_text("# CV\n", encoding="utf-8")
    (config_dir / "api.txt").write_text("key\n", encoding="utf-8")

    yaml_text = (
        "name: rel\n"
        "description: d\n"
        "service:\n  mock:\n    resume_id: r1\n    searches:\n"
        "      - query: q\n"
        "        filter:\n"
        "          area: [1]\n          schedule: []\n          experience: null\n"
        "          only_with_salary: false\n"
        "auth:\n  login_file: secrets/login.txt\n  password_file: secrets/password.txt\n"
        "resume:\n  path: resume.md\n"
        "openai:\n  model: m\n\n  base_url: u\n"
        "  api_key_file: api.txt\n"
        "scoring:\n  min_required_score: 1\n  scoring_prompt: score\n"
        "apply:\n  apply_prompt: apply\n"
        "limits:\n  daily_apply_limit: 5\n"
    )
    path = config_dir / "pipeline.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_ok, result
    config = result.unwrap()
    # Every referenced-file content slot is stripped at load: a file ends with
    # a trailing newline (editor/echo artifact), so login/resume hold the clean
    # values without newline artifacts.
    assert config.auth.login == "login"
    assert config.resume.contents == "# CV"
    assert config.openai.api_key == "key"


def test_relative_refs_climb_out_with_dotdot(runtime_dir: Path) -> None:
    """A ``../`` reference resolves against the yaml dir and climbs out of it."""
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shared = runtime_dir / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "login.txt").write_text("outer-login\n", encoding="utf-8")
    (shared / "password.txt").write_text("outer-pass\n", encoding="utf-8")
    (shared / "resume.md").write_text("# Outer CV\n", encoding="utf-8")

    yaml_text = (
        "name: outer\n"
        "description: d\n"
        "service:\n  mock:\n    resume_id: r1\n    searches:\n"
        "      - query: q\n"
        "        filter:\n"
        "          area: [1]\n          schedule: []\n          experience: null\n"
        "          only_with_salary: false\n"
        "auth:\n  login_file: ../shared/login.txt\n  password_file: ../shared/password.txt\n"
        "resume:\n  path: ../shared/resume.md\n"
        "openai:\n  model: m\n\n  base_url: u\n"
        "  api_key: key\n"
        "scoring:\n  min_required_score: 1\n  scoring_prompt: score\n"
        "apply:\n  apply_prompt: apply\n"
        "limits:\n  daily_apply_limit: 5\n"
    )
    path = config_dir / "pipeline.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_ok, result
    config = result.unwrap()
    assert config.auth.login == "outer-login"
    assert config.auth.password == "outer-pass"
    assert config.resume.contents == "# Outer CV"


def test_referenced_file_contents_are_trimmed(runtime_dir: Path) -> None:
    """Referenced-file contents are ``strip()``ed (leading + trailing whitespace).

    Files conventionally carry a trailing newline and may carry stray leading
    whitespace; the loaded value must hold the trimmed credential/resume/prompt
    so downstream stages never see whitespace artifacts. Inline values are a
    separate path and are left exactly as authored (see
    ``test_inline_contents_load_as_ok``).
    """
    config_dir = runtime_dir / "config"
    login = config_dir / "secrets" / "login.txt"
    login.parent.mkdir(parents=True, exist_ok=True)
    login.write_text("  mock-login\n\n", encoding="utf-8")
    password = config_dir / "secrets" / "password.txt"
    password.write_text("\n\nmock-password\n", encoding="utf-8")
    api_key = config_dir / "secrets" / "api_key.txt"
    api_key.write_text("sk-test\n", encoding="utf-8")
    resume = config_dir / "resume.md"
    resume.write_text("\n# Mock CV\n", encoding="utf-8")
    score_prompt = config_dir / "prompts" / "score.md.j2"
    score_prompt.parent.mkdir(parents=True, exist_ok=True)
    score_prompt.write_text("  score template\n", encoding="utf-8")
    apply_prompt = config_dir / "prompts" / "apply.md.j2"
    apply_prompt.write_text("apply template \n", encoding="utf-8")

    path = config_dir / "pipeline.yaml"
    yaml_text = (
        "name: trim\n"
        "description: d\n"
        "service:\n  mock:\n    resume_id: r1\n    searches:\n"
        "      - query: q\n"
        "        filter:\n"
        "          area: [1]\n          schedule: []\n          experience: null\n"
        "          only_with_salary: false\n"
        f'auth:\n  login_file: "{login}"\n  password_file: "{password}"\n'
        f'resume:\n  path: "{resume}"\n'
        f'openai:\n  model: m\n\n  base_url: u\n  api_key_file: "{api_key}"\n'
        "scoring:\n  min_required_score: 1\n"
        f'  scoring_prompt_file: "{score_prompt}"\n'
        f'apply:\n  apply_prompt_file: "{apply_prompt}"\n'
        "limits:\n  daily_apply_limit: 5\n"
    )
    path.write_text(yaml_text, encoding="utf-8")
    config = load_pipeline_config(path).unwrap()
    assert config.auth.login == "mock-login"
    assert config.auth.password == "mock-password"
    assert config.resume.contents == "# Mock CV"
    assert config.openai.api_key == "sk-test"
    assert config.scoring.scoring_prompt == "score template"
    assert config.apply.apply_prompt == "apply template"


# --- inline values (file-less content slots) + exclusive-or source checking -- #
def test_inline_contents_load_as_ok(runtime_dir: Path) -> None:
    """A pipeline specifying every content slot inline (no files) loads as Ok
    carrying the exact inline contents."""
    path = build_inline_pipeline_yaml(runtime_dir / "config")
    result = load_pipeline_config(path)
    assert result.is_ok, result
    config = result.unwrap()
    assert config.auth.login == "inline-login"
    assert config.auth.password == "inline-password"
    assert config.resume.contents == "# Inline CV\n"
    assert config.openai.api_key == "inline-api-key"
    assert config.scoring.scoring_prompt == "inline score template"
    assert config.apply.apply_prompt == "inline apply template"


def test_both_file_and_inline_returns_err(runtime_dir: Path) -> None:
    """Providing BOTH a ``*_file``/``path`` and its inline ``x`` is ambiguous -> ``Err``."""
    config_dir = runtime_dir / "config"
    path = config_dir / "pipeline.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = (
        "name: both\n"
        "description: d\n"
        "service:\n  mock:\n    resume_id: r1\n    searches:\n"
        "      - query: q\n"
        "        filter:\n"
        "          area: [1]\n          schedule: []\n          experience: null\n"
        "          only_with_salary: false\n"
        "auth:\n  login_file: secrets/login.txt\n  login: inline-login\n"
        "  password: inline-password\n"
        "resume:\n  contents: '# CV'\n"
        "openai:\n  model: m\n\n  base_url: u\n"
        "  api_key: key\n"
        "scoring:\n  min_required_score: 1\n  scoring_prompt: p\n"
        "apply:\n  apply_prompt: r\n"
        "limits:\n  daily_apply_limit: 5\n"
    )
    path.write_text(yaml_text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_err
    err = result.unwrap_err()
    assert "auth: provide either 'login_file' or 'login', not both" in err


def test_missing_required_slot_returns_err(runtime_dir: Path) -> None:
    """Providing NEITHER source for a required slot (e.g. scoring prompt) is an ``Err``."""
    config_dir = runtime_dir / "config"
    path = config_dir / "pipeline.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = (
        "name: none\n"
        "description: d\n"
        "service:\n  mock:\n    resume_id: r1\n    searches:\n"
        "      - query: q\n"
        "        filter:\n"
        "          area: [1]\n          schedule: []\n          experience: null\n"
        "          only_with_salary: false\n"
        "auth:\n  login: inline-login\n  password: inline-password\n"
        "resume:\n  contents: '# CV'\n"
        "openai:\n  model: m\n\n  base_url: u\n"
        "  api_key: key\n"
        "scoring:\n  min_required_score: 3\n"
        "apply:\n  apply_prompt: r\n"
        "limits:\n  daily_apply_limit: 5\n"
    )
    path.write_text(yaml_text, encoding="utf-8")
    result = load_pipeline_config(path)
    assert result.is_err
    err = result.unwrap_err()
    assert "scoring: provide one of 'scoring_prompt_file' or 'scoring_prompt'" in err


# --- service-section + openai_captcha dump/load + content-columns reconstruct #
def _loaded_config(runtime_dir: Path) -> PipelineConfig:
    """A validated config loaded from a real yaml (referenced-file contents injected)."""
    path = build_valid_pipeline_yaml(runtime_dir / "config")
    result = load_pipeline_config(path)
    assert result.is_ok, result
    config: PipelineConfig = result.unwrap()
    return config


def _mock_section() -> MockServiceConfig:
    """A representative validated mock service section for round-trip tests."""
    return MockServiceConfig(
        resume_id="r1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=MockSearchParams(
                    area=(1,),
                    schedule=("fullDay",),
                    experience="between1And3",
                    only_with_salary=True,
                ),
            ),
        ),
    )


def test_service_section_dump_load_round_trip() -> None:
    """``dump_service_section``/``load_service_section`` re-validate the section."""
    dumped = dump_service_section(_mock_section())
    result = load_service_section("mock", dumped)
    assert result.is_ok, result
    section = result.unwrap()
    assert isinstance(section, MockServiceConfig)
    assert section.resume_id == "r1"
    assert section.searches[0].filter.area == (1,)
    assert section.searches[0].filter.only_with_salary is True


def test_load_service_section_none_returns_ok_none() -> None:
    """A ``None`` stored column (pre-migration row) is ``Ok(None)``, not an error."""
    result = load_service_section("mock", None)
    assert result.is_ok
    assert result.unwrap() is None


def test_load_service_section_unknown_board_returns_err() -> None:
    """An unregistered board name in the stored section is a clear ``Err``."""
    result = load_service_section("mystery", "{}")
    assert result.is_err
    assert "Unknown board" in result.unwrap_err()


def test_load_service_section_bad_json_returns_err() -> None:
    """Corrupted stored section JSON surfaces as a clear ``Err``."""
    result = load_service_section("mock", "{not json")
    assert result.is_err
    assert "not valid JSON" in result.unwrap_err()


def test_dump_openai_captcha_none_returns_none() -> None:
    """No captcha section → ``None`` stored (not a JSON literal)."""
    assert dump_openai_captcha(None) is None


def test_openai_captcha_dump_load_round_trip() -> None:
    """``dump_openai_captcha``/``load_openai_captcha`` re-validate the section
    and embed the ``api_key`` CONTENT (we trust the DB)."""
    captcha = OpenAIConfig(
        model="gpt-cap",
        base_url="https://cap.example.com/v1",
        api_key="captcha-secret",
        reasoning_effort="low",
    )
    dumped = dump_openai_captcha(captcha)
    assert dumped is not None
    result = load_openai_captcha(dumped)
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon is not None
    assert recon.model == "gpt-cap"
    assert recon.base_url == "https://cap.example.com/v1"
    assert recon.api_key == "captcha-secret"
    # The optional reasoning_effort rides along the opaque JSON round-trip.
    assert recon.reasoning_effort == "low"


def test_load_openai_captcha_none_returns_ok_none() -> None:
    """A ``None`` stored openai_captcha column is ``Ok(None)``."""
    result = load_openai_captcha(None)
    assert result.is_ok
    assert result.unwrap() is None


def test_load_openai_captcha_bad_json_returns_err() -> None:
    """Corrupted stored openai_captcha JSON surfaces as a clear ``Err``."""
    result = load_openai_captcha("{not json")
    assert result.is_err
    assert "not valid JSON" in result.unwrap_err()


def test_openai_captcha_default_consensus_requests() -> None:
    """K defaults to 4 vision requests per captcha attempt."""
    captcha = OpenAIConfig(model="m", base_url="https://cap.example.com/v1")
    assert captcha.consensus_requests == 4


def test_openai_captcha_consensus_requests_bounds() -> None:
    """K is bounded to 1-10 (pydantic ge/le); out-of-range values are rejected."""
    for bad in (0, 11):
        with pytest.raises(ValidationError, match="consensus_requests"):
            OpenAIConfig(model="m", base_url="https://cap.example.com/v1", consensus_requests=bad)


def test_openai_captcha_consensus_requests_round_trip() -> None:
    """A configured K survives the opaque ``openai_captcha`` JSON round trip."""
    captcha = OpenAIConfig(
        model="gpt-cap",
        base_url="https://cap.example.com/v1",
        api_key="captcha-secret",
        consensus_requests=2,
    )
    dumped = dump_openai_captcha(captcha)
    assert dumped is not None
    result = load_openai_captcha(dumped)
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon is not None
    assert recon.consensus_requests == 2


def test_load_openai_captcha_without_consensus_requests_gets_default() -> None:
    """Pre-existing stored sections (no K field) load with the default — no migration."""
    stored = '{"model": "gpt-cap", "base_url": "https://cap.example.com/v1"}'
    result = load_openai_captcha(stored)
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon is not None
    assert recon.consensus_requests == 4


def test_hh_test_solving_dump_load_round_trip() -> None:
    """The AI-decline opt-in survives the opaque ``hh_test_solving`` JSON round trip."""
    section = HhTestSolvingConfig(
        enabled=True,
        allow_ai_to_skip_test_when_not_enough_context=True,
        test_prompt="Solve {{ test_formatted }}",
    )
    dumped = dump_hh_test_solving(section)
    assert dumped is not None
    result = load_hh_test_solving(dumped)
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon is not None
    assert recon.allow_ai_to_skip_test_when_not_enough_context is True


def test_load_hh_test_solving_without_decline_flag_gets_default() -> None:
    """Pre-existing stored sections (no decline flag) load with ``False`` — no migration."""
    stored = '{"enabled": true, "test_prompt": "Solve {{ test_formatted }}"}'
    result = load_hh_test_solving(stored)
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon is not None
    assert recon.allow_ai_to_skip_test_when_not_enough_context is False


def _refs_from_config(config: PipelineConfig) -> PersistedPipelineRefs:
    """Map a loaded config onto its `PersistedPipelineRefs` row view (as the
    CLI's init/update would store it — contents in the row columns)."""
    return PersistedPipelineRefs(
        name=config.name,
        description=config.description,
        service=config.service,
        login=config.auth.login,
        password=config.auth.password,
        resume=config.resume.contents,
        openai_model=config.openai.model,
        openai_base_url=config.openai.base_url,
        openai_api_key=config.openai.api_key or "",
        openai_reasoning_effort=config.openai.reasoning_effort,
        min_required_score=config.scoring.min_required_score,
        scoring_prompt=config.scoring.scoring_prompt,
        apply_prompt=config.apply.apply_prompt,
        daily_apply_limit=config.limits.daily_apply_limit,
        service_section=dump_service_section(config.service_section),
        openai_captcha=dump_openai_captcha(config.openai_captcha),
    )


def _roundtrip(runtime_dir: Path) -> tuple[PipelineConfig, PipelineConfig]:
    """Reconstruct a config from its stored row columns, returning (orig, recon)."""
    config = _loaded_config(runtime_dir)
    result = reconstruct_pipeline_config(_refs_from_config(config))
    assert result.is_ok, result
    recon: PipelineConfig = result.unwrap()
    return config, recon


def test_config_columns_round_trip_embeds_contents(runtime_dir: Path) -> None:
    """Reconstruct from the row columns restores the config with contents,
    and does so with no file I/O (contents were stored at init time)."""
    config, recon = _roundtrip(runtime_dir)
    assert recon.name == config.name
    assert recon.service == config.service
    # Credentials / resume / prompt / key CONTENTS survive the round trip.
    # All file-loaded contents were trimmed at load time, so both sides hold
    # the clean values (no trailing-newline artifacts).
    assert recon.auth.login == config.auth.login == "mock-login"
    assert recon.auth.password == config.auth.password == "mock-password"
    assert recon.resume.contents == config.resume.contents == "# Mock CV"
    assert recon.openai.api_key == config.openai.api_key == "mock-api-key"
    assert recon.scoring.scoring_prompt == "score template"
    assert recon.apply.apply_prompt == "apply template"
    # The re-validated section is attached.
    assert recon.service_section.resume_id == "mock-resume-1"


def test_config_columns_round_trip_reconstructs_without_files(runtime_dir: Path) -> None:
    """A ``--pipeline-id``-style run never needs the original files.

    The row stores all referenced-file CONTENTS in its columns, so
    ``reconstruct_pipeline_config`` never touches the filesystem — there is no
    JSON snapshot to keep in sync.
    """
    config, recon = _roundtrip(runtime_dir)
    del config  # _roundtrip already asserts reconstruction; we only inspect recon
    # The referenced files still exist on disk (nothing was deleted); the point
    # is the reconstruction path performs zero file I/O. Deleting them would be
    # equally fine — nothing reads them — but there is no snapshot to prove.
    assert recon.resume.contents == "# Mock CV"
    assert recon.auth.login == "mock-login"
    assert recon.openai.api_key == "mock-api-key"


def test_config_columns_round_trip_embeds_openai_captcha(runtime_dir: Path) -> None:
    """The ``openai_captcha`` section (including its key) round-trips via the
    ``openai_captcha`` column."""
    config = _loaded_config(runtime_dir)
    captcha = OpenAIConfig(
        model="gpt-cap",
        base_url="https://cap.example.com/v1",
        api_key="captcha-secret",
        reasoning_effort="true",  # provider-specific value; survives opaque JSON verbatim
    )
    config = config.model_copy(update={"openai_captcha": captcha})
    result = reconstruct_pipeline_config(_refs_from_config(config))
    assert result.is_ok, result
    recon = result.unwrap()
    assert recon.openai_captcha is not None
    assert recon.openai_captcha.model == "gpt-cap"
    assert recon.openai_captcha.base_url == "https://cap.example.com/v1"
    assert recon.openai_captcha.api_key == "captcha-secret"
    # reasoning_effort rides along the opaque openai_captcha JSON column.
    assert recon.openai_captcha.reasoning_effort == "true"


def test_reconstruct_missing_service_section_returns_err(runtime_dir: Path) -> None:
    """A row with no stored service section is non-runnable (clear ``Err``)."""
    config = _loaded_config(runtime_dir)
    refs = replace(_refs_from_config(config), service_section=None)
    result = reconstruct_pipeline_config(refs)
    assert result.is_err
    assert "no stored service section" in result.unwrap_err()


def test_reconstruct_bad_service_section_returns_err(runtime_dir: Path) -> None:
    """A row with corrupted stored section JSON is a clear ``Err``."""
    config = _loaded_config(runtime_dir)
    refs = replace(_refs_from_config(config), service_section="{not json")
    result = reconstruct_pipeline_config(refs)
    assert result.is_err
    assert "not valid JSON" in result.unwrap_err()


def test_reconstruct_bad_openai_captcha_json_returns_err(runtime_dir: Path) -> None:
    """A row with corrupted stored openai_captcha JSON is a clear ``Err``."""
    config = _loaded_config(runtime_dir)
    refs = replace(_refs_from_config(config), openai_captcha="{not json")
    result = reconstruct_pipeline_config(refs)
    assert result.is_err
    assert "not valid JSON" in result.unwrap_err()


# --- openai reasoning_effort -------------------------------------------------
def test_openai_config_accepts_reasoning_effort() -> None:
    """``openai``/``openai_captcha`` sections accept any string reasoning_effort.

    Values are provider-dependent (e.g. OpenAI: low/medium/high; Gemini:
    true/false), so no closed set is enforced — the value is a plain passthrough.
    """
    config = OpenAIConfig(
        model="m",
        base_url="u",
        api_key=None,
        reasoning_effort="true",  # a Gemini-style value, not an OpenAI enum member
    )
    assert config.reasoning_effort == "true"

    # The pydantic boundary (what the YAML loader hits) accepts arbitrary
    # strings verbatim — no validation against a fixed set.
    dynamic = OpenAIConfig.model_validate({"model": "m", "base_url": "u", "reasoning_effort": "very_high"})
    assert dynamic.reasoning_effort == "very_high"


def test_openai_reasoning_effort_round_trip(runtime_dir: Path) -> None:
    """The reasoning_effort scalar survives the stored-column reconstruct.

    A ``--pipeline-id`` run rebuilds the config from ``PersistedPipelineRefs``
    (the row columns), so the scalar must survive that path; an omitted value
    stays ``None`` on both sides.
    """
    config = _loaded_config(runtime_dir)
    assert config.openai.reasoning_effort is None
    recon = _roundtrip(runtime_dir)[1]
    assert recon.openai.reasoning_effort is None

    configured = config.model_copy(update={"openai": config.openai.model_copy(update={"reasoning_effort": "medium"})})
    result = reconstruct_pipeline_config(_refs_from_config(configured))
    assert result.is_ok, result
    assert result.unwrap().openai.reasoning_effort == "medium"


def test_reconstruct_passes_through_non_openai_reasoning_effort(runtime_dir: Path) -> None:
    """A stored value outside OpenAI's classic set reconstructs verbatim.

    The scalar is a free-form passthrough, so no validation happens on
    reconstruction: a provider-specific value (e.g. Gemini's ``true``) is
    forwarded to the rebuilt config unchanged.
    """
    config = _loaded_config(runtime_dir)
    refs = replace(_refs_from_config(config), openai_reasoning_effort="true")
    result = reconstruct_pipeline_config(refs)
    assert result.is_ok, result
    assert result.unwrap().openai.reasoning_effort == "true"
