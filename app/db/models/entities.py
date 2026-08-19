from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    Conclusion,
    EventType,
    FileRole,
    ReferenceType,
    TaskStage,
    TaskStatus,
    TaskType,
)
from app.db.base import Base


def string_enum(enum_type: type, name: str, length: int) -> Enum:
    # Let SQLAlchemy derive the VARCHAR width from the longest stable value.
    # The unused argument keeps call sites visually aligned with the design widths.
    return Enum(enum_type, name=name, native_enum=False, create_constraint=True)


class CheckTask(Base):
    __tablename__ = "check_task"
    __table_args__ = (
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_check_task_progress"),
        Index("ix_check_task_status_created", "status", "created_at"),
        Index("ix_check_task_type_created", "task_type", text("created_at DESC")),
        Index("ix_check_task_client_ref", "client_reference_id"),
        Index("ix_check_task_heartbeat", "heartbeat_at", postgresql_where=text("status = 'RUNNING'")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_type: Mapped[TaskType] = mapped_column(
        string_enum(TaskType, "task_type_enum", 32), nullable=False
    )
    client_reference_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[TaskStatus] = mapped_column(
        string_enum(TaskStatus, "task_status_enum", 24), nullable=False, index=True
    )
    stage: Mapped[TaskStage] = mapped_column(
        string_enum(TaskStage, "task_stage_enum", 32), nullable=False
    )
    stage_message: Mapped[str | None] = mapped_column(String(500))
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    conclusion: Mapped[Conclusion | None] = mapped_column(
        string_enum(Conclusion, "conclusion_enum", 24), index=True
    )
    high_risk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medium_risk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_risk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    info_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    source_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("check_task.id", ondelete="SET NULL")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    files: Mapped[list["TaskFile"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskFile.sort_order"
    )
    result: Mapped["TaskResult | None"] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskFile(Base):
    __tablename__ = "task_file"
    __table_args__ = (Index("ix_task_file_task_role", "task_id", "role", "sort_order"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("check_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[FileRole] = mapped_column(
        string_enum(FileRole, "file_role_enum", 32), nullable=False
    )
    reference_type: Mapped[ReferenceType | None] = mapped_column(
        string_enum(ReferenceType, "reference_type_enum", 40)
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    safe_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    declared_mime_type: Mapped[str | None] = mapped_column(String(200))
    detected_mime_type: Mapped[str | None] = mapped_column(String(200))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    parser_name: Mapped[str | None] = mapped_column(String(100))
    parse_status: Mapped[str | None] = mapped_column(String(24))
    parse_warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task: Mapped[CheckTask] = relationship(back_populates="files")


class TaskResult(Base):
    __tablename__ = "task_result"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("check_task.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_size: Mapped[int] = mapped_column(Integer, nullable=False)
    rules_version: Mapped[str | None] = mapped_column(String(64))
    workflow_version: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task: Mapped[CheckTask] = relationship(back_populates="result")


class TaskEvent(Base):
    __tablename__ = "task_event"
    __table_args__ = (Index("ix_task_event_task_created", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("check_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[EventType] = mapped_column(
        string_enum(EventType, "event_type_enum", 32), nullable=False
    )
    stage: Mapped[TaskStage] = mapped_column(
        string_enum(TaskStage, "event_stage_enum", 32), nullable=False
    )
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task: Mapped[CheckTask] = relationship(back_populates="events")
