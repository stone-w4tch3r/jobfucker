"""UI-neutral composition root for the ``app/`` layer.

:func:`AppServices.open` builds the shared service graph (storage + factory +
pipeline service) once, independent of any presentation client. It is
**UI-neutral**: it never selects a terminal captcha handler and never constructs
a terminal auth interaction — the injected :class:`AuthInteractionProvider`
defaults to the no-op :class:`NoopAuthInteraction` (never invoked in this graph;
the factory is used only for ``validate_cap``/status, not to construct a client).

Mirrors ``bootstrap._open_runtime_storage`` and ``bootstrap._build_factory``'s
config-less path, swapping the CLI :class:`TerminalAuthInteraction` for the
injected ``interaction`` and the selected captcha handler for a no-op fallback.
Both the CLI and the Qt GUI consume this graph after Slice B rewires them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from rusty_results.prelude import Err, Ok, Result

from jobfucker.app.pipeline_service import PipelineService
from jobfucker.app.vacancy_documents import VacancyDocumentService
from jobfucker.clients.base import AuthInteractionProvider, CaptchaHandler, ClientCredentials, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.mock.client import MockClient
from jobfucker.runtime import data_dir, database_path, ensure_runtime_dirs
from jobfucker.storage.db import Storage, apply_migrations, open_storage
from jobfucker.storage.vacancy_documents import SqlAlchemyVacancyDocumentStore

__all__ = ["AppServices", "NoopAuthInteraction"]


class NoopAuthInteraction:
    """A no-op :class:`AuthInteractionProvider` for UI-neutral graphs.

    This graph never constructs a client, so the interaction is never invoked;
    if it somehow is, it returns ``Err`` rather than raising or hanging.
    """

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        """Return ``Err`` — no human interaction is available in this graph."""
        del prompt
        return Err("interaction not available")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        """Return ``Err`` — no human interaction is available in this graph."""
        del prompt
        return Err("interaction not available")


# The stateless default interaction. A shared instance is safe (B008 avoids a
# call in a mutable default) and never invoked in this graph.
_NOOP_INTERACTION: Final[NoopAuthInteraction] = NoopAuthInteraction()


def _unavailable_captcha_handler() -> CaptchaHandler:
    """A never-invoked, ALWAYS-unavailable handler for the UI-neutral graph.

    :class:`ClientDeps` requires a captcha handler, but this graph never
    constructs a client (the factory is used only for ``validate_cap``/status).
    Captcha solving is deliberately not available here on any UI surface, so a
    fixed unavailable handler suffices.
    """

    async def _unavailable(image: bytes) -> Result[str, str]:
        del image
        return Err("No captcha handler configured for this command")

    return _unavailable


@dataclass(frozen=True, slots=True)
class AppServices:
    """The composed service graph a presentation client consumes.

    Attributes:
        storage: the migrated file-backed :class:`Storage` (or injected override).
        factory: the client registry bound to the UI-neutral deps (used for
            ``validate_cap``/status; never constructs a client).
        pipelines: the :class:`PipelineService` owning pipeline lifecycle.
        _owns_storage: whether this graph opened ``storage`` itself (``open()``
            without ``storage_override``) and therefore must dispose its engine
            on :meth:`close`. A graph built on an injected override does not own
            the engine — the caller does — and must never dispose it here.
    """

    storage: Storage
    factory: Factory
    pipelines: PipelineService
    vacancies: VacancyDocumentService
    _owns_storage: bool = False

    async def close(self) -> None:
        """Dispose the storage engine, but only when this graph owns it.

        A graph that opened the file DB itself (``open()`` without
        ``storage_override``) owns its engine and must dispose it before the
        loop/process closes (the ``storage.db`` invariant). A graph built on an
        injected ``storage_override`` never disposes the caller's engine: tests
        share one in-memory StaticPool engine across commands, and disposing it
        mid-test would destroy the shared DB.

        Idempotent: the ownership flag is cleared on the first call, so a second
        ``close()`` is a no-op — safe to call twice and safe to call after a
        command failed via ``typer.Exit`` (the CLI decorator calls this from a
        ``finally``).
        """
        if not self._owns_storage:
            return
        # The dataclass is frozen; flip the flag via ``object.__setattr__`` so a
        # later close is a no-op (and the engine is disposed exactly once).
        object.__setattr__(self, "_owns_storage", False)
        await self.storage.engine.dispose()

    @classmethod
    async def open(
        cls,
        *,
        storage_override: Storage | None = None,
        interaction: AuthInteractionProvider = _NOOP_INTERACTION,
        vacancies_pipeline_id: int | None = None,
    ) -> Result[AppServices, str]:
        """Open the UI-neutral service graph.

        Args:
            storage_override: an injected :class:`Storage` (tests use an
                in-memory one). When given it is returned as-is with NO
                migrations; otherwise runtime dirs are ensured, migrations are
                applied, and the file-backed DB is opened.
            interaction: the injected :class:`AuthInteractionProvider` (defaults
                to the no-op :class:`NoopAuthInteraction`). Never terminal-dependent.
            vacancies_pipeline_id: optionally scope the vacancy-document service
                to one pipeline (``None`` = all pipelines). A missing or
                soft-deleted pipeline fails the whole graph with the standard
                "not found" message, so every vacancies command surfaces it the
                same way.

        Returns:
            ``Ok(AppServices)`` or ``Err`` when ``vacancies_pipeline_id`` names
            a missing or soft-deleted pipeline. The graph never fails on
            captcha/terminal selection. The ``Result`` is a deliberate
            forward-looking seam: a future storage/migration failure can
            surface as ``Err``, and it keeps a uniform ``if r.is_err``
            call-site across the CLI and GUI clients.

        Migrations stay synchronous (Alembic has no async story) but never
        block a live event-loop thread: they run via ``asyncio.to_thread``, per
        the async-migration spec D2.
        """
        # Alembic migration logs are noise on both the CLI and GUI surfaces;
        # suppress them so output stays clean. Migrations still run; only the
        # noise is silenced.
        logging.getLogger("alembic").setLevel(logging.WARNING)
        if storage_override is not None:
            storage = storage_override
        else:
            ensure_runtime_dirs()
            db_path = database_path()
            await asyncio.to_thread(apply_migrations, db_path)
            storage = open_storage(db_path)

        deps = ClientDeps(
            service="mock",
            profile_id=None,
            data_dir=data_dir(),
            credentials=ClientCredentials(login="", password=""),
            auth_interaction=interaction,
            captcha_handler=_unavailable_captcha_handler(),
        )
        if vacancies_pipeline_id is not None:
            identity = await storage.pipelines.get(vacancies_pipeline_id)
            if identity is None or identity.soft_deleted_at is not None:
                if storage_override is None:
                    await storage.engine.dispose()
                return Err(f"Pipeline id {vacancies_pipeline_id} not found.")
        factory = Factory(deps)
        factory.register("hh", HHClient)
        factory.register("mock", MockClient)
        pipelines = PipelineService(storage, factory)
        vacancy_documents = VacancyDocumentService.create(
            SqlAlchemyVacancyDocumentStore(storage.session_factory), vacancies_pipeline_id
        )
        if vacancy_documents.is_err:
            if storage_override is None:
                await storage.engine.dispose()
            return Err(vacancy_documents.unwrap_err())
        return Ok(
            AppServices(
                storage=storage,
                factory=factory,
                pipelines=pipelines,
                vacancies=vacancy_documents.unwrap(),
                _owns_storage=storage_override is None,
            )
        )
