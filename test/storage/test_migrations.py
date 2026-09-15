"""Phase 3 (task 3.3): Alembic migration tests.

Proves the canonical schema path — ``alembic upgrade head`` produces the five
tables (identity ``pipelines`` + ``pipeline_snapshot`` + the three result/
counter tables) exactly as the ORM models define — plus the non-destructive,
idempotent startup path (``storage.db.apply_migrations``) and the **data-shaping**
backfill (N legacy identity+config rows → N identities + N snapshots + backfilled
result/snapshot provenance + summed auth-keyed daily counts).

These tests use a real **file** DB (``tmp_path``), not in-memory, because
Alembic connects with its own pooled engine; a file DB behaves like production
SQLite and avoids the in-memory single-connection pitfall.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from jobfucker.clients.base import ServiceVacancyId
from jobfucker.storage.db import Storage, apply_migrations, open_storage
from jobfucker.storage.dto import VacancyRecord
from jobfucker.storage.models import Base
from test.storage.builders import build_vacancy, make_pipeline, make_snapshot

EXPECTED_TABLES = frozenset({"pipelines", "pipeline_snapshot", "vacancies", "audit_log", "daily_limits"})


def _insert_vacancy_row(engine: Engine, record: VacancyRecord) -> None:
    """Insert a vacancy via Core using only columns the live revision has.

    The ORM model tracks head, so a repo insert at a pre-head revision would
    name columns no migration has created yet (``has_hh_test``); the live
    inspector column set keeps the fixture valid at the intermediate revision
    this test deliberately starts from.
    """
    table = Base.metadata.tables["vacancies"]
    live = {column["name"] for column in inspect(engine).get_columns("vacancies")}
    values: dict[str, object] = {  # lint-ignore[restricted-object]: SQLAlchemy Core insert values
        "pipeline_id": record.pipeline_id,
        "fetched_snapshot_id": record.fetched_snapshot_id,
        "scored_snapshot_id": record.scored_snapshot_id,
        "generated_snapshot_id": record.generated_snapshot_id,
        "applied_snapshot_id": record.applied_snapshot_id,
        "external_id": record.external_id,
        "title": record.title,
        "url": record.url,
        "company": record.company,
        "description": record.description,
        "salary": record.salary,
        "score": record.score,
        "score_reasoning": record.score_reasoning,
        "score_error": record.score_error,
        "cover_letter": record.cover_letter,
        "cover_letter_error": record.cover_letter_error,
        "apply_status": record.apply_status,
        "apply_error": record.apply_error,
        "skip_reason": record.skip_reason,
        "applied_at": record.applied_at,
        "fetched_at": record.fetched_at,
        "scored_at": record.scored_at,
        "generated_at": record.generated_at,
        "user_edited_at": record.user_edited_at,
        "manual_skip": int(record.manual_skip),
        "manual_skip_reason": record.manual_skip_reason,
        "notes": record.notes,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "soft_deleted_at": record.soft_deleted_at,
    }
    live_values = {name: value for name, value in values.items() if name in live}
    with engine.begin() as conn:
        conn.execute(table.insert().values(**live_values))


@pytest.fixture
def migrated_storage(tmp_path: Path) -> Generator[Storage]:
    """A file-backed, migrated :class:`Storage` whose engine is disposed on exit."""
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)
    storage = open_storage(db)
    try:
        yield storage
    finally:
        asyncio.run(storage.engine.dispose())


def _table_names(engine: Engine) -> set[str]:
    """Return the set of user tables present on ``engine`` (excluding alembic's)."""
    names = set(inspect(engine).get_table_names())
    names.discard("alembic_version")
    return names


def _column_names(engine: Engine, table: str) -> set[str]:
    """Return the column names of ``table`` on ``engine``."""
    return {col["name"] for col in inspect(engine).get_columns(table)}


@pytest.mark.integration
async def test_alembic_upgrade_creates_the_five_tables(tmp_path: Path) -> None:
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)

    engine = create_engine(f"sqlite:///{db}")
    try:
        assert _table_names(engine) == set(EXPECTED_TABLES)
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_migrated_schema_matches_orm_models(tmp_path: Path) -> None:
    """The migration-built schema is exactly what the models define."""
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)

    engine = create_engine(f"sqlite:///{db}")
    try:
        # The models' metadata declares the same five tables as canonical.
        assert set(Base.metadata.tables.keys()) == set(EXPECTED_TABLES)
        assert _table_names(engine) == set(Base.metadata.tables.keys())
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_migrated_columns_match_orm_models(tmp_path: Path) -> None:
    """Every migrated table exposes exactly its ORM model's columns.

    Guards the post-snapshot contract: ``pipelines`` is identity-only (no config
    columns, no ``is_active``), ``pipeline_snapshot`` carries the config-content
    columns, ``daily_limits`` is auth-keyed ``(service, login, date)``, and
    ``vacancies``/``audit_log`` carry the snapshot-provenance columns — nothing
    may drift from the models.
    """
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)

    engine = create_engine(f"sqlite:///{db}")
    try:
        for table in EXPECTED_TABLES:
            orm_columns = set(Base.metadata.tables[table].columns.keys())
            migrated_columns = _column_names(engine, table)
            assert migrated_columns == orm_columns, f"columns drifted for {table}"

        # pipelines: identity only
        pipeline_cols = _column_names(engine, "pipelines")
        identity_cols = {
            "id",
            "name",
            "description",
            "current_snapshot_id",
            "created_at",
            "updated_at",
            "soft_deleted_at",
        }
        assert identity_cols <= pipeline_cols
        assert "is_active" not in pipeline_cols
        for config_col in (
            "query",
            "service",
            "service_section",
            "openai_captcha",
            "login",
            "password",
            "resume",
            "openai_model",
            "openai_base_url",
            "openai_api_key",
            "openai_reasoning_effort",
            "min_required_score",
            "scoring_prompt",
            "apply_prompt",
            "daily_apply_limit",
        ):
            assert config_col not in pipeline_cols

        # pipeline_snapshot carries the config content
        snapshot_cols = _column_names(engine, "pipeline_snapshot")
        for content_col in ("service", "service_section", "openai_captcha", "login", "password", "resume"):
            assert content_col in snapshot_cols

        # daily_limits auth-keyed
        daily_cols = _column_names(engine, "daily_limits")
        assert {"service", "login", "date", "count"} <= daily_cols
        assert "pipeline_id" not in daily_cols
        assert "auth_identifier" not in daily_cols

        # vacancy/audit snapshot provenance
        vacancy_cols = _column_names(engine, "vacancies")
        for snap_col in ("fetched_snapshot_id", "scored_snapshot_id", "generated_snapshot_id", "applied_snapshot_id"):
            assert snap_col in vacancy_cols
        assert "pipeline_snapshot_id" in _column_names(engine, "audit_log")
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_reply_to_apply_migration_preserves_and_renames_data(tmp_path: Path) -> None:
    """Scenario: an existing DB reaches the application-vocabulary schema.

    Given a database at the previous head with a successful reply and reply
    audit entry, when it upgrades to head, then prompt/result columns use the
    new names and the stored success/audit vocabulary becomes ``applied``.
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "a0b1c2d3e4f5")

    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO pipelines (name) VALUES ('rename-test')"))
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_snapshot
                        (pipeline_id, snapshot_no, source, query, service, login,
                         password, resume, openai_model, openai_api_key,
                         min_required_score, scoring_prompt, reply_prompt,
                         daily_apply_limit)
                    VALUES
                        (1, 1, 'from_file', 'python', 'mock', 'login', 'password',
                         'resume', 'model', 'key', 3, 'score prompt', 'reply prompt', 5)
                    """
                )
            )
            conn.execute(text("UPDATE pipelines SET current_snapshot_id=1 WHERE id=1"))
            conn.execute(
                text(
                    """
                    INSERT INTO vacancies
                        (pipeline_id, replied_snapshot_id, external_id, title, url,
                         description, reply_status, replied_at)
                    VALUES
                        (1, 1, 'vac-1', 'Python Developer', 'https://x/vac-1',
                         'description', 'replied', '2026-08-30 10:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO audit_log
                        (pipeline_id, pipeline_snapshot_id, action, details)
                    VALUES (1, 1, 'reply', '{"replied": 1, "total": 1}')
                    """
                )
            )
    finally:
        engine.dispose()

    _alembic_cmd(db, "upgrade", "head")

    engine = create_engine(f"sqlite:///{db}")
    try:
        assert "apply_prompt" in _column_names(engine, "pipeline_snapshot")
        assert "reply_prompt" not in _column_names(engine, "pipeline_snapshot")
        vacancy_columns = _column_names(engine, "vacancies")
        assert {"apply_status", "apply_error", "applied_at", "applied_snapshot_id"} <= vacancy_columns
        assert not {"reply_status", "reply_error", "replied_at", "replied_snapshot_id"} & vacancy_columns

        with engine.connect() as conn:
            prompt = conn.execute(text("SELECT apply_prompt FROM pipeline_snapshot WHERE id=1")).one()
            status = conn.execute(
                text("SELECT apply_status, applied_at, applied_snapshot_id FROM vacancies WHERE id=1")
            ).one()
            audit = conn.execute(text("SELECT action, details FROM audit_log WHERE id=1")).one()
        assert prompt == ("reply prompt",)
        assert status == ("applied", "2026-08-30 10:00:00", 1)
        assert audit == ("apply", '{"applied": 1, "total": 1}')
    finally:
        engine.dispose()


