"""rename pipelines content columns (drop the misleading *_file suffix)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-06 05:45:00.000000

The two ``pipelines`` columns `openai_api_key_file` and `scoring_prompt_file`
store the loaded file CONTENTS (loaded once at init/update), not paths — the
"file" suffix is misleading (real config-side *path* inputs like
``config.openai.api_key_file`` live in :mod:`jobfucker.config` and keep their
names). This revision renames them data-preservingly:

- ``openai_api_key_file`` -> ``openai_api_key``
- ``scoring_prompt_file``  -> ``scoring_prompt``

Both keep their existing ``TEXT`` / NOT NULL constraints; only the column names
change, so no stored data is lost. The rename is guarded on current column
existence (same inspector pattern as the other guarded revisions) so re-running
or odd intermediate states stay idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pipelines_columns() -> set[str]:
    """Return the current ``pipelines`` column names on the live connection.

    Guards the renames below: each ``alter_column`` is conditional on the old
    name still being present (and the new name still absent), keeping the
    revision idempotent on re-run and safe against partially-migrated DBs.
    """
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipelines")}


def upgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "openai_api_key_file" in cols and "openai_api_key" not in cols:
            batch_op.alter_column(
                "openai_api_key_file",
                new_column_name="openai_api_key",
                existing_type=sa.Text(),
                nullable=False,
            )
        if "scoring_prompt_file" in cols and "scoring_prompt" not in cols:
            batch_op.alter_column(
                "scoring_prompt_file",
                new_column_name="scoring_prompt",
                existing_type=sa.Text(),
                nullable=False,
            )


def downgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "openai_api_key" in cols and "openai_api_key_file" not in cols:
            batch_op.alter_column(
                "openai_api_key",
                new_column_name="openai_api_key_file",
                existing_type=sa.Text(),
                nullable=False,
            )
        if "scoring_prompt" in cols and "scoring_prompt_file" not in cols:
            batch_op.alter_column(
                "scoring_prompt",
                new_column_name="scoring_prompt_file",
                existing_type=sa.Text(),
                nullable=False,
            )
