"""Contract tests for the generated vacancy-document schema.

Scenario: the typed vacancy contract generates one deterministic schema
  Given the registered vacancy schema source
  When schemas are generated into a clean project root
  Then the public and packaged artifacts are identical Draft 2020-12 schemas
  And the embedded content version matches the generated schema

Scenario: every persisted vacancy field has one document role
  Given the VacancyRecord DTO
  When its fields are compared with the vacancy document policy classifications
  Then every DTO field is either editable, review context, or explicitly not exported
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from jobfucker.app.vacancy_document_schema import (
    CONTEXT_FIELD_NAMES,
    EDITABLE_FIELD_NAMES,
    VACANCY_SCHEMA_ID,
    VACANCY_SCHEMA_SOURCE,
)
from jobfucker.schema_generation import derive_schema_version, generate_all_schemas, generate_schema
from jobfucker.storage.dto import VacancyRecord

_JSON_MAPPING = TypeAdapter(dict[str, JsonValue])  # lint-ignore[raw-dict]: parsed JSON schema boundary


def test_generation_writes_identical_versioned_artifacts(tmp_path: Path) -> None:
    generated_paths = generate_all_schemas(tmp_path)

    public_path = tmp_path / "docs/schemas/vacancies-dump.schema.json"
    packaged_path = tmp_path / "src/jobfucker/resources/schemas/vacancies-dump.schema.json"
    pipeline_path = tmp_path / "docs/schemas/pipeline.schema.json"
    assert generated_paths == (public_path, packaged_path, pipeline_path)
    assert public_path.read_bytes() == packaged_path.read_bytes()

    schema = _JSON_MAPPING.validate_json(public_path.read_bytes())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == VACANCY_SCHEMA_ID
    assert schema["x-jobfucker-schema-version"] == derive_schema_version(schema)


def test_generation_is_deterministic() -> None:
    first = generate_schema(VACANCY_SCHEMA_SOURCE)
    second = generate_schema(VACANCY_SCHEMA_SOURCE)

    assert first == second
    assert first.content.endswith(b"\n")


def test_committed_and_packaged_schemas_have_no_generation_drift(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    generated_paths = generate_all_schemas(tmp_path)

    for generated_path in generated_paths:
        committed_path = project_root / generated_path.relative_to(tmp_path)
        assert committed_path.read_bytes() == generated_path.read_bytes()


def test_schema_version_changes_with_contract_content() -> None:
    first: dict[str, JsonValue] = {"type": "string"}  # lint-ignore[raw-dict]: schema-generator boundary
    second: dict[str, JsonValue] = {"type": "integer"}  # lint-ignore[raw-dict]: schema-generator boundary

    assert derive_schema_version(first) != derive_schema_version(second)


def test_every_vacancy_dto_field_is_editable_context_or_explicitly_unexported() -> None:
    roles = (set(EDITABLE_FIELD_NAMES), set(CONTEXT_FIELD_NAMES))
    vacancy_fields = {field.name for field in fields(VacancyRecord)}
    document_fields = roles[0] | roles[1]

    # Editable and context roles are disjoint. The only document fields
    # without a DTO column are the derived staleness flags. The remaining DTO
    # fields (id, snapshot provenance, soft_deleted_at) are deliberately not
    # exported as values: the id rides in the handle, soft-deletion is the
    # `deleted` flag.
    assert not (roles[0] & roles[1])
    assert document_fields - vacancy_fields == {"score_stale", "letter_stale", "user_stale"}
    assert vacancy_fields - document_fields == {
        "id",
        "fetched_snapshot_id",
        "scored_snapshot_id",
        "generated_snapshot_id",
        "applied_snapshot_id",
        "soft_deleted_at",
        "has_hh_test",
    }
