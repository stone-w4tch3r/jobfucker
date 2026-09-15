"""vacancies: timestamp-based staleness; drop position_index and archived

Revision ID: a0b1c2d3e4f5
Revises: a7b8c9d0e1f2
Create Date: 2026-08-19 00:00:00.000000

Fetch is now an insert-only mirror sync (``--refresh`` opt-in overwrites listing
data only), so ``position_index`` — a last-seen listing rank that never was
identity — is dropped entirely: ordering everywhere is by ``id``. The board's
``archived`` marker is dropped too: ``soft_deleted_at`` is the **only**
lifecycle signal and is set manually only, never by fetch.

Staleness is computed from timestamps, not boolean flags:

- ``fetched_at`` — when the listing row was last fetched/refreshed;
- ``scored_at`` / ``generated_at`` — when the score / cover-letter artifact
  was last written by its stage;
- ``user_edited_at`` — when a manual mutation touched the row.

NULL-safe read-side rules: ``score_stale = fetched_at > scored_at``,
``letter_stale = fetched_at > generated_at``,
``user_stale = fetched_at > user_edited_at`` (any NULL → not stale).

Backfill (best-effort, then never re-run): ``fetched_at = created_at``;
``scored_at = updated_at`` where a score exists; ``generated_at = updated_at``
where a cover letter exists. ``user_edited_at`` is not recoverable from legacy
rows and stays NULL (approximation acknowledged in the design).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.drop_column("position_index")
        batch_op.drop_column("archived")
        batch_op.add_column(sa.Column("fetched_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("scored_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("generated_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("user_edited_at", sa.Text(), nullable=True))

    # Backfill the four timestamps from the best available legacy signals.
    op.execute(
        sa.text(
            "UPDATE vacancies SET fetched_at = created_at WHERE fetched_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE vacancies SET scored_at = updated_at "
            "WHERE score IS NOT NULL AND scored_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE vacancies SET generated_at = updated_at "
            "WHERE cover_letter IS NOT NULL AND generated_at IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("position_index", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("archived", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.drop_column("fetched_at")
        batch_op.drop_column("scored_at")
        batch_op.drop_column("generated_at")
        batch_op.drop_column("user_edited_at")
