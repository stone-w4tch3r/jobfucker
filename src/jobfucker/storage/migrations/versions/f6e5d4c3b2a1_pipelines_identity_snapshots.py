"""pipelines become identity-only; config moves to pipeline_snapshot

Revision ID: f6e5d4c3b2a1
Revises: d4e5f6a7b8c9
Create Date: 2026-08-06 06:00:00.000000

First **data-shaping** migration in the chain (per docs/specs/jobfucker.pipeline-
snapshots.md). It splits the single ``pipelines`` row (identity + config
snapshot, re-id'd on every replace=True update) into a stable identity table
(``pipelines``, config columns removed) plus an append-only ``pipeline_snapshot``
table, re-points results to their producing snapshot, and re-keys daily limits
per auth.

Because the config columns are dropped and the ORM at head is identity-only,
the backfills run as **raw SQL via ``op.get_bind()``** — the ORM models no longer
describe the legacy ``pipelines`` shape. Every step is guarded by the same
inspector/column-existence style as the rest of the chain so re-running or odd
intermediate states stay idempotent; the data backfills add **row-level** guards
(``WHERE NOT EXISTS`` / ``IS NULL``) so a retry after a partial failure cannot
insert a duplicate ``(pipeline_id, snapshot_no)`` row.

Sequence:
  1. ``CREATE TABLE pipeline_snapshot`` (+ ``uq_pipelines_name_active`` partial
     unique index on ``pipelines``).
  2. Add nullable ``current_snapshot_id`` to ``pipelines``.
  3. Backfill: one snapshot (snapshot_no=1, source='from_file') per pipeline,
     copying the legacy config columns; set ``current_snapshot_id`` to it.
  4. Add ``*_snapshot_id`` cols to ``vacancies`` + ``pipeline_snapshot_id`` to
     ``audit_log``; backfill each to the row's pipeline's snapshot.
  5. Re-key ``daily_limits`` to ``(service, login, date)``, SUM(count) grouped by
     auth — counts are **never lost**.
  6. Drop the config columns + ``is_active`` from ``pipelines``; add the FK
     constraints (``current_snapshot_id``, the 4 vacancy snapshot FKs, the audit
     snapshot FK) to ``pipeline_snapshot``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f6e5d4c3b2a1"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The legacy config columns (with their names already renamed to drop the
# ``*_file`` suffix at d4e5f6a7b8c9) that the snapshot backfill copies verbatim.
_CONFIG_COLUMNS: tuple[str, ...] = (
    "query",
    "service",
    "service_section",
    "openai_captcha",
    "login",
    "password",
    "resume",
    "openai_model",
    "openai_thinking_level",
    "openai_base_url",
    "openai_api_key",
    "min_required_score",
    "scoring_prompt",
    "reply_prompt",
    "daily_apply_limit",
)


def _columns(table: str) -> set[str]:
    """Return the current column names of ``table`` on the live connection."""
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "pipeline_snapshot" not in _table_names():
        _create_snapshot_table()
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pipelines_name_active "
        "ON pipelines(name) WHERE soft_deleted_at IS NULL"
    )
    _add_current_snapshot_id()
    _backfill_pipelines_into_snapshots()
    _add_and_backfill_vacancy_and_audit_snapshot_columns()
    _rekey_daily_limits()
    _strip_pipelines_config_and_add_fks()
    _add_snapshot_indexes()


def _table_names() -> set[str]:
    """Return the set of user table names on the live connection."""
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def _create_snapshot_table() -> None:
    """Create ``pipeline_snapshot`` full DDL (spec §DDL)."""
    op.create_table(
        "pipeline_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_no", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), server_default="manual", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), server_default="hh", nullable=False),
        sa.Column("service_section", sa.Text(), nullable=True),
        sa.Column("openai_captcha", sa.Text(), nullable=True),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("resume", sa.Text(), nullable=False),
        sa.Column("openai_model", sa.Text(), nullable=False),
        sa.Column("openai_thinking_level", sa.Text(), nullable=True),
        sa.Column("openai_base_url", sa.Text(), nullable=True),
        sa.Column("openai_api_key", sa.Text(), nullable=False),
        sa.Column("min_required_score", sa.Integer(), server_default="3", nullable=False),
        sa.Column("scoring_prompt", sa.Text(), nullable=False),
        sa.Column("reply_prompt", sa.Text(), nullable=False),
        sa.Column("daily_apply_limit", sa.Integer(), server_default="50", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.ForeignKeyConstraint(
            ["pipeline_id"], ["pipelines.id"], name=op.f("fk_pipeline_snapshot_pipeline_id_pipelines")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_snapshot")),
        sa.UniqueConstraint(
            "pipeline_id", "snapshot_no", name="uq_pipeline_snapshot_pipeline_snapshot_no"
        ),
    )
    with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_pipeline_snapshot_pipeline_id"), ["pipeline_id"], unique=False)


def _add_current_snapshot_id() -> None:
    cols = _columns("pipelines")
    if "current_snapshot_id" not in cols:
        with op.batch_alter_table("pipelines", schema=None) as batch_op:
            batch_op.add_column(sa.Column("current_snapshot_id", sa.Integer(), nullable=True))


def _backfill_pipelines_into_snapshots() -> None:
    """Create one snapshot per pipeline and point ``current_snapshot_id`` at it.

    Raw SQL: the ORM models at head no longer expose the legacy config columns,
    so they cannot express this copy. The ``WHERE NOT EXISTS`` guard makes the
    backfill **row-idempotent**: a retry after a partial failure cannot insert a
    second ``(pipeline_id, snapshot_no=1)`` row and trip
    ``UNIQUE(pipeline_id, snapshot_no)``.
    """
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO pipeline_snapshot
                (pipeline_id, snapshot_no, source, note, query, service, service_section,
                 openai_captcha, login, password, resume, openai_model, openai_thinking_level,
                 openai_base_url, openai_api_key, min_required_score, scoring_prompt,
                 reply_prompt, daily_apply_limit, created_at)
            SELECT id, 1, 'from_file', NULL, query, service, service_section, openai_captcha,
                   login, password, resume, openai_model, openai_thinking_level, openai_base_url,
                   openai_api_key, min_required_score, scoring_prompt, reply_prompt,
                   daily_apply_limit, (datetime('now'))
            FROM pipelines p
            WHERE NOT EXISTS (
                SELECT 1 FROM pipeline_snapshot ps WHERE ps.pipeline_id = p.id
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE pipelines SET current_snapshot_id = (
                SELECT ps.id FROM pipeline_snapshot ps
                WHERE ps.pipeline_id = pipelines.id AND ps.snapshot_no = 1
            )
            """
        )
    )


