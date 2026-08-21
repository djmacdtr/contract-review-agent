from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import EventType, FileRole, TaskStage, TaskStatus, TaskType
from app.core.errors import AppError, TaskNotFoundError
from app.core.ids import new_file_id, new_task_id
from app.db.models import CheckTask, TaskEvent, TaskFile, TaskResult
from app.db.repositories.task_repository import TaskRepository
from app.schemas.files import RemoteFile
from app.schemas.requests import DraftReviewCreate, FinalComparisonCreate
from app.schemas.tasks import (
    TaskAccepted,
    TaskDetail,
    TaskErrorView,
    TaskListData,
    TaskSummary,
)
from app.services.url_security import sanitize_url


class TaskService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        repository: TaskRepository | None = None,
    ):
        self.session = session
        self.settings = settings
        self.repository = repository or TaskRepository()

    async def create_draft(self, request: DraftReviewCreate, request_id: str) -> TaskAccepted:
        reference_count = len(request.reference_files)
        if reference_count > self.settings.MAX_REFERENCE_FILES:
            raise AppError(
                "INVALID_REQUEST",
                "辅助资料数量超过当前配置上限",
                status_code=400,
                details={
                    "max_reference_files": self.settings.MAX_REFERENCE_FILES,
                    "actual_reference_files": reference_count,
                },
            )
        files = [
            (FileRole.TARGET, request.target_file, 0),
            (FileRole.TEMPLATE, request.template_file, 1),
            *[
                (FileRole.REFERENCE, item, index + 2)
                for index, item in enumerate(request.reference_files)
            ],
        ]
        return await self._create(
            TaskType.DRAFT_REVIEW,
            request.client_reference_id,
            request.options.model_dump(mode="json"),
            files,
            request_id,
        )

    async def create_final(self, request: FinalComparisonCreate, request_id: str) -> TaskAccepted:
        files = [
            (FileRole.BASELINE, request.baseline_file, 0),
            (FileRole.TARGET, request.target_file, 1),
        ]
        return await self._create(
            TaskType.FINAL_COMPARE,
            request.client_reference_id,
            request.options.model_dump(mode="json"),
            files,
            request_id,
        )

    async def _create(
        self,
        task_type: TaskType,
        client_reference_id: str | None,
        options: dict,
        files: Iterable[tuple[FileRole, RemoteFile, int]],
        request_id: str,
        source_task_id: str | None = None,
    ) -> TaskAccepted:
        task_id = new_task_id()
        file_rows: list[TaskFile] = []
        snapshot_files: list[dict] = []
        for role, remote, order in files:
            safe_url = sanitize_url(str(remote.url))
            # Legacy callers may still send reference_type. It is deliberately
            # ignored because document classification is a system output.
            reference_type = None
            file_rows.append(
                TaskFile(
                    id=new_file_id(),
                    task_id=task_id,
                    role=role,
                    reference_type=reference_type,
                    sort_order=order,
                    url=str(remote.url),
                    safe_url=safe_url,
                    file_name=remote.file_name,
                    declared_mime_type=remote.mime_type,
                    parse_warnings=[],
                )
            )
            snapshot_files.append(
                {
                    "role": role.value,
                    "reference_type": reference_type.value if reference_type else None,
                    "safe_url": safe_url,
                    "file_name": remote.file_name,
                    "mime_type": remote.mime_type,
                    "display_name": remote.display_name,
                    "sort_order": order,
                }
            )

        task = CheckTask(
            id=task_id,
            task_type=task_type,
            client_reference_id=client_reference_id,
            status=TaskStatus.PENDING,
            stage=TaskStage.QUEUED,
            stage_message="任务已创建，等待 Worker",
            progress=0,
            options=options,
            input_snapshot={"files": snapshot_files, "options": options},
            request_id=request_id,
            source_task_id=source_task_id,
            max_attempts=self.settings.TASK_MAX_ATTEMPTS,
        )
        async with self.session.begin():
            self.session.add(task)
            self.session.add_all(file_rows)
            self.session.add(
                TaskEvent(
                    task_id=task_id,
                    event_type=EventType.RETRY if source_task_id else EventType.CREATED,
                    stage=TaskStage.QUEUED,
                    progress=0,
                    message="重试任务已创建" if source_task_id else "任务已创建",
                    details={"source_task_id": source_task_id} if source_task_id else None,
                )
            )
        await self.session.refresh(task)
        return self._accepted(task)

    async def get_detail(self, task_id: str) -> TaskDetail:
        task = await self.repository.get(self.session, task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        error = None
        if task.error_code:
            error = TaskErrorView(
                code=task.error_code,
                message=task.error_message or "任务失败",
                details=task.error_details,
            )
        return TaskDetail(
            task_id=task.id,
            task_type=task.task_type,
            client_reference_id=task.client_reference_id,
            status=task.status,
            stage=task.stage,
            stage_message=task.stage_message,
            progress=task.progress,
            attempt_count=task.attempt_count,
            created_at=task.created_at,
            started_at=task.started_at,
            updated_at=task.updated_at,
            finished_at=task.finished_at,
            error=error,
        )

    async def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        task_type: TaskType | None,
        status: TaskStatus | None,
        client_reference_id: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> TaskListData:
        rows, total = await self.repository.list_tasks(
            self.session,
            page=page,
            page_size=page_size,
            task_type=task_type,
            status=status,
            client_reference_id=client_reference_id,
            created_from=created_from,
            created_to=created_to,
        )
        return TaskListData(
            items=[
                TaskSummary(
                    task_id=row.id,
                    task_type=row.task_type,
                    client_reference_id=row.client_reference_id,
                    status=row.status,
                    progress=row.progress,
                    conclusion=row.conclusion,
                    high_risk_count=row.high_risk_count,
                    medium_risk_count=row.medium_risk_count,
                    low_risk_count=row.low_risk_count,
                    info_count=row.info_count,
                    created_at=row.created_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_result(self, task_id: str) -> dict:
        task = await self.repository.get(self.session, task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError(
                "TASK_NOT_FINISHED",
                "任务尚未成功完成，结果不可用",
                status_code=409,
                details={"task_id": task_id, "current_status": task.status.value},
            )
        result = (
            await self.session.execute(select(TaskResult).where(TaskResult.task_id == task_id))
        ).scalar_one_or_none()
        if not result:
            raise AppError("INTERNAL_ERROR", "任务结果缺失", status_code=500)
        return result.result

    async def retry(self, task_id: str, request_id: str) -> TaskAccepted:
        source = await self.repository.get(self.session, task_id, with_files=True)
        if not source:
            raise TaskNotFoundError(task_id)
        if source.status != TaskStatus.FAILED:
            raise AppError(
                "TASK_NOT_RETRYABLE",
                "仅失败任务允许重试",
                status_code=409,
                details={"task_id": task_id, "current_status": source.status.value},
            )
        files = [
            (
                file.role,
                RemoteFile(
                    url=file.url,
                    file_name=file.file_name,
                    mime_type=file.declared_mime_type,
                    reference_type=file.reference_type,
                ),
                file.sort_order,
            )
            for file in source.files
        ]
        source_type = source.task_type
        source_client_reference_id = source.client_reference_id
        source_options = dict(source.options)
        source_id = source.id
        # The read above starts SQLAlchemy's implicit transaction. End it before
        # _create opens the explicit atomic creation transaction.
        await self.session.rollback()
        return await self._create(
            source_type,
            source_client_reference_id,
            source_options,
            files,
            request_id,
            source_task_id=source_id,
        )

    @staticmethod
    def _accepted(task: CheckTask) -> TaskAccepted:
        return TaskAccepted(
            task_id=task.id,
            task_type=task.task_type,
            status=task.status,
            progress=task.progress,
            created_at=task.created_at,
            status_url=f"/api/v1/tasks/{task.id}",
            result_url=f"/api/v1/tasks/{task.id}/result",
            source_task_id=task.source_task_id,
        )
