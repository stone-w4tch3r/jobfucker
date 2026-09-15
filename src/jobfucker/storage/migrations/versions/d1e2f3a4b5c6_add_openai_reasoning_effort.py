"""add pipeline_snapshot.openai_reasoning_effort (optional native reasoning_effort)

Revision ID: d1e2f3a4b5c6
Revises: e5f6a7b8c9d0
Create Date: 2026-08-08 00:00:00.000000

Unlike the previously dropped ``openai_thinking_level`` (a per-provider knob,
removed by ``e5f6a7b8c9d0``), this is an optional passthrough of the native
OpenAI ``reasoning_effort`` parameter, stored as a scalar next to
``openai_model`` on the config-content snapshot. The main ``openai`` section is
persisted as scalar columns, so the scalar must exist for a ``--pipeline-id``
run to reconstruct the config without silently dropping the knob. The add is
conditional on current absence so ``head`` converges from any prior revision;
the column is dropped on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _snapshot_columns() -> set[str]:
    """Return the current ``pipeline_snapshot`` column names on the live connection."""
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipeline_snapshot")}


def upgrade() -> None:
    cols = _snapshot_columns()
    if "openai_reasoning_effort" not in cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.add_column(sa.Column("openai_reasoning_effort", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = _snapshot_columns()
    if "openai_reasoning_effort" in cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.drop_column("openai_reasoning_effort")