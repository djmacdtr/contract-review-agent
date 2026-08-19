import asyncio
import socket
from contextlib import suppress

import structlog

from app.core.config import Settings
from app.core.enums import TaskStage
from app.core.errors import WorkflowError
from app.db.repositories.task_repository import TaskRepository
from app.db.session import SessionFactory
from app.workflows.router import WorkflowRouter
from app.workflows.types import WorkflowOutput

logger = structlog.get_logger(__name__)


class WorkerRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: TaskRepository | None = None,
        workflow=None,
    ) -> None:
        self.settings = settings
        self.repository = repository or TaskRepository()
        self.workflow = workflow or WorkflowRouter(settings)
        self.worker_id = f"{settings.WORKER_ID}:{socket.gethostname()}:{id(self):x}"
        self._stopping = asyncio.Event()

    async def recover_stale(self) -> tuple[list[str], list[str]]:
        async with SessionFactory() as session, session.begin():
            result = await self.repository.recover_stale(
                session, self.settings.WORKER_STALE_AFTER_SECONDS
            )
        if any(result):
            logger.warning(
                "stale_tasks_recovered", requeued_count=len(result[0]), failed_count=len(result[1])
            )
        return result

    async def claim(self):
        async with SessionFactory() as session, session.begin():
            return await self.repository.claim_next(session, self.worker_id)

    async def _progress(self, task_id: str, stage: TaskStage, progress: int, message: str) -> None:
        async with SessionFactory() as session, session.begin():
            owned = await self.repository.update_progress(
                session,
                task_id=task_id,
                worker_id=self.worker_id,
                stage=stage,
                progress=progress,
                message=message,
            )
        if not owned:
            raise RuntimeError("task ownership lost")

    async def _heartbeat_loop(self, task_id: str) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self.settings.WORKER_HEARTBEAT_INTERVAL_SECONDS)
                async with SessionFactory() as session, session.begin():
                    owned = await self.repository.heartbeat(session, task_id, self.worker_id)
                if not owned:
                    return
        except asyncio.CancelledError:
            raise

    async def process(self, task) -> bool:
        heartbeat = asyncio.create_task(self._heartbeat_loop(task.id))
        files = [
            {
                "file_id": item.id,
                "role": item.role.value,
                "reference_type": item.reference_type.value if item.reference_type else None,
                "file_name": item.file_name,
                "url": item.url,
                "safe_url": item.safe_url,
            }
            for item in task.files
        ]
        try:
            output = await self.workflow.run(
                task_id=task.id,
                task_type=task.task_type,
                files=files,
                options=task.options,
                progress_callback=lambda stage, progress, message: self._progress(
                    task.id, stage, progress, message
                ),
            )
            if isinstance(output, WorkflowOutput):
                result = output.result
                file_metadata = output.file_metadata
            else:
                result = output
                file_metadata = []
            metadata = result["metadata"]
            async with SessionFactory() as session, session.begin():
                completed = await self.repository.complete(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    result=result,
                    schema_version=self.settings.RESULT_SCHEMA_VERSION,
                    rules_version=metadata.get("rules_version", self.settings.RULES_VERSION),
                    workflow_version=metadata.get("workflow_version", self.settings.WORKFLOW_VERSION),
                    model_name=metadata.get("primary_model"),
                    file_metadata=file_metadata,
                )
            if not completed:
                logger.warning("task_completion_skipped_ownership_lost", task_id=task.id)
                return False
            logger.info("task_succeeded", task_id=task.id, task_type=task.task_type.value)
            return True
        except WorkflowError as exc:
            logger.error(
                "task_failed",
                task_id=task.id,
                task_type=task.task_type.value,
                error_code=exc.code,
            )
            async with SessionFactory() as session, session.begin():
                await self.repository.fail(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    code=exc.code,
                    message=exc.safe_message,
                )
            return False
        except Exception as exc:
            logger.error(
                "task_failed",
                task_id=task.id,
                task_type=task.task_type.value,
                error_type=type(exc).__name__,
            )
            async with SessionFactory() as session, session.begin():
                await self.repository.fail(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    code="INTERNAL_WORKFLOW_ERROR",
                    message="任务处理发生未分类错误",
                )
            return False
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def run_once(self) -> bool:
        await self.recover_stale()
        task = await self.claim()
        if not task:
            return False
        await self.process(task)
        return True

    async def run_forever(self) -> None:
        logger.info("worker_started", worker_id=self.worker_id)
        while not self._stopping.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.WORKER_POLL_INTERVAL_SECONDS
                    )
                except TimeoutError:
                    pass
        logger.info("worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stopping.set()
