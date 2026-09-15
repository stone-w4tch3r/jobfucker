"""Vacancy-specific policy and application service for review documents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol

from pydantic import JsonValue, TypeAdapter, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.storage.dto import VacancyRecord
from jobfucker.table_documents.service import TableDocumentService

from .vacancy_document_schema import (
    EDITING_INSTRUCTIONS,
    VACANCY_SCHEMA_ID,
    VacancyContext,
    VacancyDocumentRow,
    VacancyDumpDocument,
    VacancyEditable,
)

_JSON_SCHEMA_MAPPING: Final = TypeAdapter(dict[str, JsonValue])  # lint-ignore[raw-dict]: packaged schema boundary
_SCHEMA_VERSION_KEY: Final = "x-jobfucker-schema-version"

type ScalarValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class VacancyPlanSummary:
    """Stable counts printed by preview and apply commands."""

    rows_considered: int
    changed: int
    unchanged: int
    soft_deleted: int
    restored: int
    ignored_read_only: int


@dataclass(frozen=True, slots=True)
class VacancyMutation:
    """One row's field changes passed to the atomic store."""

    handle: str
    vacancy_id: int
    pipeline_id: int
    changes: tuple[tuple[str, ScalarValue], ...]


@dataclass(frozen=True, slots=True)
class VacancyDocumentPlan:
    """Validated vacancy plan passed to the transactional store."""

    summary: VacancyPlanSummary
    mutations: tuple[VacancyMutation, ...]


class VacancyDocumentStore(Protocol):
    """Persistence port required by :class:`VacancyDocumentService`."""

    async def list_all(self) -> list[VacancyRecord]: ...

    async def apply(self, mutations: tuple[VacancyMutation, ...]) -> Result[None, str]: ...


def vacancy_handle(vacancy_id: int) -> str:
    """Return the stable opaque handle for one database vacancy id."""
    return f"vacancy_{vacancy_id}"


def _vacancy_id_from_handle(handle: str) -> int:
    """Parse the database vacancy id out of a validated document handle."""
    return int(handle.removeprefix("vacancy_"))


def vacancy_editable_value(vacancy: VacancyRecord, field_name: str) -> ScalarValue:
    """Project one logical editable field from a stored vacancy."""
    match field_name:
        case "score":
            return vacancy.score
        case "score_reasoning":
            return vacancy.score_reasoning
        case "cover_letter":
            return vacancy.cover_letter
        case "notes":
            return vacancy.notes
        case "manual_skip":
            return vacancy.manual_skip
        case "manual_skip_reason":
            return vacancy.manual_skip_reason
        case "deleted":
            return vacancy.soft_deleted_at is not None
        case _:
            raise ValueError(f"unknown vacancy editable field: {field_name}")


def apply_vacancy_editable_values(
    vacancy: VacancyRecord,
    values: tuple[tuple[str, ScalarValue], ...],
) -> VacancyRecord:
    """Apply validated persisted-field values through the shared mutation policy."""
    updated = vacancy
    for field_name, value in values:
        match field_name:
            case "score":
                if value is not None and type(value) is not int:
                    raise TypeError("validated score must be int or None")
                updated = replace(updated, score=value)
            case "score_reasoning":
                if value is not None and not isinstance(value, str):
                    raise TypeError("validated score_reasoning must be str or None")
                updated = replace(updated, score_reasoning=value)
            case "cover_letter":
                if value is not None and not isinstance(value, str):
                    raise TypeError("validated cover_letter must be str or None")
                updated = replace(updated, cover_letter=value)
            case "notes":
                if value is not None and not isinstance(value, str):
                    raise TypeError("validated notes must be str or None")
                updated = replace(updated, notes=value)
            case "manual_skip":
                if not isinstance(value, bool):
                    raise TypeError("validated manual_skip must be bool")
                updated = replace(updated, manual_skip=value)
            case "manual_skip_reason":
                if value is not None and not isinstance(value, str):
                    raise TypeError("validated manual_skip_reason must be str or None")
                updated = replace(updated, manual_skip_reason=value)
            case "deleted":
                raise ValueError("deleted is a database-time projection and must be applied by the store")
            case _:
                raise ValueError(f"unknown vacancy editable field: {field_name}")
    return updated


