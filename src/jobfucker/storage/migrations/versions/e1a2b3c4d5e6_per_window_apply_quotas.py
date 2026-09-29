"""per-window apply quotas: two counters (auth + pipeline) with a period

Revision ID: e1a2b3c4d5e6
Revises: a9b8c7d6e5f4
Create Date: 2026-09-25 18:30:00.000000

The single ``daily_limits`` account counter is replaced by two independent
counters over the board's declared quota window (``day`` for HH, ``month`` for
Habr):

- ``auth_apply_limits`` (``service, login, period, period_key``) — the shared
  per-account counter, converted 1:1 from the old ``daily_limits`` rows with
  ``period='day'`` and the old ``date`` becoming ``period_key``;
- ``pipeline_apply_limits`` (``pipeline_id, period, period_key``) — a new
  per-pipeline counter. It starts empty: the old model kept no per-pipeline
  history, so previous applications are not backfilled into it (lossy by
  design, local dev data).

The snapshot column ``daily_apply_limit`` is renamed to ``apply_limit``
(data-preserving). Downgrade collapses ``auth_apply_limits`` back to
``daily_limits`` using ``period_key`` as ``date`` and drops the per-pipeline
table; month-window rows are not representable in the old shape (lossy).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5e6"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    snapshot_cols = _columns("pipeline_snapshot")
    with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
        if "daily_apply_limit" in snapshot_cols and "apply_limit" not in snapshot_cols:
            batch_op.alter_column(
                "daily_apply_limit",
                new_column_name="apply_limit",
                existing_type=sa.Integer(),
                nullable=False,
            )

    tables = _table_names()
    if "daily_limits" in tables and "auth_apply_limits" not in tables:
        _rebuild_auth_limits_from_daily_limits()
    if "pipeline_apply_limits" not in _table_names():
        _create_pipeline_apply_limits()


def _rebuild_auth_limits_from_daily_limits() -> None:
    """Convert ``daily_limits(service, login, date)`` into the window-keyed table."""
    conn = op.get_bind()
    op.create_table(
        "auth_apply_limits_new",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service", sa.Text(), server_default="hh", nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), server_default="day", nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_auth_apply_limits"),
        sa.UniqueConstraint(
            "service", "login", "period", "period_key", name="uq_auth_apply_limits_service_login_period_key"
        ),
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO auth_apply_limits_new (service, login, period, period_key, count, created_at, updated_at)
            SELECT service, login, 'day', date, count, created_at, updated_at
            FROM daily_limits
            """
        )
    )
    op.drop_table("daily_limits")
    op.execute("ALTER TABLE auth_apply_limits_new RENAME TO auth_apply_limits")


def _create_pipeline_apply_limits() -> None:
    op.create_table(
        "pipeline_apply_limits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.Text(), server_default="day", nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.ForeignKeyConstraint(
            ["pipeline_id"], ["pipelines.id"], name="fk_pipeline_apply_limits_pipeline_id_pipelines"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_apply_limits"),
        sa.UniqueConstraint("pipeline_id", "period", "period_key", name="uq_pipeline_apply_limits_pipeline_period_key"),
    )
    with op.batch_alter_table("pipeline_apply_limits", schema=None) as batch_op:
        batch_op.create_index("ix_pipeline_apply_limits_pipeline_id", ["pipeline_id"], unique=False)


def downgrade() -> None:
    if "pipeline_apply_limits" in _table_names():
        op.drop_table("pipeline_apply_limits")

    tables = _table_names()
    if "auth_apply_limits" in tables and "daily_limits" not in tables:
        _rebuild_daily_limits_from_auth_limits()

    snapshot_cols = _columns("pipeline_snapshot")
    with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
        if "apply_limit" in snapshot_cols and "daily_apply_limit" not in snapshot_cols:
            batch_op.alter_column(
                "apply_limit",
                new_column_name="daily_apply_limit",
                existing_type=sa.Integer(),
                nullable=False,
            )


def _rebuild_daily_limits_from_auth_limits() -> None:
    """Collapse the window-keyed auth counter back to ``daily_limits`` (lossy for months)."""
    conn = op.get_bind()
    op.create_table(
        "daily_limits_new",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service", sa.Text(), server_default="hh", nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now'))"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_daily_limits"),
        sa.UniqueConstraint("service", "login", "date", name="uq_daily_limits_service_login_date"),
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO daily_limits_new (service, login, date, count, created_at, updated_at)
            SELECT service, login, period_key, SUM(count), MIN(created_at), MAX(updated_at)
            FROM auth_apply_limits
            GROUP BY service, login, period_key
            """
        )
    )
    op.drop_table("auth_apply_limits")
    op.execute("ALTER TABLE daily_limits_new RENAME TO daily_limits")