def _alembic_cmd(db: Path, *args: str) -> None:
    """Run an alembic CLI command against the file DB at ``db``.

    Alembic is imported at module level (not lazily here) on purpose: its
    one-time ``setup plugin`` DEBUG records fire at import, and a lazily
    imported alembic would leak them into any root-DEBUG capture a test has
    already attached (order-dependent flake under xdist scheduling).
    """
    ini = Path(__file__).resolve().parents[2] / "src" / "jobfucker" / "storage" / "migrations" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    getattr(command, args[0])(cfg, *args[1:])


def _pipelines_columns(db: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db}")
    try:
        return _column_names(engine, "pipelines")
    finally:
        engine.dispose()


def _seed_legacy_rows(db: Path) -> None:
    """Seed N legacy (identity+config-in-one-row) pipelines + results at prior head.

    Runs raw SQL because the ORM models at head no longer describe the legacy
    shape. Two pipelines, two vacancies, two audit rows, and daily counts that
    must SUM by (service, login, date) across pipelines after the migration.
    """
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO pipelines
                        (name, description, query, service, service_section, openai_captcha,
                         login, password, resume, openai_model, openai_thinking_level,
                         openai_base_url, openai_api_key, min_required_score, scoring_prompt,
                         reply_prompt, daily_apply_limit, is_active)
                    VALUES
                        ('p-a', 'alpha', 'python', 'mock', NULL, NULL, 'loginA', 'pwA', 'resumeA',
                         'gpt-a', 'medium', NULL, 'keyA', 3, 'scoreA', 'replyA', 50, 1),
                        ('p-b', 'beta', 'java', 'mock', NULL, NULL, 'loginB', 'pwB', 'resumeB',
                         'gpt-b', 'medium', NULL, 'keyB', 4, 'scoreB', 'replyB', 60, 1)
                    """
                )
            )
            # vacancies (legacy columns only)
            conn.execute(
                text(
                    """
                    INSERT INTO vacancies
                        (pipeline_id, position_index, external_id, title, url, company, description,
                         salary, score, score_reasoning, score_error, cover_letter, cover_letter_error,
                         reply_status, reply_error, replied_at, manual_skip, manual_skip_reason, notes,
                         archived, created_at, updated_at, soft_deleted_at)
                    VALUES
                        (1, 0, 'vac-1', 'Senior Python', 'https://x/v1', 'ACME', 'Senior role', NULL,
                         5, 'good', NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0,
                         '2026-08-05 09:00:00', '2026-08-05 09:00:00', NULL),
                        (2, 0, 'vac-2', 'Junior Java', 'https://x/v2', 'XYZ', 'Junior role', NULL,
                         NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0,
                         '2026-08-05 09:00:00', '2026-08-05 09:00:00', NULL)
                    """
                )
            )
            # audit rows (legacy columns only)
            conn.execute(
                text(
                    """
                    INSERT INTO audit_log (pipeline_id, action, details, created_at)
                    VALUES (1, 'fetch', 'fetch-op', '2026-08-05 09:00:00'),
                           (2, 'score', 'score-op', '2026-08-05 09:00:00')
                    """
                )
            )
            # daily limits: shared@ex.com split across 2 pipelines MUST sum to 8
            conn.execute(
                text(
                    """
                    INSERT INTO daily_limits (pipeline_id, auth_identifier, date, count, created_at, updated_at)
                    VALUES (1, 'shared@ex.com', '2026-08-05', 3, '2026-08-05 09:00:00', '2026-08-05 09:00:00'),
                           (2, 'shared@ex.com', '2026-08-05', 5, '2026-08-05 09:00:00', '2026-08-05 09:00:00'),
                           (1, 'other@ex.com', '2026-08-06', 2, '2026-08-06 09:00:00', '2026-08-06 09:00:00')
                    """
                )
            )
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_migration_backfills_legacy_rows_into_identity_and_snapshots(tmp_path: Path) -> None:
    """Data-shaping: N legacy rows -> N identities + N snapshots + backfilled provenance.

    Counts are summed (never lost) by (service, login, date); every vacancy/audit
    row is pointed at its pipeline's migrated snapshot.
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "d4e5f6a7b8c9")  # prior head (legacy shape)
    _seed_legacy_rows(db)
    apply_migrations(db)  # upgrade to new head

    storage = open_storage(db)
    try:
        pipelines = await storage.pipelines.list()
        assert [p.name for p in pipelines] == ["p-a", "p-b"]
        assert len(pipelines) == 2

        for identity in pipelines:
            snapshots = await storage.snapshots.list(identity.id)
            assert len(snapshots) == 1
            snap = snapshots[0]
            assert snap.snapshot_no == 1
            assert snap.source == "from_file"
            assert snap.note is None
            # current_snapshot_id points at the single migrated snapshot
            assert identity.current_snapshot_id == snap.id
            # config content copied verbatim from the legacy row
            assert snap.min_required_score in (3, 4)

        # vacancy/audit provenance backfilled to the pipeline's snapshot
        p1 = next(p for p in pipelines if p.name == "p-a")
        p2 = next(p for p in pipelines if p.name == "p-b")
        v1 = (await storage.vacancies.list_by_pipeline(p1.id))[0]
        assert v1.external_id == ServiceVacancyId("vac-1")
        assert v1.fetched_snapshot_id == (await storage.snapshots.current(p1.id)).id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
        assert v1.scored_snapshot_id == (await storage.snapshots.current(p1.id)).id  # type: ignore[union-attr]  # rationale: repo accessor returns Optional; the row was just written by the assertion's own preceding call
        assert (await storage.vacancies.list_by_pipeline(p2.id))[0].external_id == ServiceVacancyId("vac-2")

        audit = await storage.audit_log.list()
        assert len(audit) == 2
        for entry in audit:
            assert entry.pipeline_snapshot_id is not None

        # daily counts summed by auth, never lost
        shared = await storage.daily_limits.get("mock", "shared@ex.com", "2026-08-05")
        assert shared is not None
        assert shared.count == 8  # 3 + 5 from two pipelines summed into one auth counter
        other = await storage.daily_limits.get("mock", "other@ex.com", "2026-08-06")
        assert other is not None
        assert other.count == 2
        assert len(await storage.daily_limits.list()) == 2
    finally:
        await storage.engine.dispose()