def _stale_flags(vacancy: VacancyRecord) -> tuple[bool, bool, bool]:
    """Computed staleness flags (score/letter/user) for the read-only context.

    Same-format ``YYYY-MM-DD HH:MM:SS`` UTC strings compare lexicographically;
    a missing artifact timestamp is not stale, only absent.
    """

    def stale(fetched_at: str | None, artifact_at: str | None) -> bool:
        return fetched_at is not None and artifact_at is not None and fetched_at > artifact_at

    return (
        stale(vacancy.fetched_at, vacancy.scored_at),
        stale(vacancy.fetched_at, vacancy.generated_at),
        stale(vacancy.fetched_at, vacancy.user_edited_at),
    )


def _document_row(vacancy: VacancyRecord) -> VacancyDocumentRow:
    score_stale, letter_stale, user_stale = _stale_flags(vacancy)
    return VacancyDocumentRow(
        context_edit_not_allowed=VacancyContext(
            pipeline_id=vacancy.pipeline_id,
            external_id=vacancy.external_id,
            created_at=vacancy.created_at,
            updated_at=vacancy.updated_at,
            fetched_at=vacancy.fetched_at,
            scored_at=vacancy.scored_at,
            generated_at=vacancy.generated_at,
            user_edited_at=vacancy.user_edited_at,
            score_stale=score_stale,
            letter_stale=letter_stale,
            user_stale=user_stale,
            title=vacancy.title,
            url=vacancy.url,
            company=vacancy.company,
            description=vacancy.description,
            salary=vacancy.salary,
            score_error=vacancy.score_error,
            cover_letter_error=vacancy.cover_letter_error,
            apply_status=vacancy.apply_status,
            apply_error=vacancy.apply_error,
            skip_reason=vacancy.skip_reason,
            applied_at=vacancy.applied_at,
        ),
        editable=VacancyEditable(
            score=vacancy.score,
            score_reasoning=vacancy.score_reasoning,
            cover_letter=vacancy.cover_letter,
            notes=vacancy.notes,
            manual_skip=vacancy.manual_skip,
            manual_skip_reason=vacancy.manual_skip_reason,
            deleted=vacancy.soft_deleted_at is not None,
        ),
    )


def _incoming_values(editable: VacancyEditable) -> tuple[tuple[str, ScalarValue], ...]:
    present = editable.model_fields_set
    values: list[tuple[str, ScalarValue]] = []
    if "score" in present:
        values.append(("score", editable.score))
    if "score_reasoning" in present:
        values.append(("score_reasoning", editable.score_reasoning))
    if "cover_letter" in present:
        values.append(("cover_letter", editable.cover_letter))
    if "notes" in present:
        values.append(("notes", editable.notes))
    if "manual_skip" in present:
        values.append(("manual_skip", editable.manual_skip))
    if "manual_skip_reason" in present:
        values.append(("manual_skip_reason", editable.manual_skip_reason))
    if "deleted" in present:
        values.append(("deleted", editable.deleted))
    return tuple(values)


