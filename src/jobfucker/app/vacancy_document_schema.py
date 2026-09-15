"""Typed source for the versioned vacancy review-document schema."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from jobfucker.schema_source import SchemaSource

VACANCY_SCHEMA_ID: Final = (
    "https://raw.githubusercontent.com/example-org/jobfucker/main/docs/schemas/vacancies-dump.schema.json"
)
EDITING_INSTRUCTIONS: Final = """Edit values only inside `editable` blocks.

You may remove entries from `vacancies` to select a smaller apply batch.
Removing an entry does not delete it from the database.

Set `deleted` to true to soft-delete a vacancy. Set it to false to restore
a soft-deleted vacancy.

Blocks ending in `_edit_not_allowed` are informational or internal.
Preview changes with:
  jobfucker vacancies apply --source FILE --dry-run"""

EDITABLE_FIELD_NAMES: Final = (
    "score",
    "score_reasoning",
    "cover_letter",
    "notes",
    "manual_skip",
    "manual_skip_reason",
)
CONTEXT_FIELD_NAMES: Final = (
    "pipeline_id",
    "external_id",
    "created_at",
    "updated_at",
    "fetched_at",
    "scored_at",
    "generated_at",
    "user_edited_at",
    "score_stale",
    "letter_stale",
    "user_stale",
    "title",
    "url",
    "company",
    "description",
    "salary",
    "score_error",
    "cover_letter_error",
    "apply_status",
    "apply_error",
    "skip_reason",
    "applied_at",
)

VacancyHandle = Annotated[str, StringConstraints(pattern=r"^vacancy_[1-9][0-9]*$")]


class _StrictDocumentModel(BaseModel):
    """Strict, closed model used by every vacancy-document block."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class VacancyContext(_StrictDocumentModel):
    """Read-only vacancy values included to make review useful."""

    pipeline_id: int
    external_id: str
    created_at: str
    updated_at: str
    fetched_at: str | None  # last fetch/refresh stamp; drives the stale flags
    scored_at: str | None  # last score-stage write stamp
    generated_at: str | None  # last cover-letter write stamp
    user_edited_at: str | None  # last manual-mutation stamp
    score_stale: bool  # fetched_at > scored_at (a refresh marked the score stale)
    letter_stale: bool  # fetched_at > generated_at (a refresh marked the letter stale)
    user_stale: bool  # fetched_at > user_edited_at (a refresh marked manual edits stale)
    title: str
    url: str
    company: str | None
    description: str
    salary: str | None
    score_error: str | None
    cover_letter_error: str | None
    apply_status: Literal["pending", "applied", "skipped", "error"] | None
    apply_error: str | None
    skip_reason: str | None
    applied_at: str | None


class VacancyEditable(_StrictDocumentModel):
    """Partial user-editable projection; omitted fields remain out of the batch."""

    score: int | None = Field(default=None, ge=1, le=5)
    score_reasoning: str | None = None
    cover_letter: str | None = None
    notes: str | None = None
    manual_skip: bool = False
    manual_skip_reason: str | None = None
    deleted: bool = False


class VacancyDocumentRow(_StrictDocumentModel):
    """One visible vacancy entry."""

    context_edit_not_allowed: VacancyContext
    editable: VacancyEditable


class VacancyDumpDocument(_StrictDocumentModel):
    """Versioned vacancy review document shared by YAML and JSON encodings."""

    schema_url: str = Field(alias="$schema", json_schema_extra={"const": VACANCY_SCHEMA_ID})
    editing_instructions_edit_not_allowed: str = Field(json_schema_extra={"const": EDITING_INSTRUCTIONS})
    vacancies: Mapping[VacancyHandle, VacancyDocumentRow] = Field(json_schema_extra={"additionalProperties": False})


VACANCY_SCHEMA_SOURCE: Final = SchemaSource(
    name="vacancies-dump",
    schema_id=VACANCY_SCHEMA_ID,
    model=VacancyDumpDocument,
    artifact_paths=(
        Path("docs/schemas/vacancies-dump.schema.json"),
        Path("src/jobfucker/resources/schemas/vacancies-dump.schema.json"),
    ),
)