@pytest.mark.integration
async def test_migration_downgrade_restores_legacy_pipelines_shape(tmp_path: Path) -> None:
    """Downgrading the head revision reinstates identity+config-in-one-row.

    At the previous head the ``pipelines`` table carries the config columns +
    ``is_active`` and no ``pipeline_snapshot`` table; upgrading restores the
    snapshot shape. (``daily_limits`` downgrade is best-effort/lossy by design.)
    """
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)  # upgrade to head (identity-only + snapshots)
    head_engine = create_engine(f"sqlite:///{db}")
    head_cols = _pipelines_columns(db)
    assert "current_snapshot_id" in head_cols
    assert "is_active" not in head_cols
    try:
        assert "pipeline_snapshot" in _table_names(head_engine)
    finally:
        head_engine.dispose()

    _alembic_cmd(db, "downgrade", "d4e5f6a7b8c9")
    down_cols = _pipelines_columns(db)
    assert "current_snapshot_id" not in down_cols
    assert "is_active" in down_cols
    assert "query" in down_cols and "openai_api_key" in down_cols
    down_engine = create_engine(f"sqlite:///{db}")
    try:
        assert "pipeline_snapshot" not in _table_names(down_engine)
    finally:
        down_engine.dispose()

    _alembic_cmd(db, "upgrade", "head")
    rehead_cols = _pipelines_columns(db)
    assert "current_snapshot_id" in rehead_cols
    assert "is_active" not in rehead_cols
    rehead_engine = create_engine(f"sqlite:///{db}")
    try:
        assert "pipeline_snapshot" in _table_names(rehead_engine)
    finally:
        rehead_engine.dispose()


