"""Asynchronous SQLAlchemy engine/session factory + repositories.

This is the **only** module in the app that owns SQLAlchemy outside ``models.py``.
It hides all ORM/``AsyncSession`` mechanics behind small repository classes that
map rows to the frozen domain DTOs in :mod:`dto`. Engine/CLI/UI never import
``sqlalchemy`` (ruff ``banned-api``) and never see ORM objects.

Design notes:
- Engine is **injectable**: ``create_async_sqlite_engine(path)`` for a real file
  DB (the composition root passes the resolved ``jobfucker.db``) and
  ``storage_from_engine(engine)`` for tests that supply an in-memory engine
  (``sqlite+aiosqlite:///:memory:`` via the ``in_memory_db`` fixture).
- Repositories are bound to an :class:`sqlalchemy.ext.asyncio.async_sessionmaker`
  and commit on success / roll back on error per public method (each method is
  one unit of work), via the ``async with session_factory()`` lifecycle. This
  keeps atomic operations (daily-limit increment, snapshot append) correct and
  makes callers side-effect free.
- The five tables' schema lives in :mod:`models` (``Base.metadata``); tests build
  it with ``await create_schema(engine)`` or via the Alembic migration path.
- **Loop safety:** aiosqlite connections are loop-independent (each runs on its
  own background thread with a private event loop), so an ``AsyncEngine`` works
  under ``asyncio.run`` one-shots, pytest-asyncio, and a live qasync loop. The
  one invariant: always ``await engine.dispose()`` before the loop/process
  closes (see the async-migration spec §2.2).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol, TypeIs

from rusty_results.prelude import Err, Ok, Result
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jobfucker.clients.base import ServiceVacancyId

from .dto import (
    ApplyStatus,
    AuditLogEntry,
    DailyLimit,
    Pipeline,
    PipelineSnapshot,
    VacancyRecord,
)
from .models import AuditLog, Base
from .models import DailyLimit as DailyLimitRow
from .models import Pipeline as PipelineRow
from .models import PipelineSnapshot as PipelineSnapshotRow
from .models import Vacancy as VacancyRow

SessionFactory = async_sessionmaker[AsyncSession]

# SQLite now() stamp for explicit timestamp writes (soft-delete, repoint, bump).
# This must stay a SQL expression — a literal str here would store the text
# ``"(datetime('now'))"`` instead of a real timestamp. ``func.datetime("now")``
# renders exactly ``datetime('now')`` (the same expression the models use as a
# server default) and is fully typed, unlike a raw ``text()`` clause which
# basedpyright rejects on ORM attribute assignment.
_NOW = func.datetime("now")
DATABASE_NOW: Final = _NOW

# The closed set the ``apply_status`` column CHECK allows; used to narrow the
# column's ``str`` type back to :data:`ApplyStatus` when mapping rows to DTOs.
_APPLY_STATUSES: Final[tuple[ApplyStatus, ...]] = ("pending", "applied", "skipped", "error")


def _is_apply_status(value: str) -> TypeIs[ApplyStatus]:
    """Narrow a raw column value to :data:`ApplyStatus` (guarded by the DB CHECK)."""
    return value in _APPLY_STATUSES


class _CursorLike(Protocol):
    """Minimal structural type for a DBAPI cursor (sqlalchemy event boundary)."""

    def execute(self, statement: str) -> None: ...

    def close(self) -> None: ...


class _DbapiConnectionLike(Protocol):
    """Minimal structural type for a raw DBAPI connection (sqlalchemy event)."""

    def cursor(self) -> _CursorLike: ...


# --- Engine / session factory ------------------------------------------------
def _set_sqlite_foreign_keys(dbapi_connection: _DbapiConnectionLike, *args: object) -> None:
    """Enable ``PRAGMA foreign_keys`` on a freshly opened SQLite connection."""
    del args  # sqlalchemy passes (connection_record); not needed here
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _enable_foreign_keys(engine: AsyncEngine) -> None:
    """Registers the FK pragma listener.

    SQLite disables FK enforcement by default; without this a dangling
    ``pipeline_id`` would silently pass. The listener runs once per new
    connection, so every session on ``engine`` inherits the pragma. Async
    engines dispatch the ``connect`` event on their underlying sync engine.
    """
    event.listen(engine.sync_engine, "connect", _set_sqlite_foreign_keys)


def create_async_sqlite_engine(db_path: Path) -> AsyncEngine:
    """Build an async SQLite engine for a real file DB at ``db_path``."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _enable_foreign_keys(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Build an :class:`async_sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` keeps ORM attributes readable after a commit so
    repository methods that map to DTOs after committing do not trigger a reload.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Create the five tables from :mod:`models` metadata on ``engine``.

    Used by tests for the fast path (and by a future ``init``); the canonical
    schema path is the Alembic migrations in ``migrations/``.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- DTO <-> ORM mapping -----------------------------------------------------
