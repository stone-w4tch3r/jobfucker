"""Drop pipeline_snapshot.query: the search pool lives in service_section

Revision ID: d5e6f7a8b9c0
Revises: c9d0e1f2a3b4
Create Date: 2026-09-10 08:00:00.000000

Multi-search fetch moved every search's query (and its filter + fetch window)
into the ordered ``searches`` pool inside the opaque ``service_section`` JSON,
so the snapshot-level scalar ``query`` column has no source anymore. The column
is dropped; its values are redundant with the last snapshot's pool and are not
migrated (re-run ``init``/``update`` to re-append a pool-bearing snapshot).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite has no plain DROP COLUMN on old versions; batch_alter_table
    # rebuilds the table with the column removed (safe: no FK targets the
    # snapshot table's query column).
    with op.batch_alter_table("pipeline_snapshot") as batch:
        batch.drop_column("query")


def downgrade() -> None:
    # The dropped scalar cannot be reconstructed from the pool JSON without
    # picking an arbitrary entry; restoring the column empty keeps the schema
    # shape only.
    with op.batch_alter_table("pipeline_snapshot") as batch:
        batch.add_column(sa.Column("query", sa.Text(), nullable=False, server_default=""))