@pytest.mark.integration
async def test_stale_intermediate_db_recovers_to_target_schema(tmp_path: Path) -> None:
    """Regression: a stale intermediate DB reaches head instead of crashing."""
    db = tmp_path / "jobfucker.db"
    # Build the "prototype-a1b2" state seen on the user's real DB.
    _alembic_cmd(db, "upgrade", "a1b2c3d4e5f6")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE pipelines DROP COLUMN openai_captcha"))
    finally:
        engine.dispose()
    assert "openai_captcha" not in _pipelines_columns(db)

    # This is the exact call that used to crash on every open-storage command.
    apply_migrations(db)

    cols = _pipelines_columns(db)
    assert "current_snapshot_id" in cols
    assert "is_active" not in cols
    assert "query" not in cols
    engine = create_engine(f"sqlite:///{db}")
    try:
        assert "pipeline_snapshot" in _table_names(engine)
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_apply_migrations_is_idempotent_and_non_destructive(tmp_path: Path) -> None:
    """Re-applying migrations never drops or corrupts existing data."""
    db = tmp_path / "jobfucker.db"
    apply_migrations(db)

    storage = open_storage(db)
    try:
        pipeline = await storage.pipelines.create(make_pipeline(name="kept"))
        snap = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=1))
        await storage.pipelines.set_current_snapshot(pipeline.id, snap.id)
        await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", title="kept-vacancy"))
    finally:
        await storage.engine.dispose()

    # Re-run the startup migration path (no-op at head) — must keep data.
    apply_migrations(db)

    storage = open_storage(db)
    try:
        stored = await storage.pipelines.get(pipeline.id)
        assert stored is not None
        assert stored.name == "kept"
        assert stored.current_snapshot_id == snap.id
        rows = await storage.vacancies.list_by_pipeline(pipeline.id)
        assert [r.title for r in rows] == ["kept-vacancy"]
    finally:
        await storage.engine.dispose()