def _pipeline_to_row(pipeline: Pipeline) -> PipelineRow:
    return PipelineRow(
        name=pipeline.name,
        description=pipeline.description,
        current_snapshot_id=pipeline.current_snapshot_id,
    )


def _pipeline_from_row(row: PipelineRow) -> Pipeline:
    return Pipeline(
        id=row.id,
        name=row.name,
        description=row.description,
        current_snapshot_id=row.current_snapshot_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        soft_deleted_at=row.soft_deleted_at,
    )


def _snapshot_to_row(snapshot: PipelineSnapshot) -> PipelineSnapshotRow:
    return PipelineSnapshotRow(
        pipeline_id=snapshot.pipeline_id,
        snapshot_no=snapshot.snapshot_no,
        source=snapshot.source,
        note=snapshot.note,
        service=snapshot.service,
        service_section=snapshot.service_section,
        openai_captcha=snapshot.openai_captcha,
        hh_test_solving=snapshot.hh_test_solving,
        login=snapshot.login,
        password=snapshot.password,
        resume=snapshot.resume,
        openai_model=snapshot.openai_model,
        openai_base_url=snapshot.openai_base_url,
        openai_api_key=snapshot.openai_api_key,
        openai_reasoning_effort=snapshot.openai_reasoning_effort,
        min_required_score=snapshot.min_required_score,
        scoring_prompt=snapshot.scoring_prompt,
        apply_prompt=snapshot.apply_prompt,
        daily_apply_limit=snapshot.daily_apply_limit,
    )


def _snapshot_from_row(row: PipelineSnapshotRow) -> PipelineSnapshot:
    return PipelineSnapshot(
        id=row.id,
        pipeline_id=row.pipeline_id,
        snapshot_no=row.snapshot_no,
        source=row.source,
        note=row.note,
        service=row.service,
        service_section=row.service_section,
        openai_captcha=row.openai_captcha,
        hh_test_solving=row.hh_test_solving,
        login=row.login,
        password=row.password,
        resume=row.resume,
        openai_model=row.openai_model,
        openai_base_url=row.openai_base_url,
        openai_api_key=row.openai_api_key,
        openai_reasoning_effort=row.openai_reasoning_effort,
        min_required_score=row.min_required_score,
        scoring_prompt=row.scoring_prompt,
        apply_prompt=row.apply_prompt,
        daily_apply_limit=row.daily_apply_limit,
        created_at=row.created_at,
    )


def _vacancy_to_row(vacancy: VacancyRecord) -> VacancyRow:
    return VacancyRow(
        pipeline_id=vacancy.pipeline_id,
        fetched_snapshot_id=vacancy.fetched_snapshot_id,
        scored_snapshot_id=vacancy.scored_snapshot_id,
        generated_snapshot_id=vacancy.generated_snapshot_id,
        applied_snapshot_id=vacancy.applied_snapshot_id,
        external_id=vacancy.external_id,
        title=vacancy.title,
        url=vacancy.url,
        company=vacancy.company,
        description=vacancy.description,
        salary=vacancy.salary,
        score=vacancy.score,
        score_reasoning=vacancy.score_reasoning,
        score_error=vacancy.score_error,
        cover_letter=vacancy.cover_letter,
        cover_letter_error=vacancy.cover_letter_error,
        apply_status=vacancy.apply_status,
        apply_error=vacancy.apply_error,
        skip_reason=vacancy.skip_reason,
        applied_at=vacancy.applied_at,
        fetched_at=vacancy.fetched_at,
        scored_at=vacancy.scored_at,
        generated_at=vacancy.generated_at,
        user_edited_at=vacancy.user_edited_at,
        manual_skip=int(vacancy.manual_skip),
        manual_skip_reason=vacancy.manual_skip_reason,
        notes=vacancy.notes,
        has_hh_test=int(vacancy.has_hh_test) if vacancy.has_hh_test is not None else None,
    )


