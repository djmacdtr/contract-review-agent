from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import orjson
from pydantic import ValidationError

from app.adapters.document_parser.base import ParseMode
from app.adapters.document_parser.textin_models import TextInParseResponse
from app.core.config import Settings
from app.core.errors import WorkflowError
from app.services.downloader import LocalFile

TEXTIN_ENGINE_PATH = "/api/contracts/v3/parser/external/engine"
HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
TRANSIENT_STATUSES = {502, 503, 504}
FIXED_PARAMETERS = {
    "page_details": 1,
    "markdown_details": 1,
    "table_flavor": "html",
    "get_image": "none",
    "get_excel": 0,
    "raw_ocr": 1,
    "char_details": 0,
    "apply_document_tree": 1,
    "apply_merge": 1,
}

Sleep = Callable[[float], Awaitable[None]]


class TextInDocumentParserClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.sleep = sleep

    def _configuration(self) -> tuple[str, str, str]:
        base_url = self.settings.OCR_BASE_URL.strip().rstrip("/")
        key = self.settings.OCR_API_KEY.strip()
        header = self.settings.OCR_AUTH_HEADER.strip()
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        valid_header = (
            bool(HEADER_NAME.fullmatch(header)) and header.lower() not in FORBIDDEN_HEADERS
        )
        if not self.settings.OCR_ENABLED or not valid_url or not key or not valid_header:
            raise WorkflowError("OCR_NOT_CONFIGURED", "OCR 文档解析服务未完整配置")
        return base_url, header, key

    async def _body(self, file: LocalFile) -> AsyncIterator[bytes]:
        with file.path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                yield chunk

    async def _read_limited(self, response: httpx.Response) -> bytes:
        limit = int(self.settings.OCR_MAX_RESPONSE_MB * 1024 * 1024)
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应超过允许大小")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应超过允许大小")
        return bytes(body)

    @staticmethod
    def _failure_details(kind: str, attempts: int, started: float) -> dict[str, int | str]:
        return {
            "component": "EXTERNAL_DOCUMENT_PARSER",
            "failure_kind": kind,
            "attempts": attempts,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        }

    @staticmethod
    def _network_failure_kind(exc: httpx.HTTPError) -> str:
        if isinstance(exc, httpx.ConnectTimeout):
            return "CONNECT_TIMEOUT"
        if isinstance(exc, httpx.ReadTimeout):
            return "READ_TIMEOUT"
        if isinstance(exc, httpx.WriteTimeout):
            return "WRITE_TIMEOUT"
        return "NETWORK_ERROR"

    def _raise_http(self, status: int, *, attempts: int, started: float) -> None:
        if status in {401, 403}:
            raise WorkflowError("OCR_AUTH_FAILED", "OCR 服务鉴权失败")
        if status in {400, 406, 422}:
            raise WorkflowError("OCR_REQUEST_INVALID", "OCR 服务拒绝了解析请求")
        if status == 429:
            raise WorkflowError("OCR_QUOTA_EXCEEDED", "OCR 服务额度不足")
        if status >= 500:
            details = (
                self._failure_details(f"UPSTREAM_{status}", attempts, started)
                if status in TRANSIENT_STATUSES
                else None
            )
            raise WorkflowError(
                "OCR_SERVICE_UNAVAILABLE",
                "OCR 服务暂时不可用",
                retryable=True,
                details=details,
            )
        if status >= 400:
            raise WorkflowError("OCR_PARSE_FAILED", "OCR 服务无法解析文档")

    def _raise_business(self, code: int) -> None:
        mapping = {
            400: ("OCR_REQUEST_INVALID", "OCR 服务拒绝了解析请求"),
            401: ("OCR_AUTH_FAILED", "OCR 服务鉴权失败"),
            403: ("OCR_AUTH_FAILED", "OCR 服务鉴权失败"),
            406: ("OCR_REQUEST_INVALID", "OCR 服务拒绝了解析请求"),
            40423: ("OCR_PASSWORD_REQUIRED", "PDF 已加密或需要密码"),
            40429: ("OCR_QUOTA_EXCEEDED", "OCR 服务额度不足"),
            50207: ("OCR_PARTIAL_FAILURE", "OCR 服务仅完成了部分页面"),
            500: ("OCR_SERVICE_UNAVAILABLE", "OCR 服务暂时不可用"),
            10702: ("OCR_RESPONSE_INVALID", "同步 OCR 返回了未完成状态"),
            10703: ("OCR_PARSE_FAILED", "OCR 服务解析文档失败"),
        }
        code_and_message = mapping.get(code, ("OCR_PARSE_FAILED", "OCR 服务解析文档失败"))
        raise WorkflowError(*code_and_message, retryable=code == 500)

    async def parse(self, file: LocalFile, *, mode: ParseMode) -> TextInParseResponse:
        base_url, header, key = self._configuration()
        started = time.monotonic()
        timeout = httpx.Timeout(
            self.settings.OCR_TIMEOUT_SECONDS,
            connect=min(30.0, self.settings.OCR_TIMEOUT_SECONDS),
        )
        attempts = self.settings.OCR_HTTP_RETRY_ATTEMPTS + 1
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{base_url}{TEXTIN_ENGINE_PATH}",
                        params={**FIXED_PARAMETERS, "parse_mode": mode},
                        headers={header: key, "Content-Type": "application/octet-stream"},
                        content=self._body(file),
                    ) as response:
                        if response.status_code in TRANSIENT_STATUSES and attempt + 1 < attempts:
                            await self._read_limited(response)
                            await self.sleep(self.settings.OCR_RETRY_BACKOFF_SECONDS * (2**attempt))
                            continue
                        self._raise_http(
                            response.status_code,
                            attempts=attempt + 1,
                            started=started,
                        )
                        body = await self._read_limited(response)
                try:
                    parsed = TextInParseResponse.model_validate(orjson.loads(body))
                except (orjson.JSONDecodeError, ValidationError, TypeError) as exc:
                    raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应结构无效") from exc
                if parsed.code != 200:
                    self._raise_business(parsed.code)
                if parsed.data is None or parsed.data.result is None:
                    raise WorkflowError("OCR_RESPONSE_INVALID", "OCR 服务响应缺少解析结果")
                parsed._response_size_bytes = len(body)
                return parsed
            except WorkflowError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < attempts:
                    await self.sleep(self.settings.OCR_RETRY_BACKOFF_SECONDS * (2**attempt))
                    continue
                raise WorkflowError(
                    "OCR_SERVICE_UNAVAILABLE",
                    "OCR 服务连接失败或超时",
                    retryable=True,
                    details=self._failure_details(
                        self._network_failure_kind(exc), attempt + 1, started
                    ),
                ) from exc
        raise WorkflowError(
            "OCR_SERVICE_UNAVAILABLE",
            "OCR 服务暂时不可用",
            retryable=True,
            details=self._failure_details("NETWORK_ERROR", attempts, started),
        )