def _ignored_context_fields(context: VacancyContext, vacancy: VacancyRecord) -> tuple[str, ...]:
    score_stale, letter_stale, user_stale = _stale_flags(vacancy)
    differences: list[str] = []
    comparisons = (
        ("pipeline_id", context.pipeline_id, vacancy.pipeline_id),
        ("external_id", context.external_id, vacancy.external_id),
        ("created_at", context.created_at, vacancy.created_at),
        ("updated_at", context.updated_at, vacancy.updated_at),
        ("fetched_at", context.fetched_at, vacancy.fetched_at),
        ("scored_at", context.scored_at, vacancy.scored_at),
        ("generated_at", context.generated_at, vacancy.generated_at),
        ("user_edited_at", context.user_edited_at, vacancy.user_edited_at),
        ("score_stale", context.score_stale, score_stale),
        ("letter_stale", context.letter_stale, letter_stale),
        ("user_stale", context.user_stale, user_stale),
        ("title", context.title, vacancy.title),
        ("url", context.url, vacancy.url),
        ("company", context.company, vacancy.company),
        ("description", context.description, vacancy.description),
        ("salary", context.salary, vacancy.salary),
        ("score_error", context.score_error, vacancy.score_error),
        ("cover_letter_error", context.cover_letter_error, vacancy.cover_letter_error),
        ("apply_status", context.apply_status, vacancy.apply_status),
        ("apply_error", context.apply_error, vacancy.apply_error),
        ("skip_reason", context.skip_reason, vacancy.skip_reason),
        ("applied_at", context.applied_at, vacancy.applied_at),
    )
    for field_name, incoming_value, current_value in comparisons:
        if incoming_value != current_value:
            differences.append(field_name)
    return tuple(differences)


def _load_packaged_schema() -> Result[dict[str, JsonValue], str]:  # lint-ignore[raw-dict]: packaged schema boundary
    schema_path = Path(__file__).parents[1] / "resources" / "schemas" / "vacancies-dump.schema.json"
    try:
        return Ok(_JSON_SCHEMA_MAPPING.validate_json(schema_path.read_bytes()))
    except (OSError, ValidationError) as exc:
        return Err(f"failed to load packaged vacancy schema: {exc}")


