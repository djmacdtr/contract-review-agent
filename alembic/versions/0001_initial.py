"""Create persistent task queue tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

task_type = sa.Enum("DRAFT_REVIEW", "FINAL_COMPARE", name="task_type_enum", native_enum=False, create_constraint=True)
task_status = sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="task_status_enum", native_enum=False, create_constraint=True)
task_stage = sa.Enum(
    "QUEUED", "DOWNLOADING", "PARSING", "OCR", "TEMPLATE_COMPARE", "FACT_EXTRACTION",
    "CROSS_VALIDATE", "RULE_CHECKING", "VERSION_COMPARE", "GENERATING_ADVICE",
    "PERSISTING_RESULT", "COMPLETED", name="task_stage_enum", native_enum=False, create_constraint=True,
)
conclusion = sa.Enum("PASS", "RISK_FOUND", "REVIEW_REQUIRED", "FAILED", name="conclusion_enum", native_enum=False, create_constraint=True)
file_role = sa.Enum("TARGET", "TEMPLATE", "REFERENCE", "BASELINE", name="file_role_enum", native_enum=False, create_constraint=True)
reference_type = sa.Enum("REVIEW_OPINION", "PROJECT_CONFIRMATION", "LEGAL_COMPLIANCE_REPORT", "OTHER", name="reference_type_enum", native_enum=False, create_constraint=True)
event_type = sa.Enum("CREATED", "STAGE_CHANGED", "RETRY", "COMPLETED", "FAILED", name="event_type_enum", native_enum=False, create_constraint=True)
event_stage = sa.Enum(
    "QUEUED", "DOWNLOADING", "PARSING", "OCR", "TEMPLATE_COMPARE", "FACT_EXTRACTION",
    "CROSS_VALIDATE", "RULE_CHECKING", "VERSION_COMPARE", "GENERATING_ADVICE",
    "PERSISTING_RESULT", "COMPLETED", name="event_stage_enum", native_enum=False, create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "check_task",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_type", task_type, nullable=False),
        sa.Column("client_reference_id", sa.String(128)),
        sa.Column("status", task_status, nullable=False),
        sa.Column("stage", task_stage, nullable=False),
        sa.Column("stage_message", sa.String(500)),
        sa.Column("progress", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("conclusion", conclusion),
        sa.Column("high_risk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("medium_risk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("low_risk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("info_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("source_task_id", sa.String(32), sa.ForeignKey("check_task.id", ondelete="SET NULL")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_check_task_progress"),
    )
    op.create_index("ix_check_task_status", "check_task", ["status"])
    op.create_index("ix_check_task_conclusion", "check_task", ["conclusion"])
    op.create_index("ix_check_task_status_created", "check_task", ["status", "created_at"])
    op.create_index("ix_check_task_type_created", "check_task", ["task_type", sa.text("created_at DESC")])
    op.create_index("ix_check_task_client_ref", "check_task", ["client_reference_id"])
    op.create_index("ix_check_task_heartbeat", "check_task", ["heartbeat_at"], postgresql_where=sa.text("status = 'RUNNING'"))

    op.create_table(
        "task_file",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("check_task.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", file_role, nullable=False),
        sa.Column("reference_type", reference_type),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("safe_url", sa.Text(), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("declared_mime_type", sa.String(200)),
        sa.Column("detected_mime_type", sa.String(200)),
        sa.Column("file_size", sa.BigInteger()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("page_count", sa.Integer()),
        sa.Column("parser_name", sa.String(100)),
        sa.Column("parse_status", sa.String(24)),
        sa.Column("parse_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_file_task_id", "task_file", ["task_id"])
    op.create_index("ix_task_file_sha256", "task_file", ["sha256"])
    op.create_index("ix_task_file_task_role", "task_file", ["task_id", "role", "sort_order"])

    op.create_table(
        "task_result",
        sa.Column("task_id", sa.String(32), sa.ForeignKey("check_task.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_size", sa.Integer(), nullable=False),
        sa.Column("rules_version", sa.String(64)),
        sa.Column("workflow_version", sa.String(64)),
        sa.Column("model_name", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "task_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("check_task.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", event_type, nullable=False),
        sa.Column("stage", event_stage, nullable=False),
        sa.Column("progress", sa.SmallInteger(), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_event_task_id", "task_event", ["task_id"])
    op.create_index("ix_task_event_task_created", "task_event", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_table("task_event")
    op.drop_table("task_result")
    op.drop_table("task_file")
    op.drop_table("check_task")
