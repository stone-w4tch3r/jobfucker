"""vacancies: drop position uniqueness; archived marker is the lifecycle signal

Revision ID: a7b8c9d0e1f2
Revises: d1e2f3a4b5c6
Create Date: 2026-08-19 00:00:00.000000

``position_index`` is the vacancy's rank in a board listing at fetch time —
informational data, not identity. Board listings reorder between fetches
(promoted items, freshness sorting, filter changes), so the table-level
``UNIQUE(pipeline_id, position_index)`` made every re-fetch over a reordered
listing crash with IntegrityError and had to go entirely (not even a partial
"live rows" index: without eviction-on-absence two live rows may legitimately
share a last-seen rank).

Vacancy lifecycle is driven by one ground truth only: the board's explicit
archived/closed marker on the vacancy (mapped by the client onto
``Vacancy.archived``; ``VacancyRepository.upsert`` translates it into
``soft_deleted_at``). Absence from a search listing is deliberately not a
signal.

The constraint drop is conditional on current existence so ``head`` converges
from any prior revision; the downgrade restores the stricter constraint
(best-effort: it fails if rows share a position, which the stricter invariant
inherently forbids anyway).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _unique_constraint_names(table: str) -> set[str | None]:
    """Return the current named UNIQUE constraint names on ``table``."""
    inspector = sa.inspect(op.get_bind())
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table)}


def upgrade() -> None:
    # SQLite keeps UNIQUE constraints in the table DDL, so dropping one is a
    # batch (copy-and-move) table rebuild.
    if "uq_vacancies_pipeline_position" in _unique_constraint_names("vacancies"):
        with op.batch_alter_table("vacancies", schema=None) as batch_op:
            batch_op.drop_constraint("uq_vacancies_pipeline_position", type_="unique")


def downgrade() -> None:
    if "uq_vacancies_pipeline_position" not in _unique_constraint_names("vacancies"):
        with op.batch_alter_table("vacancies", schema=None) as batch_op:
            batch_op.create_unique_constraint(
                "uq_vacancies_pipeline_position", ["pipeline_id", "position_index"]
            )
