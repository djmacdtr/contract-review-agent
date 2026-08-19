from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PreparedFile:
    file_id: str
    file_name: str
    safe_url: str


class FileDownloadService(Protocol):
    async def prepare(self, files: list[dict]) -> list[PreparedFile]: ...


class MockFileDownloadService:
    """Milestone 0-1 placeholder. It never performs a network request."""

    async def prepare(self, files: list[dict]) -> list[PreparedFile]:
        return [
            PreparedFile(file_id=item["file_id"], file_name=item["file_name"], safe_url=item["safe_url"])
            for item in files
        ]

