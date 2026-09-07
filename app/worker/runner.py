import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress

import structlog
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError

from app.core.config import Settings
from app.core.enums import TaskStage, TaskStatus
from app.core.errors import WorkflowError
from app.core.reliability import LLM_TRANSPORT_ERROR_CODES
from app.db.repositories.task_repository import TaskRepository
from app.db.session import SessionFactory
from app.workflows.router import WorkflowRouter
from app.workflows.types import WorkflowOutput

logger = structlog.get_logger(__name__)


def _is_transient_db_error(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, InterfaceError, DisconnectionError)):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


def _is_transient_workflow_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if not isinstance(exc, WorkflowError):
        return False
    details = exc.details if isinstance(exc.details, dict) else {}
    code = details.get("failure_code") or details.get("underlying_failure_code") or exc.code
    if code in LLM_TRANSPORT_ERROR_CODES:
        return True
    return code in {
        "DOWNLOAD_TIMEOUT",
        "OCR_TIMEOUT",
        "OCR_SERVICE_UNAVAILABLE",
        "OCR_UPSTREAM_ERROR",
        "OCR_NETWORK_ERROR",
    }


class WorkerRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: TaskRepository | None = None,
        workflow=None,
        session_factory=None,
    ) -> None:
        self.settings = settings
        self.repository = repository or TaskRepository()
        self.workflow = workflow or WorkflowRouter(settings)
        self.session_factory = session_factory or SessionFactory
        self.worker_id = f"{settings.WORKER_ID}:{socket.gethostname()}:{id(self):x}"
        self._stopping = asyncio.Event()

    async def _db_write(
        self,
        operation: Callable[[object], Awaitable[object]],
    ) -> object:
        max_retries = max(
            0, min(int(getattr(self.settings, "DB_WRITE_RETRY_ATTEMPTS", 1)), 1)
        )
        for attempt in range(max_retries + 1):
            try:
                async with self.session_factory() as session, session.begin():
                    return await operation(session)
            except BaseException as exc:
                if (
                    attempt >= max_retries
                    or not _is_transient_db_error(exc)
                ):
                    raise
                await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
        raise RuntimeError("database write retry exhausted")

    async def recover_stale(self) -> tuple[list[str], list[str]]:
        async def operation(session):
            return await self.repository.recover_stale(
                session, self.settings.effective_worker_stale_after_seconds
            )

        result = await self._db_write(operation)
        if any(result):
            logger.warning(
                "stale_tasks_recovered", requeued_count=len(result[0]), failed_count=len(result[1])
            )
        return result

    async def claim(self):
        async def operation(session):
            return await self.repository.claim_next(session, self.worker_id)

        return await self._db_write(operation)

    async def _progress(self, task_id: str, stage: TaskStage, progress: int, message: str) -> None:
        async def operation(session):
            return await self.repository.update_progress(
                session,
                task_id=task_id,
                worker_id=self.worker_id,
                stage=stage,
                progress=progress,
                message=message,
            )
        owned = await self._db_write(operation)
        if not owned:
            raise RuntimeError("task ownership lost")

    async def _heartbeat_loop(self, task_id: str) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self.settings.WORKER_HEARTBEAT_INTERVAL_SECONDS)
                async def operation(session):
                    return await self.repository.heartbeat(session, task_id, self.worker_id)

                owned = await self._db_write(operation)
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
            workflow_options = dict(task.options or {})
            if task.source_task_id:
                workflow_options["source_task_id"] = task.source_task_id
            resume_attempts = 0
            while True:
                try:
                    output = await self.workflow.run(
                        task_id=task.id,
                        task_type=task.task_type,
                        files=files,
                        options=workflow_options,
                        progress_callback=lambda stage, progress, message: self._progress(
                            task.id, stage, progress, message
                        ),
                    )
                    break
                except BaseException as exc:
                    max_resume = max(
                        0,
                        min(
                            int(getattr(self.settings, "TASK_TRANSIENT_RESUME_ATTEMPTS", 1)),
                            1,
                        ),
                    )
                    if resume_attempts >= max_resume or not _is_transient_workflow_error(exc):
                        raise
                    resume_attempts += 1
                    logger.warning(
                        "task_transient_resume",
                        task_id=task.id,
                        resume_attempt=resume_attempts,
                    )
            if isinstance(output, WorkflowOutput):
                result = output.result
                file_metadata = output.file_metadata
            else:
                result = output
                file_metadata = []
            metadata = result["metadata"]
            async def complete_operation(session):
                return await self.repository.complete(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    result=result,
                    schema_version=result["schema_version"],
                    rules_version=metadata.get("rules_version", self.settings.RULES_VERSION),
                    workflow_version=metadata.get(
                        "workflow_version", self.settings.WORKFLOW_VERSION
                    ),
                    model_name=metadata.get("primary_model"),
                    file_metadata=file_metadata,
                )
            completed = await self._db_write(complete_operation)
            if not completed:
                async def read_task(session):
                    return await self.repository.get(session, task.id)

                persisted = await self._db_write(read_task)
                if getattr(persisted, "status", None) == TaskStatus.SUCCEEDED:
                    logger.info("task_completion_already_persisted", task_id=task.id)
                    return True
                logger.warning("task_completion_skipped_ownership_lost", task_id=task.id)
                return False
            logger.info("task_succeeded", task_id=task.id, task_type=task.task_type.value)
            return True
        except WorkflowError as exc:
            details = exc.details or {}
            failure_code = exc.code
            failure_message = exc.safe_message
            failure_details = exc.details
            logger.error(
                "task_failed",
                task_id=task.id,
                task_type=task.task_type.value,
                error_code=exc.code,
                component=details.get("component"),
                failure_kind=details.get("failure_kind"),
                attempts=details.get("attempts"),
                elapsed_ms=details.get("elapsed_ms"),
                failure_stage=details.get("failure_stage"),
                chain=details.get("chain"),
                file_id=details.get("file_id", details.get("file")),
                batch_depth=details.get("batch_depth"),
                unit_count=details.get("unit_count"),
                numeric_candidate_count=details.get("numeric_candidate_count"),
                batch_id=details.get("batch_id"),
                failure_code=details.get("failure_code"),
                underlying_failure_code=details.get("underlying_failure_code"),
                finish_reason=details.get("finish_reason"),
                content_chars=details.get("content_chars"),
                reasoning_content_chars=details.get("reasoning_content_chars"),
                max_tokens=details.get("max_tokens"),
                http_status=details.get("http_status"),
                usage=details.get("usage"),
                expected_count=details.get("expected_count"),
                returned_count=details.get("returned_count"),
                missing_index_count=details.get("missing_index_count"),
                duplicate_index_count=details.get("duplicate_index_count"),
                invalid_index_count=details.get("invalid_index_count"),
                public_evidence_file_id=details.get("public_evidence_file_id"),
                public_evidence_location=details.get("public_evidence_location"),
                required_evidence_count=details.get("required_evidence_count"),
                covered_evidence_count=details.get("covered_evidence_count"),
                missing_evidence_count=details.get("missing_evidence_count"),
            )
            async def fail_operation(session):
                return await self.repository.fail(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    code=failure_code,
                    message=failure_message,
                    details=failure_details,
                )
            await self._db_write(fail_operation)
            return False
        except Exception as exc:
            logger.error(
                "task_failed",
                task_id=task.id,
                task_type=task.task_type.value,
                error_type=type(exc).__name__,
            )
            async def fail_operation(session):
                return await self.repository.fail(
                    session,
                    task_id=task.id,
                    worker_id=self.worker_id,
                    code="INTERNAL_WORKFLOW_ERROR",
                    message="任务处理发生未分类错误",
                    details=None,
                )
            await self._db_write(fail_operation)
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
        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            llm_enabled=self.settings.LLM_ENABLED,
            llm_response_format=(
                "json_schema"
                if self.settings.LLM_NATIVE_STRUCTURED_OUTPUT
                else self.settings.LLM_RESPONSE_FORMAT
            ),
            llm_native_structured_output=self.settings.LLM_NATIVE_STRUCTURED_OUTPUT,
            llm_extraction_model=self.settings.LLM_EXTRACTION_MODEL,
            llm_advice_model=self.settings.LLM_ADVICE_MODEL,
        )
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
