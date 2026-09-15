"""Phase 1 (task 1.3): DB-backed integration smoke for the ``integration`` family.

Proves the ``integration`` marker collects and that a real in-memory async
SQLAlchemy engine/session round-trips a row (persistence path) with per-test
isolation — the storage integration pattern every later phase reuses. A
throwaway pipeline row stands in for the real storage models that land in
Phase 3. Async per the async-migration (``in_memory_db`` is an async engine).
"""

from __future__ import annotations

import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    """Throwaway declarative base for the integration smoke."""


class _Pipeline(_Base):
    """A minimal pipeline row proving DB-backed integration works."""

    __tablename__ = "pipelines"

    name: Mapped[str] = mapped_column(String, primary_key=True)


@pytest.mark.integration
async def test_integration_smoke_persists_and_reads_back(in_memory_db: AsyncEngine) -> None:
    """A row written through a session is readable back from the same engine."""
    async with in_memory_db.begin() as conn:
        await conn.run_sync(_Pipeline.metadata.create_all)
    async with AsyncSession(in_memory_db) as session:
        session.add(_Pipeline(name="demo"))
        await session.commit()

        rows = (await session.execute(select(_Pipeline))).scalars().all()
        assert [row.name for row in rows] == ["demo"]
