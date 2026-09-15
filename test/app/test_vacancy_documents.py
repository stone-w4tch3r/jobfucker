"""Application/integration tests for vacancy review documents.

Scenario: a filtered document applies only its remaining vacancy
  Given two stored vacancies and a dump containing both
  When one visible entry is removed and the other entry's notes are edited
  Then only the remaining vacancy is changed and the orphan row is skipped

Scenario: planning produces a direct field overwrite
  Given a dumped vacancy
  When the document changes an editable field
  Then the change is applied verbatim over the current database value

Scenario: pipeline identity and timestamps are visible review context
  Given a stored vacancy belonging to a pipeline with creation and update times
  When the vacancy document is dumped
  Then its pipeline id and timestamps appear in read-only context

Scenario: a pipeline-scoped service sees only its own pipeline
  Given two pipelines with vacancies and one scoped service
  When it dumps and edits a document
  Then the dump holds only scoped rows and the scoped plan ignores foreign documents
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import text

from jobfucker.app.vacancy_document_schema import VacancyDumpDocument
from jobfucker.app.vacancy_documents import VacancyDocumentService
from jobfucker.storage.db import Storage
from jobfucker.storage.vacancy_documents import SqlAlchemyVacancyDocumentStore
from jobfucker.table_documents.codec import DocumentFormat
from test.storage.builders import make_pipeline, make_vacancy


def _service(storage: Storage) -> VacancyDocumentService:
    result = VacancyDocumentService.create(SqlAlchemyVacancyDocumentStore(storage.session_factory))
    assert result.is_ok
    return result.unwrap()


def _service_scoped(storage: Storage, pipeline_id: int) -> VacancyDocumentService:
    result = VacancyDocumentService.create(SqlAlchemyVacancyDocumentStore(storage.session_factory), pipeline_id)
    assert result.is_ok
    return result.unwrap()


def _edit_document(
    document: VacancyDumpDocument,
    handle: str,
    **editable_updates: str | int | bool | None,
) -> VacancyDumpDocument:
    row = document.vacancies[handle]
    edited_row = row.model_copy(update={"editable": row.editable.model_copy(update=editable_updates)})
    return document.model_copy(update={"vacancies": {**document.vacancies, handle: edited_row}})


async def test_dump_contains_active_and_soft_deleted_rows_in_id_order(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    active = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="active"))
    deleted = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="deleted"))
    # Manual lifecycle: soft-delete the second row the way a user action would.
    async with storage.engine.begin() as conn:
        await conn.execute(
            text("UPDATE vacancies SET soft_deleted_at = '2026-08-05 10:00:00' WHERE id = :id"),
            {"id": deleted.id},
        )

    dump_result = await _service(storage).dump()

    assert dump_result.is_ok
    document = dump_result.unwrap()
    assert tuple(document.vacancies) == (f"vacancy_{active.id}", f"vacancy_{deleted.id}")
    assert document.vacancies[f"vacancy_{active.id}"].editable.deleted is False
    assert document.vacancies[f"vacancy_{deleted.id}"].editable.deleted is True
    active_handle = f"vacancy_{active.id}"
    context = document.vacancies[active_handle].context_edit_not_allowed
    assert context.pipeline_id == pipeline.id
    assert context.created_at == active.created_at
    assert context.updated_at == active.updated_at
    assert tuple(document.model_dump(by_alias=True)) == (
        "$schema",
        "editing_instructions_edit_not_allowed",
        "vacancies",
    )


async def test_filtered_subset_changes_only_remaining_row_and_skips_orphan_row(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    first = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="first"))
    second = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, external_id="second"))
    service = _service(storage)
    document = (await service.dump()).unwrap()
    first_handle = f"vacancy_{first.id}"
    edited = _edit_document(document, first_handle, notes="reviewed")
    filtered = edited.model_copy(update={"vacancies": {first_handle: edited.vacancies[first_handle]}})

    plan_result = await service.plan(filtered)

    assert plan_result.is_ok
    plan = plan_result.unwrap()
    assert plan.summary.rows_considered == 1
    assert plan.summary.changed == 1
    assert plan.summary.unchanged == 0
    apply_result = await service.apply(plan)
    assert apply_result.is_ok
    assert (await storage.vacancies.get(first.id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must still exist
    assert (await storage.vacancies.get(second.id)).notes is None  # type: ignore[union-attr]  # rationale: seeded row must still exist


async def test_read_only_context_edits_are_ignored_and_reported(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    document = (await service.dump()).unwrap()
    handle = f"vacancy_{vacancy.id}"
    row = document.vacancies[handle]
    edited_context = row.context_edit_not_allowed.model_copy(update={"title": "Changed only in document"})
    edited_row = row.model_copy(update={"context_edit_not_allowed": edited_context})
    edited_document = document.model_copy(update={"vacancies": {handle: edited_row}})

    plan = (await service.plan(edited_document)).unwrap()

    assert plan.summary.changed == 0
    assert plan.summary.unchanged == 1
    assert plan.summary.ignored_read_only == 1


async def test_document_change_overwrites_current_database_value(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, notes="base"))
    service = _service(storage)
    document = (await service.dump()).unwrap()
    edited_document = _edit_document(document, f"vacancy_{vacancy.id}", notes="from document")
    await storage.vacancies.update(replace(vacancy, notes="from database"))

    plan = (await service.plan(edited_document)).unwrap()

    assert plan.summary.changed == 1
    assert (await service.apply(plan)).is_ok
    persisted = await storage.vacancies.get(vacancy.id)
    assert persisted is not None
    assert persisted.notes == "from document"


async def test_soft_delete_and_restore_are_planned_and_audited(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    delete_document = _edit_document((await service.dump()).unwrap(), f"vacancy_{vacancy.id}", deleted=True)

    delete_plan = (await service.plan(delete_document)).unwrap()
    assert delete_plan.summary.soft_deleted == 1
    assert (await service.apply(delete_plan)).is_ok
    deleted = await SqlAlchemyVacancyDocumentStore(storage.session_factory).list_all()
    assert deleted[0].soft_deleted_at is not None

    restore_document = _edit_document((await service.dump()).unwrap(), f"vacancy_{vacancy.id}", deleted=False)
    restore_plan = (await service.plan(restore_document)).unwrap()
    assert restore_plan.summary.restored == 1
    assert (await service.apply(restore_plan)).is_ok
    restored = await SqlAlchemyVacancyDocumentStore(storage.session_factory).list_all()
    assert restored[0].soft_deleted_at is None

    audit = await storage.audit_log.list()
    assert [entry.action for entry in audit] == ["vacancies_document", "vacancies_document"]


async def test_unknown_document_handle_is_rejected(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    document = (await service.dump()).unwrap()
    handle = next(iter(document.vacancies))
    renamed_document = document.model_copy(
        update={
            "vacancies": {
                "vacancy_999": document.vacancies[handle],
                **{other: other_row for other, other_row in document.vacancies.items() if other != handle},
            }
        }
    )

    result = await service.plan(renamed_document)

    assert result.is_err
    assert "vacancy_999" in result.unwrap_err()
    assert "identity" in result.unwrap_err()


async def test_missing_editable_fields_are_ignored_and_explicit_null_clears(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id, score=4, notes="clear me"))
    service = _service(storage)
    document = (await service.dump()).unwrap()
    handle = f"vacancy_{vacancy.id}"
    row = document.vacancies[handle]
    partial_editable = row.editable.model_validate({"notes": None})
    partial_row = row.model_copy(update={"editable": partial_editable})
    partial_document = document.model_copy(update={"vacancies": {handle: partial_row}})

    plan = (await service.plan(partial_document)).unwrap()
    assert list(plan.mutations[0].changes) == [("notes", None)]
    assert (await service.apply(plan)).is_ok
    persisted = await storage.vacancies.get(vacancy.id)
    assert persisted is not None
    assert persisted.notes is None
    assert persisted.score == 4
    assert persisted.soft_deleted_at is None


async def test_schema_rejects_unknown_editable_field_and_malformed_handle(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    encoded = (await service.dump_encoded(DocumentFormat.JSON)).unwrap().content

    unknown_field = encoded.replace(b'"score": null,', b'"scroe": 4,\n        "score": null,', 1)
    malformed_handle = encoded.replace(f'"vacancy_{vacancy.id}"'.encode(), b'"bad_handle"')

    unknown_result = await service.plan_encoded(unknown_field, DocumentFormat.JSON)
    handle_result = await service.plan_encoded(malformed_handle, DocumentFormat.JSON)
    assert unknown_result.is_err and "scroe" in unknown_result.unwrap_err()
    assert handle_result.is_err and "bad_handle" in handle_result.unwrap_err()


async def test_manual_edit_sets_user_edited_at_but_stages_do_not(storage: Storage) -> None:
    """A manual mutation stamps ``user_edited_at``; stage writes never do.

    The document apply path is a manual mutation: applying an editable change
    bumps ``user_edited_at`` so a later ``--refresh`` reads ``user_stale`` until
    the user's edits are superseded. Direct ``update`` (the stages' write path)
    must leave it untouched.
    """
    pipeline = await storage.pipelines.create(make_pipeline())
    vacancy = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    document = _edit_document((await service.dump()).unwrap(), f"vacancy_{vacancy.id}", notes="user note")

    plan = (await service.plan(document)).unwrap()
    assert plan.summary.changed == 1
    assert (await service.apply(plan)).is_ok

    edited = await storage.vacancies.get(vacancy.id)
    assert edited is not None and edited.user_edited_at is not None
    assert edited.notes == "user note"

    # A stage-style write (VacancyRepository.update) never touches user_edited_at.
    await storage.vacancies.update(replace(edited, score=4))
    after = await storage.vacancies.get(vacancy.id)
    assert after is not None
    assert after.score == 4
    assert after.user_edited_at == edited.user_edited_at  # unchanged by the stage write


async def test_unchanged_document_writes_no_audit_entries(storage: Storage) -> None:
    pipeline = await storage.pipelines.create(make_pipeline())
    await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id))
    service = _service(storage)
    plan = (await service.plan((await service.dump()).unwrap())).unwrap()

    assert plan.summary.changed == 0
    assert (await service.apply(plan)).is_ok
    assert await storage.audit_log.list() == []


async def test_pipeline_scoped_service_dumps_only_its_pipeline(storage: Storage) -> None:
    pipeline_a = await storage.pipelines.create(make_pipeline(name="a"))
    pipeline_b = await storage.pipelines.create(make_pipeline(name="b"))
    active = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_a.id, external_id="a1"))
    deleted = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_a.id, external_id="a2"))
    await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_b.id, external_id="b1"))
    # Manual lifecycle: soft-delete one scoped row the way a user action would.
    async with storage.engine.begin() as conn:
        await conn.execute(
            text("UPDATE vacancies SET soft_deleted_at = '2026-08-05 10:00:00' WHERE id = :id"),
            {"id": deleted.id},
        )

    document = (await _service_scoped(storage, pipeline_a.id).dump()).unwrap()

    assert tuple(document.vacancies) == (f"vacancy_{active.id}", f"vacancy_{deleted.id}")
    assert len((await _service(storage).dump()).unwrap().vacancies) == 3


async def test_pipeline_scoped_service_round_trips_only_its_pipeline(storage: Storage) -> None:
    pipeline_a = await storage.pipelines.create(make_pipeline(name="a"))
    pipeline_b = await storage.pipelines.create(make_pipeline(name="b"))
    vacancy_a = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_a.id, external_id="a1"))
    vacancy_b = await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_b.id, external_id="b1"))
    service = _service_scoped(storage, pipeline_a.id)
    document = _edit_document((await service.dump()).unwrap(), f"vacancy_{vacancy_a.id}", notes="scoped")

    plan = (await service.plan(document)).unwrap()
    assert plan.summary.changed == 1
    assert (await service.apply(plan)).is_ok

    assert (await storage.vacancies.get(vacancy_a.id)).notes == "scoped"  # type: ignore[union-attr]  # rationale: seeded row must still exist
    assert (await storage.vacancies.get(vacancy_b.id)).notes is None  # type: ignore[union-attr]  # rationale: seeded row must still exist


async def test_scoped_plan_rejects_document_rows_from_other_pipelines(storage: Storage) -> None:
    pipeline_a = await storage.pipelines.create(make_pipeline(name="a"))
    pipeline_b = await storage.pipelines.create(make_pipeline(name="b"))
    await storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_b.id, external_id="b1"))
    foreign_document = (await _service(storage).dump()).unwrap()

    result = await _service_scoped(storage, pipeline_a.id).plan(foreign_document)

    assert result.is_err
    assert "identity" in result.unwrap_err()
