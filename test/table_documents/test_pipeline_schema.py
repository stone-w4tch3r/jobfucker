"""Contract tests for the generated pipeline.yaml schema.

Scenario: the typed pipeline contract generates one deterministic schema
  Given the registered pipeline schema source
  When schemas are generated into a clean project root
  Then the artifact is a Draft 2020-12 schema with the derived version

Scenario: committed example pipelines validate against the generated schema
  Given the repo's mock/hh pipeline yamls
  When each document is validated with jsonschema
  Then every document validates

Scenario: schema fragments reject author errors the loader rejects
  Given slot xor/exclusivity constraints and explicit nulls
  When mutated documents are validated
  Then both-sources, neither-sources, and null-value mistakes all fail

Scenario: authored schema-source models track the loader models
  Given the authored models in pipeline_schema_source and the loader models in config
  When their field names, requiredness, and hand-copied constraints are compared
  Then every pair matches (no silent drift of the authored shape)
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, JsonValue, TypeAdapter

from jobfucker.app.pipeline_schema_source import (
    PIPELINE_SCHEMA_ID,
    PIPELINE_SCHEMA_SOURCE,
    PipelineApplyYaml,
    PipelineAuthYaml,
    PipelineHhTestSolvingYaml,
    PipelineLimitsYaml,
    PipelineOpenAiYaml,
    PipelineResumeYaml,
    PipelineScoringYaml,
    PipelineServiceYaml,
    PipelineYamlDocument,
)
from jobfucker.config import (
    SERVICE_SECTION_MODELS,
    ApplyConfig,
    AuthConfig,
    HhTestSolvingConfig,
    LimitsConfig,
    OpenAIConfig,
    PipelineConfig,
    ResumeConfig,
    ScoringConfig,
)
from jobfucker.schema_generation import derive_schema_version, generate_all_schemas, generate_schema

_JSON_MAPPING = TypeAdapter(dict[str, JsonValue])  # lint-ignore[raw-dict]: parsed JSON schema boundary
_PROJECT_ROOT = Path(__file__).parents[2]
_EXAMPLE_PIPELINES = (
    "docs/examples/pipeline.mock.yaml",
    "docs/examples/pipeline.hh-fullstack.example.yaml",
)

_FILTER = "filter: {area: [1], schedule: [fullDay], experience: between1And3, only_with_salary: false}"

_VALID_HEAD = "name: x\ndescription: d\n"
_VALID_MOCK_SERVICE = "service:\n  mock:\n    resume_id: r\n    searches:\n      - query: q\n        " + _FILTER + "\n"
_TAIL_LINES: Final[dict[str, str]] = {  # lint-ignore[raw-dict]: authored yaml fragments for test documents
    "auth": "auth: {login_file: l, password_file: p}",
    "resume": "resume: {path: r.md}",
    "openai": "openai: {model: m, base_url: 'https://x'}",
    "scoring": "scoring: {min_required_score: 3, scoring_prompt: s}",
    "apply": "apply: {apply_prompt: r}",
}
_VALID_TAIL = "".join(line + "\n" for line in _TAIL_LINES.values())


def _generated_schema() -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: schema boundary
    return _JSON_MAPPING.validate_json(generate_schema(PIPELINE_SCHEMA_SOURCE).content)


def _decode_yaml_document(text: str) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: decoded document boundary
    raw = yaml.safe_load(text)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; TypeAdapter narrows it immediately
    return _JSON_MAPPING.validate_python(raw)  # type: ignore[reportAny]  # rationale: raw exists only at the immediate validation boundary


def _validation_errors(document: Mapping[str, JsonValue], schema: Mapping[str, JsonValue]) -> list[str]:
    validator = Draft202012Validator(schema)
    found: tuple[JsonSchemaValidationError, ...] = tuple(
        validator.iter_errors(document)  # type: ignore[reportUnknownMemberType,reportAny]  # rationale: jsonschema's validator protocol leaves error generators untyped; the concrete validator yields ValidationError
    )
    return sorted(error.message for error in found)


def _document_with_tail_line(
    section: str, replacement: str
) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: decoded document boundary
    """A valid document with the given section's tail line swapped."""
    text = _VALID_HEAD + _VALID_MOCK_SERVICE + _VALID_TAIL.replace(_TAIL_LINES[section] + "\n", replacement + "\n")
    return _decode_yaml_document(text)


def test_generation_is_deterministic() -> None:
    first = generate_schema(PIPELINE_SCHEMA_SOURCE)
    second = generate_schema(PIPELINE_SCHEMA_SOURCE)

    assert first == second
    assert first.content.endswith(b"\n")


def test_generated_artifact_has_id_and_version(tmp_path: Path) -> None:
    generated_paths = generate_all_schemas(tmp_path)

    artifact_path = tmp_path / "docs/schemas/pipeline.schema.json"
    assert artifact_path in generated_paths
    schema = _JSON_MAPPING.validate_json(artifact_path.read_bytes())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == PIPELINE_SCHEMA_ID
    assert schema["x-jobfucker-schema-version"] == derive_schema_version(schema)