@pytest.mark.integration
async def test_migration_drops_position_archived_and_backfills_timestamps(tmp_path: Path) -> None:
    """Head has no ``position_index``/``archived`` and backfills the staleness stamps.

    ``position_index`` (a last-seen listing rank that never was identity) and
    the board ``archived`` marker are dropped entirely — ``soft_deleted_at`` is
    the only lifecycle signal, set manually only. ``fetched_at`` is backfilled
    from ``created_at``; ``scored_at``/``generated_at`` from ``updated_at``
    where an artifact exists.
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "a7b8c9d0e1f2")  # prior head (still has the columns)
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO pipelines (name, description, current_snapshot_id, created_at, updated_at)
                    VALUES ('p', NULL, NULL, '2026-08-05 09:00:00', '2026-08-05 09:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO vacancies
                        (pipeline_id, position_index, external_id, title, url, company, description,
                         salary, score, score_reasoning, score_error, cover_letter, cover_letter_error,
                         reply_status, reply_error, replied_at, manual_skip, manual_skip_reason, notes,
                         archived, created_at, updated_at, soft_deleted_at)
                    VALUES
                        (1, 0, 'scored', 'Scored', 'https://x/1', 'ACME', 'desc', NULL,
                         4, 'ok', NULL, 'a letter', NULL, NULL, NULL, NULL, 0, NULL, NULL, 0,
                         '2026-08-05 09:00:00', '2026-08-05 12:00:00', NULL),
                        (1, 1, 'plain', 'Plain', 'https://x/2', 'ACME', 'desc', NULL,
                         NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 0,
                         '2026-08-05 09:00:00', '2026-08-05 12:00:00', NULL)
                    """
                )
            )
    finally:
        engine.dispose()

    apply_migrations(db)

    engine = create_engine(f"sqlite:///{db}")
    try:
        vacancy_cols = _column_names(engine, "vacancies")
        assert "position_index" not in vacancy_cols
        assert "archived" not in vacancy_cols
        for col in ("fetched_at", "scored_at", "generated_at", "user_edited_at"):
            assert col in vacancy_cols
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT external_id, fetched_at, scored_at, generated_at, user_edited_at FROM vacancies ORDER BY id"
                )
            ).all()
        # fetched_at backfilled from created_at; artifact stamps from updated_at.
        assert rows[0][0] == "scored"
        assert rows[0][1] == "2026-08-05 09:00:00"  # fetched_at = created_at
        assert rows[0][2] == "2026-08-05 12:00:00"  # scored_at = updated_at (score present)
        assert rows[0][3] == "2026-08-05 12:00:00"  # generated_at = updated_at (letter present)
        assert rows[0][4] is None  # user_edited_at not recoverable
        assert rows[1][0] == "plain"
        assert rows[1][1] == "2026-08-05 09:00:00"
        assert rows[1][2] is None  # no score
        assert rows[1][3] is None  # no cover letter
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_repos_work_on_migrated_file_db(migrated_storage: Storage) -> None:
    """CRC: repositories operate correctly on a migration-built file DB."""
    pipeline = await migrated_storage.pipelines.create(make_pipeline())
    await migrated_storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v1", score=5))

    # skip-already-processed on score: only a score counts as the stage's result.
    assert await migrated_storage.vacancies.external_ids_scored(pipeline.id) == {ServiceVacancyId("v1")}
    # Stage-relative: the same row is NOT "done" for apply (no apply decision).
    assert await migrated_storage.vacancies.external_ids_decided(pipeline.id) == set()


