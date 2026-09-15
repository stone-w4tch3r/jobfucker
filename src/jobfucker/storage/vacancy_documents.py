"""SQLAlchemy client for atomic vacancy-document mutations and audit rows."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TypedDict

from rusty_results.prelude import Err, Ok, Result
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from jobfucker.app.vacancy_documents import (
    VacancyMutation,
    apply_vacancy_editable_values,
    vacancy_editable_value,
)

from .db import (
    DATABASE_NOW,
    SessionFactory,
    apply_vacancy_fields,
    vacancy_from_row,
    vacancy_to_row,
)
from .dto import VacancyRecord
from .models import AuditLog
from .models import Vacancy as VacancyRow


class _AuditDetails(TypedDict):
    source: str
    fields: list[str]
    soft_deleted: bool
    restored: bool


@dataclass(frozen=True, slots=True)
class _PreparedMutation:
    row: VacancyRow
    mutation: VacancyMutation


@dataclass(frozen=True, slots=True)
class SqlAlchemyVacancyDocumentStore:
    """Vacancy document store over one injected SQLAlchemy session factory."""

    session_factory: SessionFactory

    async def list_all(self) -> list[VacancyRecord]:
        """Return active and soft-deleted vacancies in stable database-id order."""
        async with self.session_factory() as session:
            rows = (await session.execute(select(VacancyRow).order_by(VacancyRow.id))).scalars().all()
            return [vacancy_from_row(row) for row in rows]

    async def apply(self, mutations: tuple[VacancyMutation, ...]) -> Result[None, str]:
        """Atomically commit vacancy updates plus per-row audits."""
        session = self.session_factory()
        try:
            async with session.begin():
                prepared: list[_PreparedMutation] = []
                for mutation in mutations:
                    row = await session.get(VacancyRow, mutation.vacancy_id)
                    if row is None:
                        return Err(f"{mutation.handle}: vacancy no longer exists; no changes were applied")
                    current = vacancy_from_row(row)
                    if current.pipeline_id != mutation.pipeline_id:
                        return Err(f"{mutation.handle}: vacancy identity changed; no changes were applied")
                    effective = tuple(
                        (field_name, value)
                        for field_name, value in mutation.changes
                        if vacancy_editable_value(current, field_name) != value
                    )
                    if effective:
                        prepared.append(_PreparedMutation(row=row, mutation=replace(mutation, changes=effective)))

                for item in prepared:
                    current = vacancy_from_row(item.row)
                    persisted_values = tuple(
                        (field_name, value) for field_name, value in item.mutation.changes if field_name != "deleted"
                    )
                    updated = apply_vacancy_editable_values(current, persisted_values)
                    apply_vacancy_fields(item.row, vacancy_to_row(updated))
                    item.row.updated_at = DATABASE_NOW
                    # Every manual mutation stamps the row as user-edited, so a
                    # later --refresh reads `user_stale` until the user's edits
                    # are superseded. Stages never set this column.
                    item.row.user_edited_at = DATABASE_NOW
                    deleted_value = next(
                        (value for field_name, value in item.mutation.changes if field_name == "deleted"), None
                    )
                    soft_deleted = False
                    restored = False
                    if deleted_value is not None:
                        assert isinstance(deleted_value, bool)
                        soft_deleted = current.soft_deleted_at is None and deleted_value
                        restored = current.soft_deleted_at is not None and not deleted_value
                        item.row.soft_deleted_at = None if restored else DATABASE_NOW

                    details: _AuditDetails = {
                        "source": "vacancies_document",
                        "fields": [field_name for field_name, _ in item.mutation.changes],
                        "soft_deleted": soft_deleted,
                        "restored": restored,
                    }
                    session.add(
                        AuditLog(
                            pipeline_id=item.mutation.pipeline_id,
                            pipeline_snapshot_id=None,
                            action="vacancies_document",
                            details=json.dumps(details, ensure_ascii=True, sort_keys=True),
                        )
                    )
            return Ok(None)
        except SQLAlchemyError as exc:
            await session.rollback()
            return Err(f"failed to apply vacancy document: {exc}")
        finally:
            await session.close()