@pytest.mark.parametrize("relative_path", _EXAMPLE_PIPELINES)
def test_committed_pipeline_yamls_validate_against_generated_schema(relative_path: str) -> None:
    schema = _generated_schema()
    document = _decode_yaml_document((_PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))

    assert _validation_errors(document, schema) == []


def test_schema_rejects_invalid_documents() -> None:
    schema = _generated_schema()
    bad_vacancy = (
        _VALID_HEAD
        + _VALID_MOCK_SERVICE
        + "    vacancies:\n      - {external_id: x, url: 'https://mock.example/x', description: d}\n"
        + _VALID_TAIL
    )
    both_boards = (
        _VALID_HEAD
        + "service:\n  mock:\n    resume_id: r\n    "
        + _FILTER
        + "\n  hh:\n    resume_id: r2\n    "
        + _FILTER
        + "\n"
        + _VALID_TAIL
    )
    unknown_root_key = _VALID_HEAD + _VALID_MOCK_SERVICE + _VALID_TAIL + "bogus: 1\n"

    # The one-board oneOf masks nested messages, so the canned-vacancy shape is
    # asserted directly against the MockVacancyParams definition.
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    vacancy_def = defs["MockVacancyParams"]
    assert isinstance(vacancy_def, dict)
    bad_entry = _decode_yaml_document("{external_id: x, url: 'https://mock.example/x', description: d}")
    assert any("'title' is a required property" in message for message in _validation_errors(bad_entry, vacancy_def))

    # Both boards are COMPLETE documents, so only the exactly-one-board
    # oneOf can fail here (no unrelated required-field masking).
    assert _validation_errors(_decode_yaml_document(bad_vacancy), schema)
    assert _validation_errors(_decode_yaml_document(both_boards), schema)
    unknown_key_errors = _validation_errors(_decode_yaml_document(unknown_root_key), schema)
    assert any("bogus" in message for message in unknown_key_errors)


@pytest.mark.parametrize(
    ("section", "both_line", "neither_line"),
    [
        (
            "auth",
            "auth: {login_file: l, login: i, password_file: p, password: w}",
            "auth: {password_file: p, password: w}",
        ),
        ("resume", "resume: {path: r.md, contents: c}", "resume: {}"),
        (
            "scoring",
            "scoring: {min_required_score: 3, scoring_prompt_file: f, scoring_prompt: s}",
            "scoring: {min_required_score: 3}",
        ),
        ("apply", "apply: {apply_prompt_file: f, apply_prompt: r}", "apply: {}"),
    ],
)
def test_xor_fragments_reject_both_and_neither(section: str, both_line: str, neither_line: str) -> None:
    schema = _generated_schema()

    assert _validation_errors(_document_with_tail_line(section, both_line), schema)
    assert _validation_errors(_document_with_tail_line(section, neither_line), schema)


def test_openai_rejects_both_api_key_sources() -> None:
    schema = _generated_schema()
    document = _document_with_tail_line(
        "openai", "openai: {model: m, base_url: 'https://x', api_key_file: f, api_key: k}"
    )

    assert _validation_errors(document, schema)


def test_explicit_null_slot_value_rejected() -> None:
    """``login_file:`` with no value is yaml null — the loader rejects it, so must the schema."""
    schema = _generated_schema()
    document = _document_with_tail_line("auth", "auth: {login_file: , password_file: p}")

    assert _validation_errors(document, schema)


@pytest.mark.parametrize(
    ("authored", "loader"),
    [
        (PipelineAuthYaml, AuthConfig),
        (PipelineResumeYaml, ResumeConfig),
        (PipelineOpenAiYaml, OpenAIConfig),
        (PipelineScoringYaml, ScoringConfig),
        (PipelineApplyYaml, ApplyConfig),
        (PipelineLimitsYaml, LimitsConfig),
        (PipelineHhTestSolvingYaml, HhTestSolvingConfig),
        (PipelineYamlDocument, PipelineConfig),
    ],
)
def test_authored_models_match_loader_fields(authored: type[BaseModel], loader: type[BaseModel]) -> None:
    authored_fields = authored.model_fields
    loader_fields = loader.model_fields
    assert set(authored_fields) == set(loader_fields)
    for name, authored_field in authored_fields.items():
        assert authored_field.is_required() == loader_fields[name].is_required(), name


def test_hand_copied_constraints_match_loader() -> None:
    """Constraints/d defaults that exist only as hand-copies in the authored models."""
    authored_captcha = PipelineOpenAiYaml.model_fields["consensus_requests"]
    loader_captcha = OpenAIConfig.model_fields["consensus_requests"]
    assert authored_captcha.metadata == loader_captcha.metadata  # ge/le bounds
    assert authored_captcha.default == loader_captcha.default == 4  # type: ignore[reportAny]  # rationale: FieldInfo.default is typed Any; the equality chain pins the exact value

    assert PipelineLimitsYaml.model_fields["daily_apply_limit"].default == 50  # type: ignore[reportAny]  # rationale: FieldInfo.default is typed Any; pinned to 50
    assert LimitsConfig.model_fields["daily_apply_limit"].default == 50  # type: ignore[reportAny]  # rationale: FieldInfo.default is typed Any; pinned to 50


def test_authored_service_keys_match_board_registry() -> None:
    assert set(PipelineServiceYaml.model_fields) == set(SERVICE_SECTION_MODELS)
