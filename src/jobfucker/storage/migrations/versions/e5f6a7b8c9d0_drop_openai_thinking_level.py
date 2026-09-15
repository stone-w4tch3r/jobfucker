"""drop pipeline_snapshot.openai_thinking_level (no per-provider reasoning knob)

Revision ID: e5f6a7b8c9d0
Revises: f6e5d4c3b2a1
Create Date: 2026-08-07 00:00:00.000000

The per-provider AI reasoning parameter (``openai_thinking_level``) is removed
from the whole app: the reasoning API differs between providers (OpenAI
``reasoning_effort`` vs OpenRouter ``reasoning`` body), it is not really needed,
and it is too hard to support right now. At head the config-content column lives
on ``pipeline_snapshot`` (the ``pipelines`` identity row is config-free), so this
revision drops it there. The drop is conditional on current existence so ``head``
converges from any prior revision; the column is re-added on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "f6e5d4c3b2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _snapshot_columns() -> set[str]:
    """Return the current ``pipeline_snapshot`` column names on the live connection."""
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipeline_snapshot")}


def upgrade() -> None:
    cols = _snapshot_columns()
    if "openai_thinking_level" in cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.drop_column("openai_thinking_level")


def downgrade() -> None:
    cols = _snapshot_columns()
    if "openai_thinking_level" not in cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.add_column(sa.Column("openai_thinking_level", sa.Text(), nullable=True))
