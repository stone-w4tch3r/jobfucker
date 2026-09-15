"""drop config_snapshot; re-add openai_captcha (row columns are the source of truth)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-06 03:00:00.000000

Per user directive (2026-08-06): **the ``pipelines`` row columns ARE the source
of truth — not a duplicated JSON snapshot.** The ``config_snapshot`` column
(added in ``b2c3d4e5f6a7``) is rejected as needless duplication, so this revision
removes it. The loaded file CONTENTS already live in the row columns
(``login``/``password``/``resume``/``openai_api_key_file``/
``scoring_prompt_file``/``reply_prompt`` — semantics now "contents", not paths),
and ``service_section`` holds the opaque board section JSON.

The AI-captcha block must be recoverable from the row too, so ``openai_captcha``
(nullable JSON) is re-added: it holds the optional captcha section
(model/thinking_level/base_url/api_key content) and is ``None`` when unset.

This revision:
- drops ``config_snapshot`` (Text, nullable) from ``pipelines``;
- adds ``openai_captcha`` (Text, nullable, no server default).

``service_section`` is kept unchanged (opaque board section, re-validated
through the registry).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pipelines_columns() -> set[str]:
    """Return the current ``pipelines`` column names on the live connection.

    Guards the transient mutations below: existing DBs may arrive at this
    revision with a partially-migrated ``pipelines`` table (e.g. the real
    intermediate state that never carried ``openai_captcha``), so each add/drop
    is conditional on current existence. This makes the chain idempotent and
    lets ``head`` converge on the target schema from any prior revision.
    """
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipelines")}


def upgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "config_snapshot" in cols:
            batch_op.drop_column("config_snapshot")
        if "openai_captcha" not in cols:
            batch_op.add_column(sa.Column("openai_captcha", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = _pipelines_columns()
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        if "openai_captcha" in cols:
            batch_op.drop_column("openai_captcha")
        if "config_snapshot" not in cols:
            batch_op.add_column(sa.Column("config_snapshot", sa.Text(), nullable=True))
