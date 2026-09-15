"""Behavior tests for the reusable table-document codec.

Scenario: YAML and JSON encode one ordered logical document
  Given an ordered table document
  When it is encoded as YAML and JSON
  Then both decode to the same values and the trailing block stays last

Scenario: hostile document syntax fails closed
  Given duplicate keys, YAML aliases, oversize input, or a schema-invalid value
  When the document is decoded
  Then decoding returns an error before application logic sees the value

Scenario: multiline text is readable and lossless in YAML
  Given instructions and vacancy text containing newlines
  When the document is encoded as YAML
  Then literal block scalars are used and decoding restores the exact strings
"""

from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue

from jobfucker.table_documents.codec import (
    DocumentFormat,
    DocumentLimits,
    OverwriteMode,
    decode_document,
    encode_document,
    write_document,
)


def _simple_schema() -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: JSON Schema fixture
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["vacancies", "trailing_block"],
        "additionalProperties": False,
        "properties": {
            "vacancies": {"type": "object", "additionalProperties": {"type": "integer"}},
            "trailing_block": {"type": "object"},
        },
    }


def _document() -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: encoded document fixture
    return {
        "vacancies": {"vacancy_2": 2, "vacancy_1": 1},
        "trailing_block": {"version": "sha256:test"},
    }


def test_yaml_and_json_round_trip_the_same_ordered_document() -> None:
    document = _document()

    yaml_result = encode_document(document, DocumentFormat.YAML)
    json_result = encode_document(document, DocumentFormat.JSON)

    assert yaml_result.is_ok
    assert json_result.is_ok
    yaml_bytes = yaml_result.unwrap()
    json_bytes = json_result.unwrap()
    assert yaml_bytes.rfind(b"trailing_block:") > yaml_bytes.rfind(b"vacancies:")
    assert json_bytes.rfind(b'"trailing_block"') > json_bytes.rfind(b'"vacancies"')
    assert decode_document(yaml_bytes, DocumentFormat.YAML, _simple_schema()).unwrap() == document
    assert decode_document(json_bytes, DocumentFormat.JSON, _simple_schema()).unwrap() == document


def test_yaml_encodes_multiline_strings_as_literal_blocks() -> None:
    document: dict[str, JsonValue] = {
        "editing_instructions_edit_not_allowed": "First line.\nSecond line.",
        "vacancies": {"vacancy_1": {"description": "Line one.\nLine two."}},
    }
    permissive_schema: dict[str, JsonValue] = {"type": "object"}

    result = encode_document(document, DocumentFormat.YAML)

    assert result.is_ok
    content = result.unwrap()
    assert b"editing_instructions_edit_not_allowed: |-" in content
    assert b"description: |-" in content
    assert decode_document(content, DocumentFormat.YAML, permissive_schema).unwrap() == document


def test_decoder_rejects_duplicate_json_and_yaml_keys() -> None:
    duplicate_json = b'{"vacancies": {}, "vacancies": {}, "trailing_block": {}}'
    duplicate_yaml = b"vacancies: {}\nvacancies: {}\ntrailing_block: {}\n"

    json_result = decode_document(duplicate_json, DocumentFormat.JSON, _simple_schema())
    yaml_result = decode_document(duplicate_yaml, DocumentFormat.YAML, _simple_schema())

    assert json_result.is_err and "duplicate" in json_result.unwrap_err().lower()
    assert yaml_result.is_err and "duplicate" in yaml_result.unwrap_err().lower()


def test_decoder_rejects_yaml_aliases_and_oversize_input() -> None:
    alias_yaml = b"vacancies: &rows {}\ntrailing_block: *rows\n"

    alias_result = decode_document(alias_yaml, DocumentFormat.YAML, _simple_schema())
    size_result = decode_document(
        b"0123456789",
        DocumentFormat.JSON,
        _simple_schema(),
        limits=DocumentLimits(max_bytes=4, max_rows=2),
    )

    assert alias_result.is_err and "alias" in alias_result.unwrap_err().lower()
    assert size_result.is_err and "too large" in size_result.unwrap_err().lower()


def test_decoder_reports_schema_path_and_enforces_row_limit() -> None:
    invalid = b'{"vacancies":{"vacancy_1":"wrong"},"trailing_block":{}}'
    too_many = b'{"vacancies":{"vacancy_1":1,"vacancy_2":2},"trailing_block":{}}'

    invalid_result = decode_document(invalid, DocumentFormat.JSON, _simple_schema())
    row_result = decode_document(
        too_many,
        DocumentFormat.JSON,
        _simple_schema(),
        limits=DocumentLimits(max_bytes=1024, max_rows=1),
    )

    assert invalid_result.is_err and "vacancies.vacancy_1" in invalid_result.unwrap_err()
    assert row_result.is_err and "too many rows" in row_result.unwrap_err().lower()


def test_atomic_write_refuses_overwrite_unless_requested(tmp_path: Path) -> None:
    output_path = tmp_path / "vacancies.json"
    output_path.write_bytes(b"old")

    refused = write_document(output_path, b"new", OverwriteMode.REFUSE)
    replaced = write_document(output_path, b"new", OverwriteMode.REPLACE)

    assert refused.is_err and "already exists" in refused.unwrap_err()
    assert replaced.is_ok
    assert output_path.read_bytes() == b"new"
    assert not tuple(tmp_path.glob(".vacancies.json.*"))


def test_atomic_write_refuses_dangling_symlink_without_force(tmp_path: Path) -> None:
    output_path = tmp_path / "vacancies.json"
    missing_target = tmp_path / "missing.json"
    output_path.symlink_to(missing_target)

    refused = write_document(output_path, b"sensitive", OverwriteMode.REFUSE)

    assert refused.is_err and "already exists" in refused.unwrap_err()
    assert output_path.is_symlink()
    assert not missing_target.exists()
    assert not tuple(tmp_path.glob(".vacancies.json.*"))