@pytest.mark.integration
async def test_migration_downgrade_restores_daily_limits_without_null_or_duplicate_pipelines(tmp_path: Path) -> None:
    """F5: downgrading a populated DB re-keys daily_limits back to per-pipeline
    rows — no NULL ``pipeline_id`` and no ``UNIQUE(pipeline_id,
    auth_identifier, date)`` duplicates (best-effort/lossy head-snapshot
    pairing: rows with no matching pipeline are dropped, counts summed per key).
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "d4e5f6a7b8c9")  # legacy (pre-snapshot) head
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            # Two pipelines share one account (their auth login IS the account id);
            # a third uses its own account.
            conn.execute(
                text(
                    """
                    INSERT INTO pipelines
                        (name, description, query, service, service_section, openai_captcha,
                         login, password, resume, openai_model, openai_thinking_level,
                         openai_base_url, openai_api_key, min_required_score, scoring_prompt,
                         reply_prompt, daily_apply_limit, is_active)
                    VALUES
                        ('p-a', 'alpha', 'python', 'mock', NULL, NULL, 'acct@ex.com', 'pwA', 'resumeA',
                         'gpt-a', 'medium', NULL, 'keyA', 3, 'scoreA', 'replyA', 50, 1),
                        ('p-b', 'beta', 'java', 'mock', NULL, NULL, 'acct@ex.com', 'pwB', 'resumeB',
                         'gpt-b', 'medium', NULL, 'keyB', 4, 'scoreB', 'replyB', 60, 1),
                        ('p-c', 'gamma', 'rust', 'mock', NULL, NULL, 'other@ex.com', 'pwC', 'resumeC',
                         'gpt-c', 'medium', NULL, 'keyC', 5, 'scoreC', 'replyC', 70, 1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO daily_limits (pipeline_id, auth_identifier, date, count, created_at, updated_at)
                    VALUES (1, 'acct@ex.com', '2026-08-05', 3, '2026-08-05 09:00:00', '2026-08-05 09:00:00'),
                           (2, 'acct@ex.com', '2026-08-05', 5, '2026-08-05 09:00:00', '2026-08-05 09:00:00'),
                           (3, 'other@ex.com', '2026-08-06', 2, '2026-08-06 09:00:00', '2026-08-06 09:00:00')
                    """
                )
            )
    finally:
        engine.dispose()

    apply_migrations(db)  # upgrade to head (auth-keyed daily_limits)
    _alembic_cmd(db, "downgrade", "d4e5f6a7b8c9")  # back to the legacy shape

    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT pipeline_id, auth_identifier, date FROM daily_limits ORDER BY pipeline_id, date")
            ).all()
        assert rows, "expected per-pipeline daily_limits rows after downgrade"
        assert all(row[0] is not None for row in rows), "NULL pipeline_id must not be inserted"
        keys = [(row[0], row[1], row[2]) for row in rows]
        assert len(keys) == len(set(keys)), "duplicate (pipeline_id, auth_identifier, date) after downgrade"
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_apply_migrations_preserves_app_configured_logging(tmp_path: Path) -> None:
    """Regression: alembic's ``fileConfig`` must not wipe the app's logging tree.

    ``env.py`` calls ``fileConfig()``, which reprograms the whole logging tree
    (root level from ``[logger_root]`` = WARNING, root handlers replaced by the
    ini's console handler, pre-existing loggers disabled). The CLI/GUI configure
    their logging BEFORE storage opens (file + console handlers, root at DEBUG),
    so migrations running mid-command used to silently kill the app's logs —
    including the AI request/response DEBUG records. The env must restore a
    pre-configured root level + handlers and leave app loggers enabled.
    """
    root = logging.getLogger()
    original_handlers = root.handlers.copy()
    original_level = root.level
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    capture = _Capture(level=logging.DEBUG)
    try:
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        apply_migrations(tmp_path / "jobfucker.db")
        # fileConfig did not reset the root level, replace the handlers, or
        # disable a pre-existing app logger.
        assert root.level == logging.DEBUG
        assert capture in root.handlers
        assert logging.getLogger("jobfucker.ai").isEnabledFor(logging.DEBUG)
        logging.getLogger("jobfucker.ai").debug("post-migration debug line")
    finally:
        root.removeHandler(capture)
        capture.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert captured == ["post-migration debug line"]


@pytest.mark.integration
async def test_migration_backfills_test_skipped_rows(tmp_path: Path) -> None:
    """The hh-test column migration flags + un-decides old test-skip rows.

    Rows skipped by the pre-feature preflight with the test-skip text are
    flagged ``has_hh_test`` and their apply state is rolled back to pending
    (score/cover-letter provenance kept), so they can be re-applied through the
    new test flow without re-fetching the whole listing. Other skip texts and
    decided rows are untouched.
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "d5e6f7a8b9c0")  # parent of the hh-test migration
    storage = open_storage(db)
    try:
        pipeline = await storage.pipelines.create(make_pipeline(name="hh-backfill"))
    finally:
        await storage.engine.dispose()

    engine = create_engine(f"sqlite:///{db}")
    try:
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="test_skipped",
                score=5,
                cover_letter="cl",
                apply_status="skipped",
                skip_reason="HH vacancy 1 requires an unsupported test",
            ),
        )
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="closed",
                score=5,
                apply_status="skipped",
                skip_reason="HH vacancy 2 is archived or closed for applicants",
            ),
        )
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id, external_id="applied", score=5, cover_letter="cl", apply_status="applied"
            ),
        )
    finally:
        engine.dispose()

    apply_migrations(db)

    storage = open_storage(db)
    try:
        rows = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
        rolled_back = rows[ServiceVacancyId("test_skipped")]
        assert rolled_back.has_hh_test is True
        assert rolled_back.apply_status is None
        assert rolled_back.skip_reason is None
        assert rolled_back.score == 5  # scoring provenance preserved
        assert rolled_back.cover_letter == "cl"

        closed = rows[ServiceVacancyId("closed")]
        assert closed.has_hh_test is not True
        assert closed.apply_status == "skipped"

        applied = rows[ServiceVacancyId("applied")]
        assert applied.has_hh_test is not True
        assert applied.apply_status == "applied"
    finally:
        await storage.engine.dispose()

    # The downgrade drops both added columns again (rollback shape).
    _alembic_cmd(db, "downgrade", "d5e6f7a8b9c0")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            vacancy_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(vacancies)"))}
            snapshot_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(pipeline_snapshot)"))}
    finally:
        engine.dispose()
    assert "has_hh_test" not in vacancy_columns
    assert "hh_test_solving" not in snapshot_columns


