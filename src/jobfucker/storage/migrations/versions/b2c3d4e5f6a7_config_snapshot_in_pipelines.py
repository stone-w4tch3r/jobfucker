"""store config snapshot in pipelines; drop openai_captcha

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-06 02:00:00.000000

Per user directive (2026-08-06): **store file contents in the DB, not
references.** The ``pipelines`` row now carries a full loaded-config snapshot
(``config.model_dump(mode="json")``) with all referenced-file CONTENTS embedded
(resume, scoring/reply prompts, openai api_key, the ``openai_captcha`` section
including its key, auth login/password). A ``--pipeline-id`` stage run
reconstructs from the snapshot with zero file I/O.

This revision:

- adds ``config_snapshot`` (Text, nullable, no server default) to ``pipelines``;
- drops ``openai_captcha`` (Text) — it is now redundant because the
  ``openai_captcha`` section is embedded (with its key) inside ``config_snapshot``.

``service_section`` is kept: the board-section Protocol is not part of
``model_dump``, so it stays a separate opaque column re-validated through the
board registry. The other path/credential columns (``login``/``resume``/...) are
kept as vestigial NOT NULL metadata and are no longer read at rebuild.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pipelines_columns() -> set[str]:
    """Return the current ``pipelines`` column names on the live connection.

    Guards the transient mutations below: the development chain mutated the
    ``openai_captcha`` column add/drop/re-add across revisions, and some existing
    DBs were stamped at ``a1b2`` with only ``service_section`` (the prototype-era
    ``a1b2`` never added ``openai_captcha``). Mutating an absent column raises
    ``KeyError`` and crashes every startup, so each add/drop is made conditional
    on current existence. This keeps fresh DBs correct *and* unsticks existing
    intermediate DBs without requiring the user to delete state.
    """
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipelines")}


def upgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "config_snapshot" not in cols:
            batch_op.add_column(sa.Column("config_snapshot", sa.Text(), nullable=True))
        if "openai_captcha" in cols:
            batch_op.drop_column("openai_captcha")


def downgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "openai_captcha" not in cols:
            batch_op.add_column(sa.Column("openai_captcha", sa.Text(), nullable=True))
        if "config_snapshot" in cols:
            batch_op.drop_column("config_snapshot")
