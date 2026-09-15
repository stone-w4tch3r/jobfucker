"""Dependency-neutral contracts for registered generated schemas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class SchemaSource:
    """One typed schema source and every artifact path generated from it."""

    name: str
    schema_id: str
    model: type[BaseModel]
    artifact_paths: tuple[Path, ...]
