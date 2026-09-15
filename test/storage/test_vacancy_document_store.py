"""Storage transaction tests for vacancy-document mutations and audit rows.

Scenario: audit failure rolls back the whole document batch
  Given two planned vacancy edits and a database trigger that rejects audit rows
  When the document plan is applied
  Then apply fails and neither vacancy edit is committed
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text

from jobfucker.app.vacancy_document_schema import VacancyDumpDocument
from jobfucker.app.vacancy_documents import VacancyDocumentService
from jobfucker.storage.db import Storage
from jobfucker.storage.vacancy_documents import SqlAlchemyVacancyDocumentStore
from test.storage.builders import make_pipeline, make_vacancy


def _edit_notes(document: VacancyDumpDocument, updates: Mapping[str, str]) -> VacancyDumpDocument:
    rows = dict(document.vacancies)
    for handle, notes in updates.items():
        row = rows[handle]
        rows[handle] = row.model_copy(update={"editable": row.editable.model_copy(update={"notes": notes})})
    return document.model_copy(update={"vacancies": rows})


async def test_audit_failure_rolls_back_every_vacancy_change(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    first = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="first"))
    second = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="second"))
    store = SqlAlchemyVacancyDocumentStore(storage.session_factory)
    service_result = VacancyDocumentService.create(store)
    assert service_result.is_ok
    service = service_result.unwrap()
    document = (await service.dump()).unwrap()
    edited = _edit_notes(
        document,
        {f"vacancy_{first.id}": "first edit", f"vacancy_{second.id}": "second edit"},
    )
    plan = (await service.plan(edited)).unwrap()
    async with storage.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TRIGGER reject_document_audit BEFORE INSERT ON audit_log "
                "WHEN NEW.action = 'vacancies_document' BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
            )
        )

    result = await service.apply(plan)

    assert result.is_err
    assert "injected audit failure" in result.unwrap_err()
    assert (await storage.vacancies.get(first.id)).notes is None  # type: ignore[union-attr]  # rationale: rollback preserves seeded row
    assert (await storage.vacancies.get(second.id)).notes is None  # type: ignore[union-attr]  # rationale: rollback preserves seeded row
    assert await storage.audit_log.list() == []
