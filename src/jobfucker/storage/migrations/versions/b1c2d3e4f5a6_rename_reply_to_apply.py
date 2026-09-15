"""rename reply vocabulary to apply

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-08-30 00:00:00.000000

The product now uses application vocabulary at every public and persisted
boundary. Rename the snapshot prompt and vacancy result columns without losing
data, convert the successful terminal status, and update existing audit rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename persisted reply fields and values to application vocabulary."""
    with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
        batch_op.alter_column(
            "reply_prompt",
            new_column_name="apply_prompt",
            existing_type=sa.Text(),
            nullable=False,
        )

    # The old status check must be absent while the successful value changes.
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_vacancies_reply_status"), type_="check")
        batch_op.drop_constraint(
            op.f("fk_vacancies_replied_snapshot_id_pipeline_snapshot"),
            type_="foreignkey",
        )
        batch_op.drop_index("ix_vacancies_replied_snapshot_id")
        batch_op.alter_column(
            "reply_status",
            new_column_name="apply_status",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "reply_error",
            new_column_name="apply_error",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "replied_at",
            new_column_name="applied_at",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "replied_snapshot_id",
            new_column_name="applied_snapshot_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
    op.execute(
        sa.text(
            "UPDATE vacancies SET apply_status='applied' "
            "WHERE apply_status='replied'"
        )
    )
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_vacancies_applied_snapshot_id_pipeline_snapshot"),
            "pipeline_snapshot",
            ["applied_snapshot_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_vacancies_applied_snapshot_id",
            ["applied_snapshot_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_vacancies_apply_status"),
            "apply_status IN ('pending','applied','skipped','error')",
        )

    op.execute(sa.text("UPDATE audit_log SET action='apply' WHERE action='reply'"))
    op.execute(
        sa.text(
            "UPDATE audit_log SET details=replace(details, '\"replied\":', '\"applied\":') "
            "WHERE action='apply' AND details LIKE '%\"replied\":%'"
        )
    )


def downgrade() -> None:
    """Restore the former persisted reply fields and values."""
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_vacancies_apply_status"), type_="check")
        batch_op.drop_constraint(
            op.f("fk_vacancies_applied_snapshot_id_pipeline_snapshot"),
            type_="foreignkey",
        )
        batch_op.drop_index("ix_vacancies_applied_snapshot_id")
        batch_op.alter_column(
            "apply_status",
            new_column_name="reply_status",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "apply_error",
            new_column_name="reply_error",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "applied_at",
            new_column_name="replied_at",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "applied_snapshot_id",
            new_column_name="replied_snapshot_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
    op.execute(
        sa.text(
            "UPDATE vacancies SET reply_status='replied' "
            "WHERE reply_status='applied'"
        )
    )
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_vacancies_replied_snapshot_id_pipeline_snapshot"),
            "pipeline_snapshot",
            ["replied_snapshot_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_vacancies_replied_snapshot_id",
            ["replied_snapshot_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_vacancies_reply_status"),
            "reply_status IN ('pending','replied','skipped','error')",
        )

    with op.batch_alter_table("pipeline_snapshot", schema=None) as batch_op:
        batch_op.alter_column(
            "apply_prompt",
            new_column_name="reply_prompt",
            existing_type=sa.Text(),
            nullable=False,
        )

    op.execute(sa.text("UPDATE audit_log SET action='reply' WHERE action='apply'"))
    op.execute(
        sa.text(
            "UPDATE audit_log SET details=replace(details, '\"applied\":', '\"replied\":') "
            "WHERE action='reply' AND details LIKE '%\"applied\":%'"
        )
    )
