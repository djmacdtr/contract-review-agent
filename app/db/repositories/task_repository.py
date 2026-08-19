from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import Conclusion, EventType, TaskStage, TaskStatus, TaskType
from app.db.models import CheckTask, TaskEvent, TaskFile, TaskResult


class TaskRepository:
    async def get(self, session: AsyncSession, task_id: str, *, with_files: bool = False) -> CheckTask | None:
        statement: Select[tuple[CheckTask]] = select(CheckTask).where(CheckTask.id == task_id)
        if with_files:
            statement = statement.options(selectinload(CheckTask.files))
        return (await session.execute(statement)).scalar_one_or_none()

    async def list_tasks(
        self,
        session: AsyncSession,
        *,
        page: int,
        page_size: int,
        task_type: TaskType | None,
        status: TaskStatus | None,
        client_reference_id: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> tuple[list[CheckTask], int]:
        filters: list[Any] = []
        if task_type:
            filters.append(CheckTask.task_type == task_type)
        if status:
            filters.append(CheckTask.status == status)
        if client_reference_id:
            filters.append(CheckTask.client_reference_id == client_reference_id)
        if created_from:
            filters.append(CheckTask.created_at >= created_from)
        if created_to:
            filters.append(CheckTask.created_at <= created_to)

        total = (await session.execute(select(func.count()).select_from(CheckTask).where(*filters))).scalar_one()
        rows = (
            await session.execute(
                select(CheckTask)
                .where(*filters)
                .order_by(CheckTask.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return list(rows), total

    async def claim_next(self, session: AsyncSession, worker_id: str) -> CheckTask | None:
        now = datetime.now(UTC)
        candidate = (
            select(CheckTask.id)
            .where(
                CheckTask.status == TaskStatus.PENDING,
                CheckTask.attempt_count < CheckTask.max_attempts,
            )
            .order_by(CheckTask.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(CheckTask)
            .where(CheckTask.id == candidate)
            .values(
                status=TaskStatus.RUNNING,
                stage=TaskStage.DOWNLOADING,
                stage_message="Worker 已领取任务，准备文件",
                progress=1,
                worker_id=worker_id,
                heartbeat_at=now,
                started_at=func.coalesce(CheckTask.started_at, now),
                attempt_count=CheckTask.attempt_count + 1,
                updated_at=now,
                error_code=None,
                error_message=None,
                error_details=None,
            )
            .returning(CheckTask.id)
        )
        claimed_id = (await session.execute(statement)).scalar_one_or_none()
        if not claimed_id:
            return None
        session.add(
            TaskEvent(
                task_id=claimed_id,
                event_type=EventType.STAGE_CHANGED,
                stage=TaskStage.DOWNLOADING,
                progress=1,
                message="Worker 已领取任务",
            )
        )
        await session.flush()
        return await self.get(session, claimed_id, with_files=True)

    async def update_progress(
        self,
        session: AsyncSession,
        *,
        task_id: str,
        worker_id: str,
        stage: TaskStage,
        progress: int,
        message: str,
    ) -> bool:
        now = datetime.now(UTC)
        changed = (
            await session.execute(
                update(CheckTask)
                .where(
                    CheckTask.id == task_id,
                    CheckTask.status == TaskStatus.RUNNING,
                    CheckTask.worker_id == worker_id,
                )
                .values(
                    stage=stage,
                    stage_message=message,
                    progress=progress,
                    heartbeat_at=now,
                    updated_at=now,
                )
                .returning(CheckTask.id)
            )
        ).scalar_one_or_none()
        if changed:
            session.add(
                TaskEvent(
                    task_id=task_id,
                    event_type=EventType.STAGE_CHANGED,
                    stage=stage,
                    progress=progress,
                    message=message,
                )
            )
        return bool(changed)

    async def heartbeat(self, session: AsyncSession, task_id: str, worker_id: str) -> bool:
        now = datetime.now(UTC)
        changed = (
            await session.execute(
                update(CheckTask)
                .where(
                    CheckTask.id == task_id,
                    CheckTask.status == TaskStatus.RUNNING,
                    CheckTask.worker_id == worker_id,
                )
                .values(heartbeat_at=now, updated_at=now)
                .returning(CheckTask.id)
            )
        ).scalar_one_or_none()
        return bool(changed)

    async def complete(
        self,
        session: AsyncSession,
        *,
        task_id: str,
        worker_id: str,
        result: dict[str, Any],
        schema_version: str,
        rules_version: str,
        workflow_version: str,
        model_name: str | None,
        file_metadata: list[dict[str, Any]] | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        stats = result["summary"]["statistics"]
        changed = (
            await session.execute(
                update(CheckTask)
                .where(
                    CheckTask.id == task_id,
                    CheckTask.status == TaskStatus.RUNNING,
                    CheckTask.worker_id == worker_id,
                )
                .values(
                    status=TaskStatus.SUCCEEDED,
                    stage=TaskStage.COMPLETED,
                    stage_message="任务处理已完成",
                    progress=100,
                    conclusion=Conclusion(result["conclusion"]),
                    high_risk_count=stats["high"],
                    medium_risk_count=stats["medium"],
                    low_risk_count=stats["low"],
                    info_count=stats["info"],
                    heartbeat_at=now,
                    updated_at=now,
                    finished_at=now,
                )
                .returning(CheckTask.id)
            )
        ).scalar_one_or_none()
        if not changed:
            return False
        for metadata in file_metadata or []:
            await session.execute(
                update(TaskFile)
                .where(TaskFile.task_id == task_id, TaskFile.id == metadata["file_id"])
                .values(
                    detected_mime_type=metadata.get("detected_mime_type"),
                    file_size=metadata.get("file_size"),
                    sha256=metadata.get("sha256"),
                    page_count=metadata.get("page_count"),
                    parser_name=metadata.get("parser_name"),
                    parse_status=metadata.get("parse_status"),
                    parse_warnings=metadata.get("parse_warnings", []),
                )
            )
        session.add(
            TaskResult(
                task_id=task_id,
                schema_version=schema_version,
                result=result,
                result_size=len(orjson.dumps(result)),
                rules_version=rules_version,
                workflow_version=workflow_version,
                model_name=model_name,
            )
        )
        session.add(
            TaskEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                stage=TaskStage.COMPLETED,
                progress=100,
                message="任务处理完成",
            )
        )
        return True

    async def fail(
        self,
        session: AsyncSession,
        *,
        task_id: str,
        worker_id: str,
        code: str,
        message: str,
    ) -> bool:
        now = datetime.now(UTC)
        changed = (
            await session.execute(
                update(CheckTask)
                .where(
                    CheckTask.id == task_id,
                    CheckTask.status == TaskStatus.RUNNING,
                    CheckTask.worker_id == worker_id,
                )
                .values(
                    status=TaskStatus.FAILED,
                    conclusion=Conclusion.FAILED,
                    stage_message="任务处理失败",
                    error_code=code,
                    error_message=message,
                    error_details=None,
                    updated_at=now,
                    finished_at=now,
                )
                .returning(CheckTask.id)
            )
        ).scalar_one_or_none()
        if changed:
            session.add(
                TaskEvent(
                    task_id=task_id,
                    event_type=EventType.FAILED,
                    stage=TaskStage.PERSISTING_RESULT,
                    progress=0,
                    message=message,
                )
            )
        return bool(changed)

    async def recover_stale(self, session: AsyncSession, stale_after_seconds: float) -> tuple[list[str], list[str]]:
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        stale = (
            await session.execute(
                select(CheckTask)
                .where(CheckTask.status == TaskStatus.RUNNING, CheckTask.heartbeat_at < cutoff)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        requeued: list[str] = []
        failed: list[str] = []
        now = datetime.now(UTC)
        for task in stale:
            task.worker_id = None
            task.updated_at = now
            if task.attempt_count < task.max_attempts:
                task.status = TaskStatus.PENDING
                task.stage = TaskStage.QUEUED
                task.stage_message = "Worker 心跳超时，任务已重新排队"
                task.progress = 0
                requeued.append(task.id)
                event_type = EventType.RETRY
            else:
                task.status = TaskStatus.FAILED
                task.conclusion = Conclusion.FAILED
                task.error_code = "WORKER_LOST"
                task.error_message = "Worker 心跳超时且已达到最大尝试次数"
                task.finished_at = now
                failed.append(task.id)
                event_type = EventType.FAILED
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type=event_type,
                    stage=task.stage,
                    progress=task.progress,
                    message=task.stage_message or task.error_message or "Worker 恢复处理",
                )
            )
        return requeued, failed