def _add_and_backfill_vacancy_and_audit_snapshot_columns() -> None:
    """Add nullable snapshot FK columns and backfill them to the pipeline's snapshot."""
    conn = op.get_bind()
    vacancy_columns = (
        "fetched_snapshot_id",
        "scored_snapshot_id",
        "generated_snapshot_id",
        "replied_snapshot_id",
    )
    existing = _columns("vacancies")
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        for col in vacancy_columns:
            if col not in existing:
                batch_op.add_column(sa.Column(col, sa.Integer(), nullable=True))

    # Re-inspect: batch recreation may have replaced the table.
    existing_vacancies = _columns("vacancies")
    if all(col in existing_vacancies for col in vacancy_columns):
        for col in vacancy_columns:
            conn.execute(
                sa.text(
                    f"""
                    UPDATE vacancies SET {col} = (
                        SELECT ps.id FROM pipeline_snapshot ps
                        WHERE ps.pipeline_id = vacancies.pipeline_id AND ps.snapshot_no = 1
                    )
                    WHERE {col} IS NULL
                    """
                )
            )

    audit_existing = _columns("audit_log")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        if "pipeline_snapshot_id" not in audit_existing:
            batch_op.add_column(sa.Column("pipeline_snapshot_id", sa.Integer(), nullable=True))
    if "pipeline_snapshot_id" in _columns("audit_log"):
        conn.execute(
            sa.text(
                """
                UPDATE audit_log SET pipeline_snapshot_id = (
                    SELECT ps.id FROM pipeline_snapshot ps
                    WHERE ps.pipeline_id = audit_log.pipeline_id AND ps.snapshot_no = 1
                )
                WHERE pipeline_snapshot_id IS NULL
                """
            )
        )


