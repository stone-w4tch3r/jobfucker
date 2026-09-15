"""Phase 1 (task 1.2): smoke tests proving every shared fixture works.

Each of the canonical fixtures in ``test/conftest.py`` is exercised here so a
bootstrap regression is caught the moment it is introduced, and cross-test DB
isolation is asserted explicitly (building on the Phase 0.4 probe). These tests
are deliberately trivial — they prove the *harness*, not app behaviour.

The throwaway ``Item`` model exists only to exercise the fixture machinery; the
real storage models live in Phase 3 (``src/jobfucker/storage/``).

Async notes (async-migration): ``in_memory_db``/``storage`` are async-engine
fixtures, so the fixture-smoke tests are ``async def`` (pytest-asyncio
``asyncio_mode="auto"``) and drive the schema/session through ``run_sync`` /
the async session factory.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobfucker.storage.db import Storage
from test.conftest import LogCapture, MakeConfigFileFn


class _Base(DeclarativeBase):
    """Throwaway declarative base for the fixture smoke tests."""


class _Item(_Base):
    """A single-column row used to prove writes do not leak across tests."""

    __tablename__ = "items"

    name: Mapped[str] = mapped_column(String, primary_key=True)


# --- in_memory_db ------------------------------------------------------------
async def test_in_memory_db_creates_schema_and_reads_back(in_memory_db: AsyncEngine) -> None:
    """``in_memory_db`` supports schema creation and querying within a test."""
    async with in_memory_db.begin() as conn:
        await conn.run_sync(_Item.metadata.create_all)
    async with AsyncSession(in_memory_db) as session:
        session.add(_Item(name="alpha"))
        await session.commit()

        rows = (await session.execute(select(_Item))).scalars().all()
        assert [row.name for row in rows] == ["alpha"]


async def test_in_memory_db_is_fresh_per_test(in_memory_db: AsyncEngine) -> None:
    """Cross-test isolation: rows from a prior test never appear here."""
    async with in_memory_db.begin() as conn:
        await conn.run_sync(_Item.metadata.create_all)
    async with AsyncSession(in_memory_db) as session:
        rows = (await session.execute(select(_Item))).scalars().all()
        assert list(rows) == []


# --- storage (async repo facade) ---------------------------------------------
async def test_storage_facade_round_trips(storage: Storage) -> None:
    """``storage`` builds the four tables and the repos drive them (async)."""
    from test.storage.builders import make_pipeline

    created = await storage.pipelines.create(make_pipeline(name="harness"))
    assert created.id > 0
    rows = await storage.pipelines.list()
    assert [p.name for p in rows] == ["harness"]


async def test_storage_is_fresh_per_test(storage: Storage) -> None:
    """Cross-test isolation: a prior test's pipeline never appears here."""
    rows = await storage.pipelines.list()
    assert rows == []


# --- captured_logs -----------------------------------------------------------
def test_captured_logs_collects_records(captured_logs: LogCapture) -> None:
    """``captured_logs`` hands back the records emitted during a test."""
    logger = logging.getLogger("jobfucker.smoke")
    logger.info("hello from smoke")
    logger.warning("watch out")

    assert "hello from smoke" in captured_logs.messages()
    assert "watch out" in captured_logs.messages()


# --- runtime_dir -------------------------------------------------------------
def test_runtime_dir_layout_and_env(runtime_dir: Path) -> None:
    """``runtime_dir`` builds config/data subdirs and wires CONFIG_DIR/DATA_DIR."""
    config = runtime_dir / "config"
    data = runtime_dir / "data"
    assert config.is_dir()
    assert data.is_dir()
    assert os.environ["CONFIG_DIR"] == str(config)
    assert os.environ["DATA_DIR"] == str(data)


# --- make_config_file --------------------------------------------------------
def test_make_config_file_writes_into_config_dir(runtime_dir: Path, make_config_file: MakeConfigFileFn) -> None:
    """``make_config_file`` writes a named file into the runtime config dir."""
    path = make_config_file("name: demo\n", name="pipeline.yaml")
    resume = make_config_file("# Resume\n", name="resume.md")

    assert path == runtime_dir / "config" / "pipeline.yaml"
    assert path.read_text(encoding="utf-8") == "name: demo\n"
    assert resume.read_text(encoding="utf-8") == "# Resume\n"