def _vacancy_from_row(row: VacancyRow) -> VacancyRecord:
    apply_status_value: str | None = row.apply_status
    apply_status: ApplyStatus | None = None
    if apply_status_value is not None:
        if not _is_apply_status(apply_status_value):
            raise ValueError(f"unsupported apply_status in DB: {apply_status_value!r}")
        apply_status = apply_status_value

    return VacancyRecord(
        id=row.id,
        pipeline_id=row.pipeline_id,
        fetched_snapshot_id=row.fetched_snapshot_id,
        scored_snapshot_id=row.scored_snapshot_id,
        generated_snapshot_id=row.generated_snapshot_id,
        applied_snapshot_id=row.applied_snapshot_id,
        external_id=ServiceVacancyId(row.external_id),
        title=row.title,
        url=row.url,
        company=row.company,
        description=row.description,
        salary=row.salary,
        score=row.score,
        score_reasoning=row.score_reasoning,
        score_error=row.score_error,
        cover_letter=row.cover_letter,
        cover_letter_error=row.cover_letter_error,
        apply_status=apply_status,
        apply_error=row.apply_error,
        skip_reason=row.skip_reason,
        applied_at=row.applied_at,
        fetched_at=row.fetched_at,
        scored_at=row.scored_at,
        generated_at=row.generated_at,
        user_edited_at=row.user_edited_at,
        manual_skip=bool(row.manual_skip),
        manual_skip_reason=row.manual_skip_reason,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        soft_deleted_at=row.soft_deleted_at,
        has_hh_test=bool(row.has_hh_test) if row.has_hh_test is not None else None,
    )


def vacancy_to_row(vacancy: VacancyRecord) -> VacancyRow:
    """Map a frozen vacancy DTO to a transient ORM row for storage clients."""
    return _vacancy_to_row(vacancy)


def vacancy_from_row(row: VacancyRow) -> VacancyRecord:
    """Map an ORM vacancy row to the frozen DTO exposed outside repositories."""
    return _vacancy_from_row(row)


def _audit_from_row(row: AuditLog) -> AuditLogEntry:
    return AuditLogEntry(
        id=row.id,
        pipeline_id=row.pipeline_id,
        pipeline_snapshot_id=row.pipeline_snapshot_id,
        action=row.action,
        details=row.details,
        created_at=row.created_at,
    )


