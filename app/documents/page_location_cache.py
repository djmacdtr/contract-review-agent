"""Persistent content-addressed DOCX page-location sidecar cache."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ExtractionCheckpoint
from app.documents.page_locations import (
    PAGE_LOCATION_CACHE_OWNER,
    PAGE_LOCATION_CACHE_VERSION,
    DocxPageLocationSidecar,
    deserialize_docx_page_location_sidecar,
    page_location_cache_identity,
    serialize_docx_page_location_sidecar,
    validate_docx_page_location_sidecar,
)


class PageLocationSidecarCache(Protocol):
    async def load(
        self, *, file_sha256: str, file_id: str
    ) -> DocxPageLocationSidecar | None: ...

    async def save(
        self, *, file_sha256: str, sidecar: DocxPageLocationSidecar
    ) -> None: ...


class InMemoryPageLocationSidecarCache:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    async def load(
        self, *, file_sha256: str, file_id: str
    ) -> DocxPageLocationSidecar | None:
        value = self.records.get(file_sha256)
        if value is None:
            return None
        try:
            sidecar = deserialize_docx_page_location_sidecar(value, file_id=file_id)
            validate_docx_page_location_sidecar(sidecar, file_id=file_id)
        except (TypeError, ValueError):
            return None
        return sidecar

    async def save(
        self, *, file_sha256: str, sidecar: DocxPageLocationSidecar
    ) -> None:
        self.records[file_sha256] = serialize_docx_page_location_sidecar(sidecar)


class SqlAlchemyPageLocationSidecarCache:
    """Use the existing checkpoint table without coupling to a task ID."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def load(
        self, *, file_sha256: str, file_id: str
    ) -> DocxPageLocationSidecar | None:
        batch_id, payload_digest = page_location_cache_identity(file_sha256)
        async with self.session_factory() as session:
            value = (
                await session.execute(
                    select(ExtractionCheckpoint.value).where(
                        ExtractionCheckpoint.task_id == PAGE_LOCATION_CACHE_OWNER,
                        ExtractionCheckpoint.file_sha256 == file_sha256,
                        ExtractionCheckpoint.batch_id == batch_id,
                        ExtractionCheckpoint.extraction_version
                        == PAGE_LOCATION_CACHE_VERSION,
                        ExtractionCheckpoint.payload_digest == payload_digest,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                )
            ).scalar_one_or_none()
        if not isinstance(value, dict):
            return None
        try:
            sidecar = deserialize_docx_page_location_sidecar(value, file_id=file_id)
            validate_docx_page_location_sidecar(sidecar, file_id=file_id)
        except (TypeError, ValueError):
            return None
        return sidecar

    async def save(
        self, *, file_sha256: str, sidecar: DocxPageLocationSidecar
    ) -> None:
        batch_id, payload_digest = page_location_cache_identity(file_sha256)
        now = datetime.now(UTC)
        statement = insert(ExtractionCheckpoint).values(
            task_id=PAGE_LOCATION_CACHE_OWNER,
            file_sha256=file_sha256,
            batch_id=batch_id,
            extraction_version=PAGE_LOCATION_CACHE_VERSION,
            payload_digest=payload_digest,
            value=serialize_docx_page_location_sidecar(sidecar),
            model_name="DOCX_PAGE_LOCATION",
            status="SUCCEEDED",
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                ExtractionCheckpoint.task_id,
                ExtractionCheckpoint.file_sha256,
                ExtractionCheckpoint.batch_id,
                ExtractionCheckpoint.extraction_version,
            ],
            set_={
                "payload_digest": payload_digest,
                "value": serialize_docx_page_location_sidecar(sidecar),
                "model_name": "DOCX_PAGE_LOCATION",
                "status": "SUCCEEDED",
                "updated_at": now,
            },
        )
        async with self.session_factory() as session:
            await session.execute(statement)
            await session.commit()
