"""add service_section + openai_captcha to pipelines

Revision ID: a1b2c3d4e5f6
Revises: 65382efa98e6
Create Date: 2026-08-06 01:00:00.000000

Adds the opaque ``service_section`` JSON column and the opaque
``openai_captcha`` JSON column to ``pipelines`` so a stored pipeline can rebuild
its engine by ``--pipeline-id`` (re-validate the ``service.<board>`` section and
the AI-captcha section + re-read referenced-file contents) without a fresh
``pipeline.yaml``. ``openai_captcha`` persists paths only (never the ``api_key``
contents). Both are nullable and without a server default: rows stored before
this migration have no sections and are non-runnable until re-``init``-ed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "65382efa98e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        batch_op.add_column(sa.Column("service_section", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("openai_captcha", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pipelines", schema=None) as batch_op:
        batch_op.drop_column("openai_captcha")
        batch_op.drop_column("service_section")