def _daily_from_row(row: DailyLimitRow) -> DailyLimit:
    return DailyLimit(
        id=row.id,
        service=row.service,
        login=row.login,
        date=row.date,
        count=row.count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- Repositories ------------------------------------------------------------
class _Repository:
    """Base: yields a committed/rolled-back session per public method."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[AsyncSession]:
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class PipelineRepository(_Repository):
    """Identity CRUD over ``pipelines``, mapping rows to :class:`Pipeline` DTOs."""

    async def create(self, pipeline: Pipeline) -> Pipeline:
        """Insert a new identity row and return it with its assigned id."""
        async with self._session() as session:
            row = _pipeline_to_row(pipeline)
            session.add(row)
            await session.flush()
            return replace(_pipeline_from_row(row), id=row.id)

    async def get(self, pipeline_id: int) -> Pipeline | None:
        """Return the identity by id, or ``None`` if absent."""
        async with self._session() as session:
            row = await session.get(PipelineRow, pipeline_id)
            return _pipeline_from_row(row) if row is not None else None

    async def find_by_name(self, name: str) -> Pipeline | None:
        """Return the active (non-soft-deleted) identity with ``name``, or ``None``.

        ``name`` is unique among active rows (the migration-only partial index
        ``uq_pipelines_name_active``), so at most one row matches.
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(PipelineRow).where(
                        PipelineRow.name == name,
                        PipelineRow.soft_deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            return _pipeline_from_row(row) if row is not None else None

    async def list(self) -> list[Pipeline]:
        """Return all non-soft-deleted identities, oldest first."""
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineRow).where(PipelineRow.soft_deleted_at.is_(None)).order_by(PipelineRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_pipeline_from_row(row) for row in rows]

    async def soft_delete(self, pipeline_id: int) -> bool:
        """Soft-delete an identity; return ``False`` if it did not exist."""
        async with self._session() as session:
            row = await session.get(PipelineRow, pipeline_id)
            if row is None:
                return False
            row.soft_deleted_at = _NOW
            return True

    async def set_current_snapshot(self, pipeline_id: int, snapshot_id: int) -> bool:
        """Repoint the identity's ``current_snapshot_id`` (its head) to ``snapshot_id``."""
        async with self._session() as session:
            row = await session.get(PipelineRow, pipeline_id)
            if row is None:
                return False
            row.current_snapshot_id = snapshot_id
            row.updated_at = _NOW
            return True


class PipelineSnapshotRepository(_Repository):
    """Append-only snapshot accessors over ``pipeline_snapshot``."""

    async def create(self, snapshot: PipelineSnapshot) -> PipelineSnapshot:
        """Insert a snapshot row and return it with its assigned id."""
        async with self._session() as session:
            row = _snapshot_to_row(snapshot)
            session.add(row)
            await session.flush()
            return replace(_snapshot_from_row(row), id=row.id)

    async def create_identity_with_snapshot(
        self, pipeline: Pipeline, snapshot: PipelineSnapshot
    ) -> Result[Pipeline, str]:
        """Insert an identity + its first snapshot and set the head — atomically.

        One session/transaction: insert the identity, flush to get its real id
        (pinned onto the snapshot, whose ``snapshot_no`` is forced to 1), insert
        the snapshot, then point ``current_snapshot_id`` at it. All-or-nothing:
        a UNIQUE violation (e.g. the migration-only same-name active index under
        a concurrent ``init``) surfaces as ``Err`` — never a raw
        :class:`IntegrityError` — and nothing is partially persisted.
        """
        try:
            async with self._session() as session:
                row = _pipeline_to_row(pipeline)
                session.add(row)
                await session.flush()
                snapshot_row = _snapshot_to_row(replace(snapshot, pipeline_id=row.id, snapshot_no=1))
                session.add(snapshot_row)
                await session.flush()
                row.current_snapshot_id = snapshot_row.id
                row.updated_at = _NOW
                await session.flush()
                # ``updated_at = _NOW`` is a SQL expression: after flush the
                # column is expired; refresh so the mapping below never lazy-
                # loads outside the greenlet context (MissingGreenlet in async).
                await session.refresh(row)
                stored = replace(_pipeline_from_row(row), id=row.id)
        except IntegrityError as exc:
            return Err(f"failed to create pipeline: {exc}")
        return Ok(stored)

    async def create_and_set_current(
        self, pipeline_id: int, snapshot: PipelineSnapshot
    ) -> Result[PipelineSnapshot, str]:
        """Append a snapshot with the next ``snapshot_no`` and repoint the head — atomically.

        One session/transaction: compute ``next_no = MAX(snapshot_no) + 1`` *in
        the same transaction* as the insert and the ``current_snapshot_id``
        repoint, so a concurrent append can neither produce a duplicate
        ``snapshot_no`` (the ``UNIQUE(pipeline_id, snapshot_no)`` violation
        surfaces as ``Err``, not a raw :class:`IntegrityError`) nor leave a
        non-current head behind. An unknown pipeline returns ``Err`` before
        anything is written.
        """
        try:
            async with self._session() as session:
                pipeline = await session.get(PipelineRow, pipeline_id)
                if pipeline is None:
                    return Err(f"Pipeline id {pipeline_id} not found.")
                max_no = await session.scalar(
                    select(func.max(PipelineSnapshotRow.snapshot_no)).where(
                        PipelineSnapshotRow.pipeline_id == pipeline_id
                    )
                )
                next_no = (int(max_no) if max_no is not None else 0) + 1
                row = _snapshot_to_row(replace(snapshot, pipeline_id=pipeline_id, snapshot_no=next_no))
                session.add(row)
                await session.flush()
                pipeline.current_snapshot_id = row.id
                pipeline.updated_at = _NOW
                await session.flush()
                stored = _snapshot_from_row(row)
        except IntegrityError as exc:
            return Err(f"failed to append snapshot for pipeline {pipeline_id}: {exc}")
        return Ok(stored)

    async def current(self, pipeline_id: int) -> PipelineSnapshot | None:
        """Return the identity's current head snapshot (via ``current_snapshot_id``).

        Falls back to ``None`` when the identity is absent or has no head set.
        """
        async with self._session() as session:
            pipeline = await session.get(PipelineRow, pipeline_id)
            if pipeline is None or pipeline.current_snapshot_id is None:
                return None
            row = await session.get(PipelineSnapshotRow, pipeline.current_snapshot_id)
            return _snapshot_from_row(row) if row is not None else None

    async def list(self, pipeline_id: int) -> list[PipelineSnapshot]:
        """Return every snapshot for a pipeline, ordered by ``snapshot_no``."""
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineSnapshotRow)
                        .where(PipelineSnapshotRow.pipeline_id == pipeline_id)
                        .order_by(PipelineSnapshotRow.snapshot_no)
                    )
                )
                .scalars()
                .all()
            )
            return [_snapshot_from_row(row) for row in rows]

    async def next_snapshot_no(self, pipeline_id: int) -> int:
        """Return the next ``snapshot_no`` to append for ``pipeline_id`` (1 if none)."""
        async with self._session() as session:
            max_no = await session.scalar(
                select(func.max(PipelineSnapshotRow.snapshot_no)).where(PipelineSnapshotRow.pipeline_id == pipeline_id)
            )
            return (int(max_no) if max_no is not None else 0) + 1


