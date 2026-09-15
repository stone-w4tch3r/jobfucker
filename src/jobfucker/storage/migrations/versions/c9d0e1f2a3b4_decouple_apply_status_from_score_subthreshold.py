"""Decouple apply_status from score sub-threshold stamping

Revision ID: c9d0e1f2a3b4
Revises: b8e4f7a6c5d3
Create Date: 2026-09-02 09:00:00.000000

The score stage used to stamp ``apply_status='skipped'`` on sub-threshold
vacancies. ``apply_status`` is now the apply stage's exclusive vocabulary
(board-side outcomes only), so score-stamped rows must be cleared back to
NULL. A ``skipped`` row that is a real board decline is kept: declines carry
either a cover letter (letters are a hard apply requirement) or a
``skip_reason`` (the board's skip text, written by every ``ApplySkipped``
outcome even under ``--allow-without-letter``). Only rows with neither were
score-stamped and are safe to reset.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8e4f7a6c5d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Score-stamped sub-threshold rows never had a cover letter NOR a
    # skip_reason (the score stage wrote neither); every apply-written decline
    # has at least one of them. Resetting exactly the former restores
    # "pending" truth: such vacancies are apply-candidates again, while real
    # board declines (including letterless ones from --allow-without-letter)
    # stay decided.
    op.execute(
        sa.text(
            "UPDATE vacancies SET apply_status = NULL "
            "WHERE apply_status = 'skipped' AND cover_letter IS NULL AND skip_reason IS NULL"
        )
    )


def downgrade() -> None:
    # Data-semantics migration, not invertible: cleared rows can no longer be
    # told apart from vacancies that were always pending. No-op downgrade.
    pass