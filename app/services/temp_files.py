from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path


class TaskWorkspace:
    """Task-scoped temporary storage that is always removed on exit."""

    def __init__(self, root: str | Path, task_id: str) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.path: Path | None = None

    async def __aenter__(self) -> TaskWorkspace:
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        safe_task_id = re.sub(r"[^A-Za-z0-9_-]", "_", self.task_id)[:48]
        created = Path(tempfile.mkdtemp(prefix=f"{safe_task_id}-", dir=root)).resolve()
        if created.parent != root:
            raise RuntimeError("temporary workspace escaped configured root")
        self.path = created
        return self

    def allocate(self, file_id: str, suffix: str) -> Path:
        if self.path is None:
            raise RuntimeError("workspace is not active")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", file_id)[:48]
        candidate = (self.path / f"{safe_id}{suffix}").resolve()
        if candidate.parent != self.path:
            raise RuntimeError("temporary file escaped task workspace")
        return candidate

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.path and self.path.exists():
            await asyncio.to_thread(shutil.rmtree, self.path, True)