class VacancyDocumentPolicy:
    """Vacancy projections, identity rules, planning, and persistence hooks.

    ``pipeline_id`` optionally scopes every dump and plan to one pipeline
    (``None`` = all pipelines); apply stays document-driven.
    """

    def __init__(
        self,
        store: VacancyDocumentStore,
        schema: Mapping[str, JsonValue],
        pipeline_id: int | None = None,
    ) -> None:
        self._store = store
        self._schema = schema
        self._pipeline_id = pipeline_id

    @property
    def schema(self) -> Mapping[str, JsonValue]:
        """Return the packaged generated schema used by the codec boundary."""
        return self._schema

    async def dump(self) -> Result[VacancyDumpDocument, str]:
        """Build a document from every active and soft-deleted row in scope.

        Scope is every pipeline unless the policy was created with
        ``pipeline_id``, in which case only that pipeline's rows are included.
        """
        rows = await self._store.list_all()
        if self._pipeline_id is not None:
            rows = [row for row in rows if row.pipeline_id == self._pipeline_id]
        rows = sorted(rows, key=lambda vacancy: vacancy.id)
        visible_rows = {vacancy_handle(row.id): _document_row(row) for row in rows}
        document = VacancyDumpDocument.model_validate(
            {
                "$schema": VACANCY_SCHEMA_ID,
                "editing_instructions_edit_not_allowed": EDITING_INSTRUCTIONS,
                "vacancies": visible_rows,
            }
        )
        return Ok(document)

    def to_json_document(
        self,
        document: VacancyDumpDocument,
    ) -> Result[dict[str, JsonValue], str]:  # lint-ignore[raw-dict]: validated document boundary
        """Serialize the typed vacancy document for the generic codec."""
        return Ok(
            _JSON_SCHEMA_MAPPING.validate_python(
                document.model_dump(by_alias=True, mode="json")  # type: ignore[reportAny]  # rationale: pydantic model_dump exposes Any values; TypeAdapter narrows the complete document
            )
        )

    def from_json_document(self, document: Mapping[str, JsonValue]) -> Result[VacancyDumpDocument, str]:
        """Narrow a schema-valid JSON mapping to the typed vacancy document."""
        try:
            return Ok(VacancyDumpDocument.model_validate(document))
        except ValidationError as exc:
            # The generated JSON Schema and this model have one typed source;
            # reaching this branch means artifact/source drift, not user error.
            return Err(f"vacancy schema artifact is inconsistent with its typed source: {exc}")

    def row_count(self, document: VacancyDumpDocument) -> int:
        """Return the visible vacancy count for CLI reporting."""
        return len(document.vacancies)

    async def plan(self, document: VacancyDumpDocument) -> Result[VacancyDocumentPlan, str]:
        """Resolve identities and produce a complete field-level merge plan."""
        if document.schema_url != VACANCY_SCHEMA_ID:
            return Err(f"unsupported vacancy schema URL: {document.schema_url}")

        current_rows_list = await self._store.list_all()
        if self._pipeline_id is not None:
            current_rows_list = [row for row in current_rows_list if row.pipeline_id == self._pipeline_id]
        current_rows = {row.id: row for row in current_rows_list}

        mutations: list[VacancyMutation] = []
        changed_handles: set[str] = set()
        soft_deleted = 0
        restored = 0
        ignored_read_only = 0

        for handle, document_row in document.vacancies.items():
            vacancy_id = _vacancy_id_from_handle(handle)
            current = current_rows.get(vacancy_id)
            if current is None:
                return Err(f"{handle}: vacancy identity does not exist in the current database")

            changes: list[tuple[str, ScalarValue]] = []
            for field_name, incoming_value in _incoming_values(document_row.editable):
                if vacancy_editable_value(current, field_name) != incoming_value:
                    changes.append((field_name, incoming_value))
                    if field_name == "deleted":
                        assert isinstance(incoming_value, bool)
                        was_deleted = current.soft_deleted_at is not None
                        if not was_deleted and incoming_value:
                            soft_deleted += 1
                        elif was_deleted and not incoming_value:
                            restored += 1

            ignored = _ignored_context_fields(document_row.context_edit_not_allowed, current)
            if ignored:
                ignored_read_only += len(ignored)
            if changes:
                changed_handles.add(handle)
                mutations.append(
                    VacancyMutation(
                        handle=handle,
                        vacancy_id=current.id,
                        pipeline_id=current.pipeline_id,
                        changes=tuple(changes),
                    )
                )

        return Ok(
            VacancyDocumentPlan(
                summary=VacancyPlanSummary(
                    rows_considered=len(document.vacancies),
                    changed=len(changed_handles),
                    unchanged=len(document.vacancies) - len(changed_handles),
                    soft_deleted=soft_deleted,
                    restored=restored,
                    ignored_read_only=ignored_read_only,
                ),
                mutations=tuple(mutations),
            )
        )

    async def apply(self, plan: VacancyDocumentPlan) -> Result[VacancyPlanSummary, str]:
        """Apply an already validated plan through the atomic persistence port."""
        if not plan.mutations:
            return Ok(plan.summary)
        result = await self._store.apply(plan.mutations)
        if result.is_err:
            return Err(result.unwrap_err())
        return Ok(plan.summary)


class VacancyDocumentService(TableDocumentService[VacancyDumpDocument, VacancyDocumentPlan, VacancyPlanSummary]):
    """Reusable table-document orchestration configured by the vacancy policy."""

    @classmethod
    def create(cls, store: VacancyDocumentStore, pipeline_id: int | None = None) -> Result[VacancyDocumentService, str]:
        """Create a service from the packaged generated schema resource.

        ``pipeline_id`` optionally scopes dump/plan to one pipeline
        (``None`` = all pipelines).
        """
        schema_result = _load_packaged_schema()
        if schema_result.is_err:
            return Err(schema_result.unwrap_err())
        schema = schema_result.unwrap()
        version = schema.get(_SCHEMA_VERSION_KEY)
        if not isinstance(version, str):
            return Err(f"packaged vacancy schema has no {_SCHEMA_VERSION_KEY}")
        return Ok(cls(VacancyDocumentPolicy(store, schema, pipeline_id)))
