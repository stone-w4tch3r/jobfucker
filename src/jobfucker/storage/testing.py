"""Shared BDD/test harness: per-call file-DB ``AsyncEngine`` for async storage steps.

The async-migration spec (D2 / §6) freezes the storage test discipline:

- **file DB + ``NullPool``**, a fresh engine per call, always disposed before the
  call returns (``AsyncEngine.dispose()`` is a coroutine — it must be awaited).
  A file DB persists across ``asyncio.run`` one-shots, so an earlier BDD step's
  writes are visible to a later step's fresh engine on the same path.
- **in-memory ``NullPool`` is broken** (a fresh empty DB per connection →
  ``no such table``); in-memory works only via ``StaticPool`` (single shared
  connection), so persistence tests use a file DB. Reserve in-memory for
  isolated read-only fakes.
- This module lives in ``storage/**`` because SQLAlchemy imports are
  ``TID251``-banned outside ``storage/`` — steps consume it, never SQLAlchemy.

Usage (a Gherkin step; ``scenario_db`` is the per-scenario file path fixture):

```python
def step(scenario_db: Path, ...) -> ...:
    return with_db(scenario_db, lambda s: s.vacancies.list_by_pipeline(...))
```
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .db import Storage, create_schema, storage_from_engine


def with_db[T](path: Path, fn: Callable[[Storage], Awaitable[T]]) -> T:
    """Run ``fn`` against a fresh file-DB :class:`Storage`, disposing the engine.

    A new engine is created per call on ``path`` with ``NullPool`` and the five
    tables created (idempotent — ``create_all`` is a no-op when they exist), so
    the schema needs no per-call setup beyond this helper and rows written by
    an earlier step on the same ``path`` are visible. The engine is always
    disposed before this returns (both on success and on failure).

    Args:
        path: the per-scenario file DB path (stable across steps of a scenario).
        fn: an async callable receiving the :class:`Storage` facade to drive.

    Returns:
        Whatever ``fn`` returns.
    """

    async def _run() -> T:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
        try:
            await create_schema(engine)
            return await fn(storage_from_engine(engine))
        finally:
            # AsyncEngine.dispose() is a coroutine — it must be awaited, never
            # called synchronously (warns "coroutine was never awaited").
            await engine.dispose()

    return asyncio.run(_run())
