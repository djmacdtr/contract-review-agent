"""Local, short-lived storage for files uploaded by the web console.

The task APIs deliberately continue to accept remote file descriptions.  This
store only gives the console a safe way to turn a browser upload into one of
those descriptions; the worker still consumes the resulting URL through the
normal downloader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.ids import new_id

UPLOAD_CHUNK_SIZE = 1024 * 1024
_UPLOAD_ID_PATTERN = re.compile(r"^upl_[A-Za-z0-9]+$")
_ALLOWED_EXTENSIONS = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class ConsoleUploadError(Exception):
    """Safe, user-facing upload failure without filesystem details."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StoredUpload:
    upload_id: str
    url: str
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str


class ConsoleUploadStore:
    """Persist uploads using an ID-derived filename and a small metadata sidecar."""

    def __init__(
        self,
        root: str | Path,
        base_url: str,
        max_size_bytes: int,
        retention_days: int = 7,
    ) -> None:
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.max_size_bytes = max_size_bytes
        self.retention_seconds = max(1, retention_days) * 24 * 60 * 60

    @classmethod
    def from_settings(cls, settings: Any) -> ConsoleUploadStore:
        return cls(
            root=settings.UPLOAD_ROOT,
            base_url=settings.CONSOLE_UPLOAD_BASE_URL,
            max_size_bytes=max(1, int(settings.MAX_FILE_SIZE_MB * 1024 * 1024)),
            retention_days=settings.CONSOLE_UPLOAD_RETENTION_DAYS,
        )

    async def save(self, upload: Any) -> StoredUpload:
        file_name = self._validate_file_name(getattr(upload, "filename", None))
        extension = Path(file_name).suffix.casefold()
        mime_type = _ALLOWED_EXTENSIONS[extension]
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.cleanup_expired()
        except OSError as exc:
            raise ConsoleUploadError("UPLOAD_STORAGE_ERROR", "文件暂时无法保存") from exc

        upload_id = new_id("upl")
        part_path = self.root / f".{upload_id}.part"
        data_path = self.root / f"{upload_id}{extension}"
        metadata_path = self.root / f".{upload_id}.json.part"
        final_metadata_path = self.root / f"{upload_id}.json"

        size_bytes = 0
        digest = hashlib.sha256()
        header = bytearray()
        try:
            with part_path.open("wb") as destination:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.max_size_bytes:
                        raise ConsoleUploadError(
                            "UPLOAD_TOO_LARGE", "文件大小超过允许上限"
                        )
                    destination.write(chunk)
                    digest.update(chunk)
                    if len(header) < 8:
                        header.extend(chunk[: 8 - len(header)])

            if size_bytes == 0:
                raise ConsoleUploadError("UPLOAD_EMPTY", "不能上传空文件")
            if not self._signature_matches(extension, bytes(header)):
                raise ConsoleUploadError("UPLOAD_SIGNATURE_MISMATCH", "文件内容与扩展名不匹配")

            os.replace(part_path, data_path)
            metadata = {
                "upload_id": upload_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": digest.hexdigest(),
                "extension": extension,
                "created_at": time.time(),
            }
            with metadata_path.open("w", encoding="utf-8") as metadata_file:
                json.dump(metadata, metadata_file, ensure_ascii=False, separators=(",", ":"))
            os.replace(metadata_path, final_metadata_path)
            return StoredUpload(
                upload_id=upload_id,
                url=f"{self.base_url}/api/v1/console/uploads/{upload_id}",
                file_name=file_name,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
            )
        except ConsoleUploadError:
            self._remove_paths(part_path, metadata_path, data_path, final_metadata_path)
            raise
        except Exception as exc:
            self._remove_paths(part_path, metadata_path, data_path, final_metadata_path)
            raise ConsoleUploadError("UPLOAD_STORAGE_ERROR", "文件暂时无法保存") from exc

    def resolve(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise ConsoleUploadError("UPLOAD_NOT_FOUND", "上传文件不存在")
        metadata_path = self.root / f"{upload_id}.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            extension = metadata["extension"]
            if (
                metadata.get("upload_id") != upload_id
                or extension not in _ALLOWED_EXTENSIONS
                or metadata.get("mime_type") != _ALLOWED_EXTENSIONS[extension]
            ):
                raise ValueError("invalid metadata")
            data_path = self.root / f"{upload_id}{extension}"
            if not data_path.is_file():
                raise FileNotFoundError(data_path)
            return data_path, metadata
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConsoleUploadError("UPLOAD_NOT_FOUND", "上传文件不存在") from exc

    def cleanup_expired(self) -> int:
        if not self.root.exists():
            return 0
        cutoff = time.time() - self.retention_seconds
        removed = 0
        for metadata_path in self.root.glob("upl_*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                upload_id = metadata["upload_id"]
                extension = metadata["extension"]
                created_at = float(metadata["created_at"])
                if (
                    not _UPLOAD_ID_PATTERN.fullmatch(upload_id)
                    or extension not in _ALLOWED_EXTENSIONS
                    or created_at >= cutoff
                ):
                    continue
                data_path = self.root / f"{upload_id}{extension}"
                metadata_path.unlink(missing_ok=True)
                data_path.unlink(missing_ok=True)
                removed += 1
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        for part_path in self.root.glob(".*.part"):
            try:
                if part_path.stat().st_mtime < cutoff:
                    part_path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _validate_file_name(file_name: str | None) -> str:
        if not file_name or not isinstance(file_name, str):
            raise ConsoleUploadError("UPLOAD_INVALID_FILENAME", "文件名不合法")
        if (
            file_name in {".", ".."}
            or "/" in file_name
            or "\\" in file_name
            or _CONTROL_CHARACTERS.search(file_name)
            or len(file_name) > 500
            or Path(file_name).name != file_name
        ):
            raise ConsoleUploadError("UPLOAD_INVALID_FILENAME", "文件名不合法")
        extension = Path(file_name).suffix.casefold()
        if extension not in _ALLOWED_EXTENSIONS:
            raise ConsoleUploadError("UPLOAD_UNSUPPORTED_TYPE", "仅支持 DOCX 或 PDF 文件")
        return file_name

    @staticmethod
    def _signature_matches(extension: str, header: bytes) -> bool:
        if extension == ".pdf":
            return header.startswith(b"%PDF-")
        return header.startswith(b"PK\x03\x04")

    @staticmethod
    def _remove_paths(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
