from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import WorkflowError
from app.services.temp_files import TaskWorkspace

Resolver = Callable[[str, int], Awaitable[list[str]]]

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


@dataclass(frozen=True)
class LocalFile:
    file_id: str
    role: str
    file_name: str
    safe_url: str
    path: Path
    file_size: int
    sha256: str
    detected_mime_type: str


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


class SafeFileDownloadService:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve_host,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.resolver = resolver
        self.allowlist = {
            host.strip().lower().rstrip(".")
            for host in settings.DOWNLOAD_HOST_ALLOWLIST.split(",")
            if host.strip()
        }

    async def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise WorkflowError("DOWNLOAD_FORBIDDEN_TARGET", "文件地址协议或主机不允许")
        if parsed.scheme == "http" and not self.settings.ALLOW_HTTP_DOWNLOADS:
            raise WorkflowError("DOWNLOAD_FORBIDDEN_TARGET", "当前环境不允许 HTTP 文件地址")
        host = parsed.hostname.lower().rstrip(".")
        explicitly_allowed = host in self.allowlist
        if self.allowlist and not explicitly_allowed:
            raise WorkflowError("DOWNLOAD_FORBIDDEN_TARGET", "文件地址主机不在允许列表")
        try:
            addresses = await self.resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except (OSError, socket.gaierror) as exc:
            raise WorkflowError("DOWNLOAD_FAILED", "文件地址 DNS 解析失败", retryable=True) from exc
        if not addresses:
            raise WorkflowError("DOWNLOAD_FAILED", "文件地址没有可用 IP", retryable=True)
        if explicitly_allowed:
            return
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise WorkflowError("DOWNLOAD_FORBIDDEN_TARGET", "文件地址解析到禁止访问的网络")

    @staticmethod
    def _file_type(file_name: str) -> tuple[str, str]:
        suffix = Path(file_name).suffix.lower()
        if suffix == ".docx":
            return suffix, DOCX_MIME
        if suffix == ".pdf":
            return suffix, PDF_MIME
        raise WorkflowError("UNSUPPORTED_FILE_TYPE", "仅支持 DOCX 和 PDF 文件")

    @staticmethod
    def _validate_signature(path: Path, expected_mime: str) -> None:
        signature = path.read_bytes()[:8]
        valid = signature.startswith(b"PK\x03\x04") if expected_mime == DOCX_MIME else signature.startswith(b"%PDF-")
        if not valid:
            raise WorkflowError("FILE_CONTENT_INVALID", "文件内容与声明的格式不一致")

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        item: dict,
        workspace: TaskWorkspace,
    ) -> LocalFile:
        suffix, expected_mime = self._file_type(item["file_name"])
        current_url = item["url"]
        max_bytes = int(self.settings.MAX_FILE_SIZE_MB * 1024 * 1024)
        redirects = 0
        while True:
            await self._validate_url(current_url)
            try:
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirects >= self.settings.DOWNLOAD_MAX_REDIRECTS:
                            raise WorkflowError("DOWNLOAD_FAILED", "文件下载重定向次数过多")
                        location = response.headers.get("location")
                        if not location:
                            raise WorkflowError("DOWNLOAD_FAILED", "文件下载重定向缺少目标地址")
                        current_url = urljoin(current_url, location)
                        redirects += 1
                        continue
                    if response.status_code >= 400:
                        raise WorkflowError("DOWNLOAD_FAILED", "文件服务返回下载失败", retryable=response.status_code >= 500)
                    declared_length = response.headers.get("content-length")
                    if declared_length and int(declared_length) > max_bytes:
                        raise WorkflowError("FILE_TOO_LARGE", "文件超过允许大小")
                    target = workspace.allocate(item["file_id"], suffix)
                    digest = hashlib.sha256()
                    size = 0
                    with target.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise WorkflowError("FILE_TOO_LARGE", "文件超过允许大小")
                            digest.update(chunk)
                            stream.write(chunk)
                    self._validate_signature(target, expected_mime)
                    return LocalFile(
                        file_id=item["file_id"],
                        role=item["role"],
                        file_name=item["file_name"],
                        safe_url=item["safe_url"],
                        path=target,
                        file_size=size,
                        sha256=digest.hexdigest(),
                        detected_mime_type=expected_mime,
                    )
            except WorkflowError:
                raise
            except httpx.TimeoutException as exc:
                raise WorkflowError("DOWNLOAD_TIMEOUT", "文件下载超时", retryable=True) from exc
            except (httpx.HTTPError, OSError, ValueError) as exc:
                raise WorkflowError("DOWNLOAD_FAILED", "文件下载失败", retryable=True) from exc

    async def prepare(self, files: list[dict], workspace: TaskWorkspace) -> list[LocalFile]:
        timeout = httpx.Timeout(self.settings.DOWNLOAD_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(self.settings.DOWNLOAD_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    prepared = []
                    for item in files:
                        prepared.append(await self._download_one(client, item, workspace))
                    return prepared
        except TimeoutError as exc:
            raise WorkflowError("DOWNLOAD_TIMEOUT", "文件下载总时长超时", retryable=True) from exc
