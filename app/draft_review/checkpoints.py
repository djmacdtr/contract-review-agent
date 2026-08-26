"""Internal extraction checkpoint contracts.

The in-memory implementation is deliberately small: it verifies the idempotent
write/read semantics needed by Map–Reduce without introducing a new service or
database table. A PostgreSQL-backed implementation can satisfy the same
protocol later by using the existing task-event persistence boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CheckpointStatus = Literal["SUCCEEDED", "FAILED"]


@dataclass(frozen=True)
class ExtractionCheckpoint:
    batch_id: str
    payload_digest: str
    status: CheckpointStatus
    value: dict | None = None
    error_code: str | None = None
    task_id: str | None = None
    file_sha256: str | None = None
    extraction_version: str = "structured-map-reduce-v2"
    model_name: str | None = None
    source_task_id: str | None = None
    updated_at: datetime | None = None


class ExtractionCheckpointStore(Protocol):
    async def load(
        self,
        batch_id: str,
        *,
        task_id: str | None = None,
        file_sha256: str | None = None,
        extraction_version: str | None = None,
        payload_digest: str | None = None,
        source_task_id: str | None = None,
    ) -> ExtractionCheckpoint | None: ...

    async def save(self, checkpoint: ExtractionCheckpoint) -> None: ...


class InMemoryExtractionCheckpointStore:
    """Idempotent checkpoint store for unit tests and offline recovery checks."""

    def __init__(self) -> None:
        self._records: dict[tuple[str | None, str | None, str, str], ExtractionCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def load(
        self,
        batch_id: str,
        *,
        task_id: str | None = None,
        file_sha256: str | None = None,
        extraction_version: str | None = None,
        payload_digest: str | None = None,
        source_task_id: str | None = None,
    ) -> ExtractionCheckpoint | None:
        async with self._lock:
            version = extraction_version or "structured-map-reduce-v2"
            candidates = [
                item
                for key, item in self._records.items()
                if key[2] == batch_id
                and (file_sha256 is None or key[1] == file_sha256)
                and key[3] == version
                and (
                    task_id is None
                    or key[0] == task_id
                    or (source_task_id is not None and key[0] == source_task_id)
                )
            ]
            for item in candidates:
                if payload_digest is None or item.payload_digest == payload_digest:
                    return item
            return None

    async def save(self, checkpoint: ExtractionCheckpoint) -> None:
        async with self._lock:
            key = (
                checkpoint.task_id,
                checkpoint.file_sha256,
                checkpoint.batch_id,
                checkpoint.extraction_version,
            )
            existing = self._records.get(key)
            if existing is None:
                self._records[key] = checkpoint
                return
            if existing != checkpoint:
                raise ValueError("checkpoint batch_id already has a different result")


class SqlAlchemyExtractionCheckpointStore:
    """Worker checkpoint store backed by the existing PostgreSQL session layer."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def load(
        self,
        batch_id: str,
        *,
        task_id: str | None = None,
        file_sha256: str | None = None,
        extraction_version: str | None = None,
        payload_digest: str | None = None,
        source_task_id: str | None = None,
    ) -> ExtractionCheckpoint | None:
        from app.db.models import ExtractionCheckpoint as ExtractionCheckpointRow

        version = extraction_version or "structured-map-reduce-v2"
        allowed_tasks = [item for item in (task_id, source_task_id) if item]
        async with self.session_factory() as session:
            statement = select(ExtractionCheckpointRow).where(
                ExtractionCheckpointRow.batch_id == batch_id,
                ExtractionCheckpointRow.file_sha256 == file_sha256,
                ExtractionCheckpointRow.extraction_version == version,
                ExtractionCheckpointRow.status == "SUCCEEDED",
            )
            if allowed_tasks:
                statement = statement.where(ExtractionCheckpointRow.task_id.in_(allowed_tasks))
            else:
                return None
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None or (payload_digest and row.payload_digest != payload_digest):
                return None
            return ExtractionCheckpoint(
                task_id=row.task_id,
                batch_id=row.batch_id,
                file_sha256=row.file_sha256,
                extraction_version=row.extraction_version,
                payload_digest=row.payload_digest,
                value=row.value,
                status="SUCCEEDED",
                model_name=row.model_name,
                updated_at=row.updated_at,
            )

    async def save(self, checkpoint: ExtractionCheckpoint) -> None:
        if checkpoint.status != "SUCCEEDED":
            return
        if not checkpoint.task_id or not checkpoint.file_sha256:
            raise ValueError("SQL checkpoint requires task_id and file_sha256")
        from app.db.models import ExtractionCheckpoint as ExtractionCheckpointRow

        async with self.session_factory() as session:
            statement = select(ExtractionCheckpointRow).where(
                ExtractionCheckpointRow.task_id == checkpoint.task_id,
                ExtractionCheckpointRow.file_sha256 == checkpoint.file_sha256,
                ExtractionCheckpointRow.batch_id == checkpoint.batch_id,
                ExtractionCheckpointRow.extraction_version == checkpoint.extraction_version,
            )
            existing = (await session.execute(statement)).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.payload_digest != checkpoint.payload_digest
                    or existing.value != checkpoint.value
                ):
                    raise ValueError("checkpoint batch_id already has a different result")
                return
            session.add(
                ExtractionCheckpointRow(
                    task_id=checkpoint.task_id,
                    file_sha256=checkpoint.file_sha256,
                    batch_id=checkpoint.batch_id,
                    extraction_version=checkpoint.extraction_version,
                    payload_digest=checkpoint.payload_digest,
                    value=checkpoint.value or {},
                    model_name=checkpoint.model_name,
                    status="SUCCEEDED",
                    updated_at=checkpoint.updated_at or datetime.now(UTC),
                )
            )
            await session.commit()
