from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.document_parser.base import ExternalDocumentParser, ParseMode
from app.core.config import Settings
from app.db.models import ExtractionCheckpoint
from app.documents.models import ParsedDocument
from app.services.downloader import LocalFile

logger = structlog.get_logger(__name__)

OCR_CACHE_OWNER = "sys_ocr_cache_v1"
OCR_CACHE_VERSION = "ocr-parsed-document-v1"


class DocumentParseCache(Protocol):
    async def load(self, *, file_sha256: str, cache_key: str) -> dict | None: ...

    async def save(
        self, *, file_sha256: str, cache_key: str, value: dict
    ) -> None: ...


class InMemoryDocumentParseCache:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}

    async def load(self, *, file_sha256: str, cache_key: str) -> dict | None:
        return self.records.get((file_sha256, cache_key))

    async def save(self, *, file_sha256: str, cache_key: str, value: dict) -> None:
        self.records[(file_sha256, cache_key)] = value


class SqlAlchemyDocumentParseCache:
    """Persistent OCR cache using the existing content-addressed checkpoint table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _identity(cache_key: str) -> tuple[str, str]:
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return f"ocr_{digest[:32]}", digest

    async def load(self, *, file_sha256: str, cache_key: str) -> dict | None:
        batch_id, digest = self._identity(cache_key)
        async with self.session_factory() as session:
            value = (
                await session.execute(
                    select(ExtractionCheckpoint.value).where(
                        ExtractionCheckpoint.task_id == OCR_CACHE_OWNER,
                        ExtractionCheckpoint.file_sha256 == file_sha256,
                        ExtractionCheckpoint.batch_id == batch_id,
                        ExtractionCheckpoint.extraction_version == OCR_CACHE_VERSION,
                        ExtractionCheckpoint.payload_digest == digest,
                        ExtractionCheckpoint.status == "SUCCEEDED",
                    )
                )
            ).scalar_one_or_none()
        return value if isinstance(value, dict) else None

    async def save(self, *, file_sha256: str, cache_key: str, value: dict) -> None:
        batch_id, digest = self._identity(cache_key)
        now = datetime.now(UTC)
        statement = insert(ExtractionCheckpoint).values(
            task_id=OCR_CACHE_OWNER,
            file_sha256=file_sha256,
            batch_id=batch_id,
            extraction_version=OCR_CACHE_VERSION,
            payload_digest=digest,
            value=value,
            model_name="TextIn",
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
                "payload_digest": digest,
                "value": value,
                "model_name": "TextIn",
                "status": "SUCCEEDED",
                "updated_at": now,
            },
        )
        async with self.session_factory() as session:
            await session.execute(statement)
            await session.commit()


class CachedExternalDocumentParser:
    """Cache validated normalized OCR output and rebind it to the current task file."""

    def __init__(
        self,
        inner: ExternalDocumentParser,
        cache: DocumentParseCache,
        settings: Settings,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.settings = settings

    def _cache_key(self, *, mode: ParseMode, include_stamp_images: bool) -> str:
        identity = {
            "version": OCR_CACHE_VERSION,
            "provider": self.settings.OCR_BASE_URL.strip().rstrip("/"),
            "mode": mode,
            "include_stamp_images": include_stamp_images,
            "low_confidence": self.settings.OCR_LOW_CONFIDENCE_THRESHOLD,
            "stamp_max_count": self.settings.OCR_STAMP_MAX_COUNT,
            "stamp_max_image_bytes": self.settings.OCR_STAMP_MAX_IMAGE_BYTES,
            "stamp_max_total_bytes": self.settings.OCR_STAMP_MAX_TOTAL_BYTES,
        }
        return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _rebind(document: ParsedDocument, file: LocalFile) -> ParsedDocument:
        warnings = [
            warning.model_copy(
                update={"file_id": file.file_id if warning.file_id is not None else None}
            )
            for warning in document.warnings
        ]
        return document.model_copy(
            update={
                "file_id": file.file_id,
                "role": file.role,
                "file_name": file.file_name,
                "sha256": file.sha256,
                "warnings": warnings,
            }
        )

    async def _parse(
        self,
        file: LocalFile,
        *,
        mode: ParseMode,
        include_stamp_images: bool,
    ) -> ParsedDocument:
        cache_key = self._cache_key(
            mode=mode, include_stamp_images=include_stamp_images
        )
        try:
            cached = await self.cache.load(
                file_sha256=file.sha256, cache_key=cache_key
            )
            if cached is not None:
                document = ParsedDocument.model_validate(cached.get("document"))
                return self._rebind(document, file)
        except Exception as exc:
            logger.warning("ocr_cache_read_failed", error_type=type(exc).__name__)

        stamp_parser = getattr(self.inner, "parse_with_stamp_images", None)
        if include_stamp_images and stamp_parser is not None:
            document = await stamp_parser(file, mode=mode)
        else:
            document = await self.inner.parse(file, mode=mode)
        try:
            await self.cache.save(
                file_sha256=file.sha256,
                cache_key=cache_key,
                value={"document": document.model_dump(mode="json")},
            )
        except Exception as exc:
            logger.warning("ocr_cache_write_failed", error_type=type(exc).__name__)
        return document

    async def parse(self, file: LocalFile, *, mode: ParseMode) -> ParsedDocument:
        return await self._parse(file, mode=mode, include_stamp_images=False)

    async def parse_with_stamp_images(
        self, file: LocalFile, *, mode: ParseMode
    ) -> ParsedDocument:
        return await self._parse(file, mode=mode, include_stamp_images=True)
