"""Bounded YAML/JSON decoding, schema validation, and atomic file output."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import JsonValue, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from rusty_results.prelude import Err, Ok, Result
from yaml.nodes import MappingNode, Node, ScalarNode
from yaml.tokens import AliasToken, AnchorToken, Token

_JSON_MAPPING: Final = TypeAdapter(dict[str, JsonValue])  # lint-ignore[raw-dict]: decoded document boundary


class DocumentFormat(StrEnum):
    """Supported encodings for one logical table document."""

    YAML = "yaml"
    JSON = "json"


class OverwriteMode(StrEnum):
    """Whether an atomic output may replace an existing path."""

    REFUSE = "refuse"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """Resource bounds checked before and immediately after decoding."""

    max_bytes: int = 16 * 1024 * 1024
    max_rows: int = 10_000


DEFAULT_DOCUMENT_LIMITS: Final = DocumentLimits()


class _DuplicateKeyError(ValueError):
    """Raised by the JSON/YAML boundaries when a mapping key repeats."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses silent last-key-wins mappings."""


class _ReadableYamlDumper(yaml.SafeDumper):
    """Safe dumper that renders multiline text as literal block scalars."""


def _represent_readable_string(dumper: _ReadableYamlDumper, value: str) -> ScalarNode:
    """Keep single-line strings compact and make multiline strings editable."""
    style = "|" if "\n" in value else None
    return dumper.represent_scalar(  # type: ignore[reportUnknownMemberType]  # rationale: PyYAML leaves the scalar value parameter untyped; this wrapper supplies str
        "tag:yaml.org,2002:str", value, style=style
    )


_ReadableYamlDumper.add_representer(str, _represent_readable_string)


def _mapping_node_pairs(node: MappingNode) -> list[tuple[Node, Node]]:
    """Narrow PyYAML's untyped node-pair collection at its wrapper boundary."""
    return list(node.value)  # type: ignore[reportAny]  # rationale: PyYAML exposes MappingNode.value as Any; MappingNode guarantees node pairs


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    *,
    deep: bool = False,
) -> dict[str, object]:  # lint-ignore[restricted-object]: PyYAML boundary  # lint-ignore[raw-dict]: narrowed below
    mapping = dict[str, object]()
    for key_node, value_node in _mapping_node_pairs(node):
        key: object = loader.construct_object(key_node, deep=deep)  # type: ignore[reportAny]  # rationale: PyYAML has an untyped construction boundary  # lint-ignore[restricted-object]: narrowed below
        if not isinstance(key, str):
            raise _DuplicateKeyError("document mapping keys must be strings")
        if key in mapping:
            raise _DuplicateKeyError(f"duplicate mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)  # type: ignore[reportAny]  # rationale: narrowed as a complete JSON-compatible document below
    return mapping


_UniqueKeyLoader.add_constructor(
    "tag:yaml.org,2002:map",
    _construct_unique_mapping,
)


def _unique_json_pairs(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: json object_pairs_hook boundary
    mapping: dict[str, JsonValue] = {}  # lint-ignore[raw-dict]: json object_pairs_hook boundary
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateKeyError(f"duplicate mapping key: {key!r}")
        mapping[key] = value
    return mapping


def _decode_json(data: bytes) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: decoded document boundary
    raw = json.loads(data, object_pairs_hook=_unique_json_pairs)  # type: ignore[reportAny]  # rationale: stdlib JSON returns Any; TypeAdapter narrows it immediately
    return _JSON_MAPPING.validate_python(raw)  # type: ignore[reportAny]  # rationale: raw exists only at the immediate validation boundary


def _decode_yaml(data: bytes) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: decoded document boundary
    text = data.decode("utf-8")
    tokens: tuple[Token, ...] = tuple(yaml.scan(text))  # type: ignore[reportUnknownMemberType,reportUnknownArgumentType]  # rationale: PyYAML scan is untyped; it contractually yields Token instances
    for token in tokens:
        if isinstance(token, (AliasToken, AnchorToken)):
            raise ValueError("YAML aliases and anchors are not supported")
    raw = yaml.load(text, Loader=_UniqueKeyLoader)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; TypeAdapter narrows it immediately  # noqa: S506
    return _JSON_MAPPING.validate_python(raw)  # type: ignore[reportAny]  # rationale: raw exists only at the immediate validation boundary


def _validation_error(document: Mapping[str, JsonValue], schema: Mapping[str, JsonValue]) -> str | None:
    validator = Draft202012Validator(schema)
    schema_errors: tuple[JsonSchemaValidationError, ...] = tuple(
        validator.iter_errors(document)  # type: ignore[reportUnknownMemberType,reportAny]  # rationale: jsonschema's validator protocol leaves error generators untyped; the concrete validator yields ValidationError
    )
    errors = sorted(schema_errors, key=lambda error: tuple(str(part) for part in error.absolute_path))
    if not errors:
        return None
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path) or "$"
    return f"document validation failed at {path}: {error.message}"


def decode_document(
    data: bytes,
    document_format: DocumentFormat,
    schema: Mapping[str, JsonValue],
    *,
    limits: DocumentLimits = DEFAULT_DOCUMENT_LIMITS,
) -> Result[dict[str, JsonValue], str]:  # lint-ignore[raw-dict]: validated external document
    """Decode one bounded document and validate it against its generated schema."""
    if len(data) > limits.max_bytes:
        return Err(f"document is too large: {len(data)} bytes (maximum {limits.max_bytes})")
    try:
        document = _decode_yaml(data) if document_format is DocumentFormat.YAML else _decode_json(data)
    except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, PydanticValidationError, ValueError) as exc:
        return Err(f"invalid {document_format.value} document: {exc}")

    vacancies = document.get("vacancies")
    if isinstance(vacancies, dict) and len(vacancies) > limits.max_rows:
        return Err(f"document has too many rows: {len(vacancies)} (maximum {limits.max_rows})")
    validation_error = _validation_error(document, schema)
    if validation_error is not None:
        return Err(validation_error)
    return Ok(document)


def encode_document(
    document: Mapping[str, JsonValue],
    document_format: DocumentFormat,
) -> Result[bytes, str]:
    """Encode one validated logical document without changing mapping order."""
    try:
        if document_format is DocumentFormat.JSON:
            return Ok((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
        text = yaml.dump(  # SafeDumper subclass; no object constructors are enabled
            dict(document),
            Dumper=_ReadableYamlDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        return Ok(text.encode())
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        return Err(f"failed to encode {document_format.value} document: {exc}")


def write_document(path: Path, content: bytes, overwrite_mode: OverwriteMode) -> Result[None, str]:
    """Atomically write sensitive document bytes, optionally replacing a file."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if overwrite_mode is OverwriteMode.REFUSE:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                return Err(f"output file already exists: {path} (use --force to replace it)")
            temporary_path.unlink()
            temporary_path = None
        else:
            os.replace(temporary_path, path)
            temporary_path = None
        return Ok(None)
    except OSError as exc:
        return Err(f"failed to write {path}: {exc}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
