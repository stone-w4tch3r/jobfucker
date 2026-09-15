"""Deterministic generation of registered JSON Schema artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter

from jobfucker.schema_source import SchemaSource

_JSON_SCHEMA_MAPPING: Final = TypeAdapter(dict[str, JsonValue])  # lint-ignore[raw-dict]: JSON Schema boundary
_SCHEMA_VERSION_KEY: Final = "x-jobfucker-schema-version"


@dataclass(frozen=True, slots=True)
class GeneratedSchema:
    """Rendered JSON Schema bytes and their content-derived version."""

    content: bytes
    version: str


def _canonical_schema_bytes(schema: dict[str, JsonValue]) -> bytes:  # lint-ignore[raw-dict]: schema boundary
    """Return canonical bytes while excluding the derived version extension."""
    unversioned = {key: value for key, value in schema.items() if key != _SCHEMA_VERSION_KEY}
    return json.dumps(unversioned, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def derive_schema_version(schema: dict[str, JsonValue]) -> str:  # lint-ignore[raw-dict]: schema boundary
    """Derive a stable SHA-256 version from schema semantics, without recursion."""
    return f"sha256:{hashlib.sha256(_canonical_schema_bytes(schema)).hexdigest()}"


def generate_schema(source: SchemaSource) -> GeneratedSchema:
    """Generate one Draft 2020-12 schema from its typed Pydantic source."""
    schema = _JSON_SCHEMA_MAPPING.validate_python(
        source.model.model_json_schema(by_alias=True, mode="validation")  # type: ignore[reportAny]  # rationale: pydantic exposes schema values as Any; TypeAdapter narrows the complete mapping
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = source.schema_id
    version = derive_schema_version(schema)
    schema[_SCHEMA_VERSION_KEY] = version
    content = (json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    return GeneratedSchema(content=content, version=version)


def generate_all_schemas(project_root: Path) -> tuple[Path, ...]:
    """Generate every registered schema under ``project_root``."""
    from jobfucker.app.pipeline_schema_source import PIPELINE_SCHEMA_SOURCE
    from jobfucker.app.vacancy_document_schema import VACANCY_SCHEMA_SOURCE

    written: list[Path] = []
    for source in (VACANCY_SCHEMA_SOURCE, PIPELINE_SCHEMA_SOURCE):
        generated = generate_schema(source)
        for relative_path in source.artifact_paths:
            output_path = project_root / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(generated.content)
            written.append(output_path)
    return tuple(written)


def main() -> None:
    """Generate all registered schemas in the current project checkout."""
    generate_all_schemas(Path.cwd())


if __name__ == "__main__":
    main()
