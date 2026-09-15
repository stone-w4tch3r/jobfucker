"""noop migration (non-destructive chain probe)

Revision ID: 65382efa98e6
Revises: ef930323cb4b
Create Date: 2026-08-05 09:28:56.554563

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65382efa98e6'
down_revision: str | None = 'ef930323cb4b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
