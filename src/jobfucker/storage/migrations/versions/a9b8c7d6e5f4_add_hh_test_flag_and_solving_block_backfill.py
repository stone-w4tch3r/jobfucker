"""add vacancies.has_hh_test + pipeline_snapshot.hh_test_solving, backfill test-skipped rows

Revision ID: a9b8c7d6e5f4
Revises: d5e6f7a8b9c0
Create Date: 2026-09-12 00:00:00.000000

Two schema additions for the hh screening-test solving feature:

1. ``vacancies.has_hh_test`` — nullable 0/1 flag reported by the HH client
   from vacancy detail at fetch time, so the apply stage and ``hh-tests`` CLI
   select test-bearing vacancies without per-candidate probing. Boards without
   the notion leave it NULL.

2. ``pipeline_snapshot.hh_test_solving`` — opaque JSON of the optional
   top-level ``hh_test_solving`` config section (enabled + prompt template
   CONTENT), mirroring ``openai_captcha`` so a ``--pipeline-id`` run can
   reconstruct the section with zero file I/O.

Data migration (why a 2000+ vacancy DB is not re-fetched): rows that were
skipped by the old apply preflight with the test-skip reason
("HH vacancy {id} requires an unsupported test" — the exact, unique text written
by both the preflight and the submit-time ``test_required`` classifier) are
flagged ``has_hh_test = 1`` and their apply state is rolled back to pending
(``apply_status``/``skip_reason``/``apply_error``/``applied_snapshot_id``
cleared). Scoring, cover letter, manual flags and timestamps are untouched, so
the row becomes an ordinary pending candidate again, eligible for the new test
flow; ``score``/``cover_letter`` survive. Only the test-skip reason text matches
— ``vacancy_unavailable`` / ``external_application`` / ``already_applied`` rows
are never touched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The exact stored skip-reason text of the old test-skip paths (preflight and
# submit-time ``test_required`` classifier both wrote it verbatim).
_TEST_SKIP_TEXT = "%requires an unsupported test%"


def _vacancy_columns() -> set[str]:
    """Return the current ``vacancies`` column names on the live connection."""
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("vacancies")}


def _snapshot_columns() -> set[str]:
    """Return the current ``pipeline_snapshot`` column names on the live connection."""
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns("pipeline_snapshot")}


def upgrade() -> None:
    vacancy_cols = _vacancy_columns()
    if "has_hh_test" not in vacancy_cols:
        with op.batch_alter_table("vacancies", schema=None) as batch_op:
            batch_op.add_column(sa.Column("has_hh_test", sa.Integer(), nullable=True))

    snap_cols = _snapshot_columns()
    if "hh_test_solving" not in snap_cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.add_column(sa.Column("hh_test_solving", sa.Text(), nullable=True))

    # Backfill + rollback: previously test-skipped vacancies become pending
    # again and are flagged test-bearing, so they can be re-applied through the
    # new test flow without re-fetching the whole listing.
    op.execute(
        sa.text(
            "UPDATE vacancies SET has_hh_test = 1, apply_status = NULL, "
            "skip_reason = NULL, apply_error = NULL, applied_snapshot_id = NULL "
            "WHERE apply_status = 'skipped' AND skip_reason LIKE :text"
        ).bindparams(text=_TEST_SKIP_TEXT)
    )


def downgrade() -> None:
    vacancy_cols = _vacancy_columns()
    if "has_hh_test" in vacancy_cols:
        with op.batch_alter_table("vacancies", schema=None) as batch_op:
            batch_op.drop_column("has_hh_test")

    snap_cols = _snapshot_columns()
    if "hh_test_solving" in snap_cols:
        with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
            batch_op.drop_column("hh_test_solving")