def _rekey_daily_limits() -> None:
    """Re-key ``daily_limits`` to ``(service, login, date)``, summing counts by auth.

    Adds a nullable ``service`` + ``login``, backfills them from the pipeline's
    snapshot (login from the legacy ``auth_identifier``), then collapses into a
    fresh table grouped by ``(service, login, date)`` with ``SUM(count)`` — so
    per-pipeline rows for one account never lose the account's shared count.
    """
    conn = op.get_bind()
    existing = _columns("daily_limits")
    with op.batch_alter_table("daily_limits", schema=None) as batch_op:
        if "service" not in existing:
            batch_op.add_column(sa.Column("service", sa.Text(), nullable=True))
        if "login" not in existing:
            batch_op.add_column(sa.Column("login", sa.Text(), nullable=True))

    conn.execute(
        sa.text(
            """
            UPDATE daily_limits SET
                login = COALESCE(auth_identifier, ''),
                service = COALESCE((
                    SELECT ps.service FROM pipeline_snapshot ps
                    WHERE ps.pipeline_id = daily_limits.pipeline_id AND ps.snapshot_no = 1
                ), 'hh')
            WHERE login IS NULL OR service IS NULL
            """
        )
    )

    op.create_table(
        "daily_limits_new",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service", sa.Text(), server_default="hh", nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_limits")),
        sa.UniqueConstraint("service", "login", "date", name="uq_daily_limits_service_login_date"),
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO daily_limits_new (service, login, date, count, created_at, updated_at)
            SELECT service, login, date, SUM(count), MIN(created_at), MAX(updated_at)
            FROM daily_limits
            GROUP BY service, login, date
            """
        )
    )
    op.drop_table("daily_limits")
    op.execute("ALTER TABLE daily_limits_new RENAME TO daily_limits")


def _strip_pipelines_config_and_add_fks() -> None:
    """Drop the config columns + ``is_active`` and add snapshot FK constraints."""
    cols = _columns("pipelines")
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        for col in _CONFIG_COLUMNS + ("is_active",):
            if col in cols:
                batch_op.drop_column(col)
        batch_op.create_foreign_key(
            "fk_pipelines_current_snapshot_id_pipeline_snapshot",
            "pipeline_snapshot",
            ["current_snapshot_id"],
            ["id"],
        )

    vacancy_cols = _columns("vacancies")
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        for col in ("fetched_snapshot_id", "scored_snapshot_id", "generated_snapshot_id", "replied_snapshot_id"):
            if col in vacancy_cols:
                batch_op.create_foreign_key(
                    f"fk_vacancies_{col}_pipeline_snapshot",
                    "pipeline_snapshot",
                    [col],
                    ["id"],
                )

    if "pipeline_snapshot_id" in _columns("audit_log"):
        with op.batch_alter_table("audit_log", schema=None) as batch_op:
            batch_op.create_foreign_key(
                "fk_audit_log_pipeline_snapshot_id_pipeline_snapshot",
                "pipeline_snapshot",
                ["pipeline_snapshot_id"],
                ["id"],
            )


def _add_snapshot_indexes() -> None:
    """Create the ``*_snapshot_id`` indexes declared ``index=True`` on the models."""
    bind = op.get_bind()
    vacancy_indexes = _index_names("vacancies")
    for col in ("fetched_snapshot_id", "scored_snapshot_id", "generated_snapshot_id", "replied_snapshot_id"):
        index_name = f"ix_vacancies_{col}"
        if index_name not in vacancy_indexes:
            op.create_index(index_name, "vacancies", [col], unique=False)
    del bind


def _index_names(table: str) -> set[str]:
    bind = op.get_bind()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def downgrade() -> None:
    """Best-effort reversal to the identity+config-in-one-row shape.

    Lossy for ``daily_limits``: the shared ``(service, login, date)`` counter is
    expanded back out to one row per pipeline whose **head snapshot** matches the
    account (rows with no matching pipeline are dropped), with counts summed per
    ``(pipeline_id, auth_identifier, date)`` so the restored shape never
    violates the legacy UNIQUE constraint. Config columns are restored by
    copying from each identity's current head snapshot.
    """
    # daily_limits pairing uses ``current_snapshot_id`` (the head), which the
    # config restore drops — so the counter restore must run first.
    _restore_daily_limits_pipeline_shape()
    _restore_pipelines_config_columns()
    _drop_vacancy_and_audit_snapshot_columns()
    op.execute("DROP INDEX IF EXISTS uq_pipelines_name_active")
    _drop_snapshot_table()


def _restore_pipelines_config_columns() -> None:
    """Re-add the config columns + ``is_active`` and copy from the head snapshot."""
    existing = _columns("pipelines")
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "is_active" not in existing:
            batch_op.add_column(sa.Column("is_active", sa.Integer(), server_default="1", nullable=False))
        new_cols = {
            "query": (sa.Text(), False, ""),
            "service": (sa.Text(), False, "hh"),
            "service_section": (sa.Text(), True),
            "openai_captcha": (sa.Text(), True),
            "login": (sa.Text(), False, ""),
            "password": (sa.Text(), False, ""),
            "resume": (sa.Text(), False, ""),
            "openai_model": (sa.Text(), False, ""),
            "openai_thinking_level": (sa.Text(), True),
            "openai_base_url": (sa.Text(), True),
            "openai_api_key": (sa.Text(), False, ""),
            "min_required_score": (sa.Integer(), False, 3),
            "scoring_prompt": (sa.Text(), False, ""),
            "reply_prompt": (sa.Text(), False, ""),
            "daily_apply_limit": (sa.Integer(), False, 50),
        }
        conn_cols = _columns("pipelines")
        for col, spec in new_cols.items():
            if col not in conn_cols:
                type_, nullable = spec[0], spec[1]
                # NOT NULL columns need a server default: SQLite refuses to ADD
                # a NOT NULL column without one when the table has rows. The
                # restore UPDATE below overwrites every one of these with the
                # head snapshot's value (COALESCE'd to the same defaults).
                batch_op.add_column(sa.Column(col, type_, nullable=nullable, server_default=str(spec[2]) if len(spec) > 2 else None))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE pipelines SET
                query = COALESCE((SELECT ps.query FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                service = COALESCE((SELECT ps.service FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), 'hh'),
                service_section = (SELECT ps.service_section FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id),
                openai_captcha = (SELECT ps.openai_captcha FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id),
                login = COALESCE((SELECT ps.login FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                password = COALESCE((SELECT ps.password FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                resume = COALESCE((SELECT ps.resume FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                openai_model = COALESCE((SELECT ps.openai_model FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                openai_thinking_level = (SELECT ps.openai_thinking_level FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id),
                openai_base_url = (SELECT ps.openai_base_url FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id),
                openai_api_key = COALESCE((SELECT ps.openai_api_key FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                min_required_score = COALESCE((SELECT ps.min_required_score FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), 3),
                scoring_prompt = COALESCE((SELECT ps.scoring_prompt FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                reply_prompt = COALESCE((SELECT ps.reply_prompt FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), ''),
                daily_apply_limit = COALESCE((SELECT ps.daily_apply_limit FROM pipeline_snapshot ps WHERE ps.id = pipelines.current_snapshot_id), 50)
            """
        )
    )
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        batch_op.drop_constraint("fk_pipelines_current_snapshot_id_pipeline_snapshot", type_="foreignkey")
        batch_op.drop_column("current_snapshot_id")


