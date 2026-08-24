from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.llm.base import LlmResult
from app.adapters.llm.schemas import (
    AdviceResponse,
    DocumentFactExtraction,
    FactMappingResponse,
    FactMappingReview,
    FactReview,
)
from app.core.config import Settings

JsonValidator = Callable[[Any], dict[str, Any]]
Sleeper = Callable[[float], Awaitable[None]]


class LlmClientError(Exception):
    def __init__(self, code: str, safe_message: str, *, retryable: bool = False) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    prefix = base if base.endswith("/v1") else f"{base}/v1"
    return f"{prefix}/{suffix.lstrip('/')}"


def _json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.I)
    if fence:
        stripped = fence.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LlmClientError("LLM_INVALID_JSON", "模型未返回有效 JSON") from exc
    if not isinstance(value, dict):
        raise LlmClientError("LLM_INVALID_JSON", "模型返回值不是 JSON 对象")
    return value


def _validate_extraction(value: Any) -> dict[str, Any]:
    try:
        return DocumentFactExtraction.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型事实结果不符合结构约束") from exc


def _validate_review(value: Any) -> dict[str, Any]:
    try:
        return FactReview.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型评审结果不符合结构约束") from exc


def _validate_advice(value: Any) -> dict[str, Any]:
    try:
        return AdviceResponse.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型建议结果不符合结构约束") from exc


def _validate_mapping(value: Any) -> dict[str, Any]:
    try:
        return FactMappingResponse.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型事实映射不符合结构约束") from exc


def _validate_mapping_review(value: Any) -> dict[str, Any]:
    try:
        return FactMappingReview.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型映射评审不符合结构约束") from exc


class OpenAIContractLlmClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not settings.LLM_BASE_URL.strip() or not settings.LLM_API_KEY.strip():
            raise ValueError("LLM base URL and API key are required")
        self.settings = settings
        self.transport = transport
        self.sleeper = sleeper

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        }

    async def probe_models(self) -> list[str]:
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings.LLM_TIMEOUT_SECONDS,
        ) as client:
            response = await self._request(
                client,
                "GET",
                _endpoint(self.settings.LLM_BASE_URL, "models"),
            )
        data = response.json().get("data") if isinstance(response.json(), dict) else None
        if not isinstance(data, list):
            raise LlmClientError("LLM_RESPONSE_INVALID", "模型列表响应格式无效")
        return [item["id"] for item in data if isinstance(item, dict) and item.get("id")]

    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult:
        system = (
            "你是合同事实抽取器。只返回一个 JSON 对象，不要 Markdown。"
            "根据本次正文开放式识别文档用途、字段及有业务含义的金额、比例、利率、期限、"
            "期数、数量和日期，不受固定字段清单限制。所有事实必须逐字来自输入块并携带原文件位置；"
            "不得推测、补全或修正原文。逐块抽取时不得因为当前块未出现其他字段而声明缺失。"
        )
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=system,
            payload=payload,
            validator=_validate_extraction,
            schema=DocumentFactExtraction,
        )

    async def review_facts(self, payload: dict[str, Any]) -> LlmResult:
        system = (
            "你是独立的合同事实评审器。逐项核验主模型给出的候选事实是否确实来自同一文件、"
            "字段语义是否正确、位置和证据文本是否匹配，并独立检查动态数值规则。"
            "不得选择冲突来源中的正确值，不得补全输入中不存在的事实。只返回 JSON，符合 FactReview。"
        )
        return await self._structured_completion(
            model=self.settings.LLM_REVIEW_MODEL,
            system=system,
            payload=payload,
            validator=_validate_review,
            schema=FactReview,
        )

    async def map_facts(self, payload: dict[str, Any]) -> LlmResult:
        system = (
            "你是跨文件合同事实映射器。目标事实目录由程序分配 target_fact_id。"
            "仅当辅助资料事实与目标事实的业务含义、条件、时间范围、单位和口径相同或可能相同时"
            "返回映射；不得仅凭字段名或数值相似映射，不得选择冲突值中的正确值。"
            "未提及不等于冲突。只有资料用途明确要求某目标事实必须出现时，才提出缺失复核要求。"
        )
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=system,
            payload=payload,
            validator=_validate_mapping,
            schema=FactMappingResponse,
        )

    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult:
        system = (
            "你是独立的跨文件事实映射评审器。逐项核验目标事实、辅助资料事实、位置证据、"
            "单位、时间范围和业务口径。映射不可靠时必须 REJECT 或 UNCERTAIN；"
            "不得补充不存在的映射或自动选择正确来源。"
        )
        return await self._structured_completion(
            model=self.settings.LLM_REVIEW_MODEL,
            system=system,
            payload=payload,
            validator=_validate_mapping_review,
            schema=FactMappingReview,
        )

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        system = (
            "你只能根据输入中的既有风险、复核项和证据生成建议，不得新增事实或结论。"
            "建议中的证据引用必须逐字使用输入提供的 file_id 和位置；无法引用时留空并说明限制。"
            "只返回符合 AdviceResponse 的 JSON 对象。"
        )

        return await self._structured_completion(
            model=self.settings.LLM_ADVICE_MODEL,
            system=system,
            payload=payload,
            validator=_validate_advice,
            schema=AdviceResponse,
        )

    async def _structured_completion(
        self,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        validator: JsonValidator,
        schema: type[BaseModel],
    ) -> LlmResult:
        started = time.perf_counter()
        request_attempts = 0
        last_error: LlmClientError | None = None
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings.LLM_TIMEOUT_SECONDS,
        ) as client:
            for structure_attempt in range(self.settings.LLM_STRUCTURE_RETRY_ATTEMPTS + 1):
                schema_json = json.dumps(
                    schema.model_json_schema(), ensure_ascii=False, separators=(",", ":")
                )
                messages = [
                    {
                        "role": "system",
                        "content": f"{system} 严格遵守以下 JSON Schema：{schema_json}",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    },
                ]
                if structure_attempt:
                    messages.append(
                        {
                            "role": "system",
                            "content": "上一响应未通过 JSON 结构校验。严格按指定结构重新返回。",
                        }
                    )
                body: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": self.settings.LLM_MAX_OUTPUT_TOKENS,
                }
                if self.settings.LLM_NATIVE_STRUCTURED_OUTPUT:
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        },
                    }
                response, attempts = await self._request_with_count(
                    client,
                    "POST",
                    _endpoint(self.settings.LLM_BASE_URL, "chat/completions"),
                    json=body,
                )
                request_attempts += attempts
                try:
                    response_body = response.json()
                    choice = response_body["choices"][0]
                    content = choice["message"]["content"]
                    if not isinstance(content, str):
                        raise LlmClientError("LLM_RESPONSE_INVALID", "模型响应缺少文本内容")
                    value = validator(_json_object(content))
                    return LlmResult(
                        value=value,
                        configured_model=model,
                        actual_model=response_body.get("model"),
                        mock=False,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        request_attempts=request_attempts,
                        structure_retries=structure_attempt,
                    )
                except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                    last_error = LlmClientError("LLM_RESPONSE_INVALID", "模型响应结构无效")
                    last_error.__cause__ = exc
                except LlmClientError as exc:
                    last_error = exc
        raise last_error or LlmClientError("LLM_RESPONSE_INVALID", "模型响应结构无效")

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        response, _attempts = await self._request_with_count(client, method, url, **kwargs)
        return response

    async def _request_with_count(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response, int]:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers,
                    **kwargs,
                )
            except httpx.TimeoutException as exc:
                if attempt < max_attempts:
                    await self.sleeper(0.5 * attempt)
                    continue
                raise LlmClientError("LLM_TIMEOUT", "模型服务请求超时", retryable=True) from exc
            except httpx.RequestError as exc:
                if attempt < max_attempts:
                    await self.sleeper(0.5 * attempt)
                    continue
                raise LlmClientError(
                    "LLM_NETWORK_ERROR", "模型服务网络请求失败", retryable=True
                ) from exc
            if response.status_code < 400:
                return response, attempt
            code, message, retryable = self._http_error(response.status_code)
            if retryable and attempt < max_attempts:
                await self.sleeper(0.5 * attempt)
                continue
            raise LlmClientError(code, message, retryable=retryable)
        raise AssertionError("unreachable")

    @staticmethod
    def _http_error(status_code: int) -> tuple[str, str, bool]:
        if status_code in {401, 403}:
            return "LLM_AUTH_FAILED", "模型服务鉴权失败", False
        if status_code == 404:
            return "LLM_ENDPOINT_NOT_FOUND", "模型服务地址或模型不存在", False
        if status_code == 429:
            return "LLM_RATE_LIMITED", "模型服务请求过于频繁", True
        if status_code >= 500:
            return "LLM_UPSTREAM_ERROR", "模型服务暂时不可用", True
        return "LLM_REQUEST_REJECTED", "模型服务拒绝了请求", False