async def test_migration_decouples_apply_status_from_score_stamping(tmp_path: Path) -> None:
    """Upgrading to head clears the score stage's legacy 'skipped' stamps.

    The score stage used to write ``apply_status='skipped'`` on sub-threshold
    vacancies. Head resets exactly those rows — 'skipped' with neither a
    cover letter nor a ``skip_reason`` — back to NULL, keeping every real
    board decline: lettered declines, letterless declines from the
    ``--allow-without-letter`` apply path (they carry ``skip_reason``), and
    pre-revision declines. Other statuses pass through verbatim, and
    re-running the migrations is a safe no-op.
    """
    db = tmp_path / "jobfucker.db"
    _alembic_cmd(db, "upgrade", "b8e4f7a6c5d3")  # previous head (still stamps nothing new)
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_snapshot
                        (pipeline_id, snapshot_no, source, query, service, login,
                         password, resume, openai_model, openai_api_key,
                         min_required_score, scoring_prompt, apply_prompt,
                         daily_apply_limit)
                    VALUES
                        (1, 1, 'from_file', 'python', 'mock', 'login', 'password',
                         'resume', 'model', 'key', 3, 'score', 'apply', 5)
                    """
                )
            )
            conn.execute(text("UPDATE pipelines SET current_snapshot_id=1 WHERE id=1"))
    finally:
        engine.dispose()

    storage = open_storage(db)
    try:
        pipeline = await storage.pipelines.create(make_pipeline(name="decouple"))
    finally:
        await storage.engine.dispose()

    engine = create_engine(f"sqlite:///{db}")
    try:
        _insert_vacancy_row(
            engine, build_vacancy(pipeline_id=pipeline.id, external_id="stamped", score=2, apply_status="skipped")
        )
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id, external_id="declined", score=5, cover_letter="cl", apply_status="skipped"
            ),
        )
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="declined_letterless",
                score=5,
                apply_status="skipped",
                skip_reason="closed on the board",
            ),
        )
        _insert_vacancy_row(
            engine,
            build_vacancy(
                pipeline_id=pipeline.id, external_id="applied", score=5, cover_letter="cl", apply_status="applied"
            ),
        )
    finally:
        engine.dispose()

    apply_migrations(db)
    apply_migrations(db)  # re-running at head is a no-op (idempotent startup path)

    storage = open_storage(db)
    try:
        rows = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
        assert rows[ServiceVacancyId("stamped")].apply_status is None  # legacy score stamp cleared
        assert rows[ServiceVacancyId("declined")].apply_status == "skipped"  # lettered decline kept
        assert rows[ServiceVacancyId("declined_letterless")].apply_status == "skipped"  # skip_reason decline kept
        assert rows[ServiceVacancyId("applied")].apply_status == "applied"  # untouched
    finally:
        await storage.engine.dispose()