def _restore_daily_limits_pipeline_shape() -> None:
    """Expand the shared auth counter back into per-pipeline rows (lossy pairing).

    Each ``(service, login, date)`` counter row is paired to every identity
    whose **head snapshot** matches that auth (``service`` + ``login``); rows
    with no matching pipeline are **skipped** rather than inserted with a NULL
    ``pipeline_id``. Counts are summed per ``(pipeline_id, auth_identifier,
    date)`` so the restored shape can never violate the legacy
    ``UNIQUE(pipeline_id, auth_identifier, date)`` — even when several auth keys
    on the same date pair to one identity.
    """
    conn = op.get_bind()
    op.create_table(
        "daily_limits_old",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("auth_identifier", sa.Text(), nullable=False),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], name=op.f("fk_daily_limits_pipeline_id_pipelines")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_limits")),
        sa.UniqueConstraint("pipeline_id", "auth_identifier", "date", name="uq_daily_limits_pipeline_auth_date"),
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO daily_limits_old (pipeline_id, auth_identifier, date, count, created_at, updated_at)
            SELECT
                p.id,
                dl.login,
                dl.date,
                SUM(dl.count),
                MIN(dl.created_at),
                MAX(dl.updated_at)
            FROM daily_limits dl
            JOIN pipelines p ON p.current_snapshot_id IS NOT NULL
            JOIN pipeline_snapshot ps ON ps.id = p.current_snapshot_id
                AND ps.login = dl.login
                AND ps.service = dl.service
            GROUP BY p.id, dl.login, dl.date
            """
        )
    )
    op.drop_table("daily_limits")
    op.execute("ALTER TABLE daily_limits_old RENAME TO daily_limits")


def _drop_vacancy_and_audit_snapshot_columns() -> None:
    vacancy_cols = _columns("vacancies")
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        for col in ("fetched_snapshot_id", "scored_snapshot_id", "generated_snapshot_id", "replied_snapshot_id"):
            if col in vacancy_cols:
                # Alembic batch recreation re-creates reflected indexes; drop the
                # matching index too or the recreated table tries to index a now-
                # absent column.
                if f"ix_vacancies_{col}" in _index_names("vacancies"):
                    batch_op.drop_index(f"ix_vacancies_{col}")
                batch_op.drop_column(col)
    if "pipeline_snapshot_id" in _columns("audit_log"):
        with op.batch_alter_table("audit_log", schema=None) as batch_op:
            batch_op.drop_column("pipeline_snapshot_id")


def _drop_snapshot_table() -> None:
    if "pipeline_snapshot" in _table_names():
        op.drop_table("pipeline_snapshot")
