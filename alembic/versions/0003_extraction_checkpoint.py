"""Add internal structured extraction checkpoints.

Revision ID: 0003_extraction_checkpoint
Revises: 0002_ungraded_risk_counts
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_extraction_checkpoint"
down_revision = "0002_ungraded_risk_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extraction_checkpoint",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("extraction_version", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("task_id", "file_sha256", "batch_id", "extraction_version"),
        sa.UniqueConstraint(
            "task_id",
            "file_sha256",
            "batch_id",
            "extraction_version",
            name="uq_extraction_checkpoint_identity",
        ),
    )
    op.create_index(
        "ix_extraction_checkpoint_source",
        "extraction_checkpoint",
        ["task_id", "file_sha256", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_checkpoint_source", table_name="extraction_checkpoint")
    op.drop_table("extraction_checkpoint")
