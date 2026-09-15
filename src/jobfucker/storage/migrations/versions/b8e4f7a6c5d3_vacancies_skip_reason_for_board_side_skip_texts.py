"""vacancies.skip_reason for board-side skip texts

Revision ID: b8e4f7a6c5d3
Revises: b1c2d3e4f5a6
Create Date: 2026-08-31 13:25:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e4f7a6c5d3'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The board's skip text for vacancies whose apply attempt ended in a skip
    # (apply_status='skipped'). Distinct from apply_error, which stays
    # error-only. Nullable: pre-existing rows keep NULL (their reason is lost).
    op.add_column("vacancies", sa.Column("skip_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vacancies", "skip_reason")
