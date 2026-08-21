"""Add ungraded risk and review counters.

Revision ID: 0002_ungraded_risk_counts
Revises: 0001_initial
Create Date: 2026-08-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_ungraded_risk_counts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "check_task",
        sa.Column("risk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "check_task",
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        """
        UPDATE check_task
        SET risk_count = high_risk_count + medium_risk_count,
            review_count = low_risk_count + info_count
        """
    )


def downgrade() -> None:
    op.drop_column("check_task", "review_count")
    op.drop_column("check_task", "risk_count")