class VacancyRepository(_Repository):
    """CRUD + batch queries over ``vacancies``, mapping to :class:`VacancyRecord`."""

    async def upsert(self, vacancy: VacancyRecord) -> VacancyRecord:
        """Insert or update the vacancy with matching ``(pipeline_id, external_id)``.

        Keeps the ORM ``UNIQUE(pipeline_id, external_id)`` invariant; an
        existing row is overwritten with the record's field values — a dumb
        full-field copy (see :func:`_apply_vacancy_fields`). The caller builds
        the right record: the fetch stage decides insert-only vs ``--refresh``
        overwrite and threads the pre-existing derived fields + timestamps it
        must preserve. ``soft_deleted_at`` is a manual lifecycle signal and is
        **never** touched here.
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(VacancyRow).where(
                        VacancyRow.pipeline_id == vacancy.pipeline_id,
                        VacancyRow.external_id == vacancy.external_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                new_row = _vacancy_to_row(vacancy)
                session.add(new_row)
                await session.flush()
                return replace(_vacancy_from_row(new_row), id=new_row.id)
            updated = _vacancy_to_row(vacancy)
            _apply_vacancy_fields(row, updated)
            row.updated_at = _NOW
            await session.flush()
            # ``updated_at = _NOW`` is a SQL expression: after flush the column
            # is expired and reading it would trigger a lazy load outside the
            # greenlet context (MissingGreenlet in async). Refresh the row so
            # the server-computed timestamp is fetched eagerly.
            await session.refresh(row)
            return _vacancy_from_row(row)

    async def get(self, vacancy_id: int) -> VacancyRecord | None:
        """Return a vacancy row by its DB id, or ``None`` if absent.

        Used by the GUI's ``VacancyManager`` edit path: it loads the current
        frozen DTO, applies an editable-field mutation, persists via
        :meth:`update` and appends an ``audit_log`` entry (Phase 7, task 7.2).
        """
        async with self._session() as session:
            row = await session.get(VacancyRow, vacancy_id)
            return _vacancy_from_row(row) if row is not None else None

    async def get_by_external_id(self, pipeline_id: int, external_id: ServiceVacancyId) -> VacancyRecord | None:
        """Return the vacancy for an external id in **any** state, or ``None``.

        State-agnostic on purpose: the fetch stage looks a vacancy up to decide
        insert-only skip vs ``--refresh`` overwrite, and the row may be
        soft-deleted at that point (fetch never touches soft-deleted rows, so
        the stage reads the state off this lookup).
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(VacancyRow).where(
                        VacancyRow.pipeline_id == pipeline_id,
                        VacancyRow.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            return _vacancy_from_row(row) if row is not None else None

    async def list_by_pipeline(self, pipeline_id: int) -> list[VacancyRecord]:
        """Return all non-soft-deleted vacancies for a pipeline, by ``id``.

        Id order is the stable listing order everywhere: ``--from/--to/--take``
        on the position stages are offsets into this list, computed at run time.
        """
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(VacancyRow)
                        .where(
                            VacancyRow.pipeline_id == pipeline_id,
                            VacancyRow.soft_deleted_at.is_(None),
                        )
                        .order_by(VacancyRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_vacancy_from_row(row) for row in rows]

    async def list_states_by_pipeline(self, pipeline_id: int) -> list[VacancyRecord]:
        """Return every vacancy row for a pipeline in **any** state, by ``id``.

        Unlike :meth:`list_by_pipeline` this includes soft-deleted rows: the
        ``search`` preview join needs the real lifecycle state (a soft-deleted
        vacancy must not read as "new" — fetch silently skips it). Position
        stages must keep using :meth:`list_by_pipeline`: its non-deleted list
        is the load-bearing ``--from/--to/--take`` offset space, and including
        deleted rows here would shift those offsets.
        """
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(VacancyRow).where(VacancyRow.pipeline_id == pipeline_id).order_by(VacancyRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_vacancy_from_row(row) for row in rows]

    async def update(self, vacancy: VacancyRecord) -> VacancyRecord:
        """Persist field changes to an existing vacancy (matched by ``vacancy.id``)."""
        async with self._session() as session:
            row = await session.get(VacancyRow, vacancy.id)
            if row is None:
                raise ValueError(f"vacancy {vacancy.id} does not exist")
            updated = _vacancy_to_row(vacancy)
            _apply_vacancy_fields(row, updated)
            row.updated_at = _NOW
            await session.flush()
            # ``updated_at = _NOW`` is a SQL expression: after flush the column
            # is expired and reading it would trigger a lazy load outside the
            # greenlet context (MissingGreenlet in async). Refresh the row so
            # the server-computed timestamp is fetched eagerly.
            await session.refresh(row)
            return _vacancy_from_row(row)

    async def list_external_ids(self, pipeline_id: int) -> set[ServiceVacancyId]:
        """Return every stored external id for a pipeline, in any state.

        State-agnostic on purpose: the fetch stage excludes already-stored ids
        from the default insert-only client call, and soft-deleted rows are
        never fetched under any flag, so excluding them is free and correct.
        """
        async with self._session() as session:
            rows = (
                (await session.execute(select(VacancyRow.external_id).where(VacancyRow.pipeline_id == pipeline_id)))
                .scalars()
                .all()
            )
            return {ServiceVacancyId(str(value)) for value in rows}

    async def external_ids_scored(self, pipeline_id: int) -> set[ServiceVacancyId]:
        """Return external ids that already carry a score (for skip-already-processed on ``score``)."""
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(VacancyRow.external_id).where(
                            VacancyRow.pipeline_id == pipeline_id,
                            VacancyRow.soft_deleted_at.is_(None),
                            VacancyRow.score.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {ServiceVacancyId(str(value)) for value in rows}

    async def external_ids_with_cover_letter(self, pipeline_id: int) -> set[ServiceVacancyId]:
        """Return external ids that already hold a cover letter (skip-already-processed on ``generate``)."""
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(VacancyRow.external_id).where(
                            VacancyRow.pipeline_id == pipeline_id,
                            VacancyRow.soft_deleted_at.is_(None),
                            VacancyRow.cover_letter.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {ServiceVacancyId(str(value)) for value in rows}

    async def external_ids_decided(self, pipeline_id: int) -> set[ServiceVacancyId]:
        """Return external ids that reached a terminal apply decision (skip-already-processed on ``apply``)."""
        terminal = ("applied", "skipped", "error")
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(VacancyRow.external_id).where(
                            VacancyRow.pipeline_id == pipeline_id,
                            VacancyRow.soft_deleted_at.is_(None),
                            VacancyRow.apply_status.in_(terminal),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {ServiceVacancyId(str(value)) for value in rows}


def _apply_vacancy_fields(row: VacancyRow, updated: VacancyRow) -> None:
    """Copy every mutable field from a transient ``updated`` row onto ``row``.

    Identity (``id``/``pipeline_id``/``external_id``) and the
    ``created_at``/``updated_at``/``soft_deleted_at`` lifecycle stamps are left
    intact so an upsert never changes which row a vacancy maps to. All four
    snapshot-provenance columns and the four staleness timestamps
    (``fetched_at``/``scored_at``/``generated_at``/``user_edited_at``) are
    **unconditionally** copied from ``updated`` — there is no per-field preserve
    logic here. Preservation of prior artifacts is therefore the **caller's**
    contract: callers must thread the previous values through the DTO they
    persist (e.g. the fetch stage re-applies the existing derived fields and
    timestamps on a ``--refresh``; each stage bumps its own ``*_at`` stamp
    before writing).
    """
    for field_name in (
        "fetched_snapshot_id",
        "scored_snapshot_id",
        "generated_snapshot_id",
        "applied_snapshot_id",
        "title",
        "url",
        "company",
        "description",
        "salary",
        "score",
        "score_reasoning",
        "score_error",
        "cover_letter",
        "cover_letter_error",
        "apply_status",
        "apply_error",
        "skip_reason",
        "applied_at",
        "fetched_at",
        "scored_at",
        "generated_at",
        "user_edited_at",
        "manual_skip",
        "manual_skip_reason",
        "notes",
        "has_hh_test",
    ):
        setattr(row, field_name, getattr(updated, field_name))


def apply_vacancy_fields(row: VacancyRow, updated: VacancyRow) -> None:
    """Copy mutable vacancy fields for storage clients sharing this mapping."""
    _apply_vacancy_fields(row, updated)


class AuditLogRepository(_Repository):
    """Append-only history over ``audit_log``."""

    async def append(
        self,
        pipeline_id: int,
        action: str,
        details: str | None = None,
        *,
        pipeline_snapshot_id: int | None = None,
    ) -> AuditLogEntry:
        """Record one audit entry and return it."""
        async with self._session() as session:
            row = AuditLog(
                pipeline_id=pipeline_id,
                pipeline_snapshot_id=pipeline_snapshot_id,
                action=action,
                details=details,
            )
            session.add(row)
            await session.flush()
            return _audit_from_row(row)

    async def list(self) -> list[AuditLogEntry]:
        """Return every audit entry, newest first.

        Read-only history for the GUI's **Audit Log** view (Phase 7, task 7.2);
        the QML view refreshes through this when the manager re-fetches.
        """
        async with self._session() as session:
            rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()))).scalars().all()
            return [_audit_from_row(row) for row in rows]


class DailyLimitRepository(_Repository):
    """Per-auth, per-day counters over ``daily_limits`` (keyed by service+login+date)."""

    async def list(self) -> list[DailyLimit]:
        """Return every daily-limit counter row, newest first.

        Read-only feed for the GUI's daily-limits surface (Phase 7, task 7.2).
        """
        async with self._session() as session:
            rows = (await session.execute(select(DailyLimitRow).order_by(DailyLimitRow.id.desc()))).scalars().all()
            return [_daily_from_row(row) for row in rows]

    async def get(self, service: str, login: str, date: str) -> DailyLimit | None:
        """Read the counter row for ``(service, login, date)``, or ``None``."""
        async with self._session() as session:
            row = (
                await session.execute(
                    select(DailyLimitRow).where(
                        DailyLimitRow.service == service,
                        DailyLimitRow.login == login,
                        DailyLimitRow.date == date,
                    )
                )
            ).scalar_one_or_none()
            return _daily_from_row(row) if row is not None else None

    async def increment(self, service: str, login: str, date: str) -> DailyLimit:
        """Atomically increment the per-auth daily counter for ``(service, login, date)``.

        The fresh-key path is **not** check-then-insert: a single
        ``INSERT ... ON CONFLICT(service, login, date) DO NOTHING`` is atomic at
        the DB level, so concurrent first increments of the same key can't race
        into a UNIQUE violation — the loser no-ops and both then run the same
        atomic ``UPDATE ... SET count = count + 1`` below (one row, both ticks
        counted). The ORM ``UNIQUE(service, login, date)`` invariant is
        preserved: one counter per auth, shared across every pipeline that uses
        the account.
        """
        async with self._session() as session:
            await session.execute(
                sqlite_insert(DailyLimitRow)
                .values(service=service, login=login, date=date, count=0, created_at=_NOW, updated_at=_NOW)
                .on_conflict_do_nothing(index_elements=["service", "login", "date"])
            )
            # Atomic at the DB level: `count = count + 1` is a single UPDATE,
            # so concurrent increments never lose a tick.
            await session.execute(
                update(DailyLimitRow)
                .where(
                    DailyLimitRow.service == service,
                    DailyLimitRow.login == login,
                    DailyLimitRow.date == date,
                )
                .values(count=DailyLimitRow.count + 1, updated_at=_NOW)
            )
            await session.flush()
            # The Core INSERT/UPDATE bypass the identity map; reload the row so
            # the returned DTO reflects the incremented counter + real stamps.
            row = (
                await session.execute(
                    select(DailyLimitRow).where(
                        DailyLimitRow.service == service,
                        DailyLimitRow.login == login,
                        DailyLimitRow.date == date,
                    )
                )
            ).scalar_one()
            return _daily_from_row(row)


# --- Storage facade ----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Storage:
    """Composition of engine, session factory and the repositories."""

    engine: AsyncEngine
    session_factory: SessionFactory
    pipelines: PipelineRepository
    snapshots: PipelineSnapshotRepository
    vacancies: VacancyRepository
    audit_log: AuditLogRepository
    daily_limits: DailyLimitRepository


def _storage_from_engine(engine: AsyncEngine) -> Storage:
    session_factory = create_session_factory(engine)
    return Storage(
        engine=engine,
        session_factory=session_factory,
        pipelines=PipelineRepository(session_factory),
        snapshots=PipelineSnapshotRepository(session_factory),
        vacancies=VacancyRepository(session_factory),
        audit_log=AuditLogRepository(session_factory),
        daily_limits=DailyLimitRepository(session_factory),
    )


def storage_from_engine(engine: AsyncEngine) -> Storage:
    """Build a :class:`Storage` facade over an existing engine (tests use in-memory)."""
    return _storage_from_engine(engine)


def open_storage(db_path: Path) -> Storage:
    """Open a file-backed :class:`Storage` at ``db_path``.

    The composition root passes the resolved ``jobfucker.db`` path; tests use
    ``storage_from_engine(in_memory_db)``.
    """
    engine = create_async_sqlite_engine(db_path)
    return _storage_from_engine(engine)


def apply_migrations(db_path: Path) -> None:
    """Apply any pending Alembic migrations to the file DB at ``db_path``.

    Runs ``alembic upgrade head`` programmatically against the migrations bundled
    in ``storage/migrations/``. Callers (the future composition root / ``init``)
    invoke this once on startup so a fresh DB is always migrated; it is a no-op
    when the DB is already at head, so re-running is safe.

    Alembic has no async engine story, so this stays synchronous. It must never
    run on a live event-loop thread: call it **before** the loop starts, or from
    within via ``await asyncio.to_thread(apply_migrations, db_path)`` (see the
    async-migration spec §2.2.5).
    """
    from alembic import command  # lazy import: only needed when migrating
    from alembic.config import Config

    ini_path = Path(__file__).parent / "migrations" / "alembic.ini"
    config = Config(str(ini_path))
    # Alembic's configparser interpolates %(...)s on read (env.py get_main_option /
    # get_section), so a literal % in the DB path (e.g. DATA_DIR "50%off") would
    # raise InterpolationSyntaxError; %% unescapes back to % when expanded.
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}".replace("%", "%%"))
    command.upgrade(config, "head")
