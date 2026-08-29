from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.llm.base import LlmResult
from app.adapters.llm.schemas import (
    AdviceResponse,
    CompactDocumentFactExtraction,
    CompactDocumentOverview,
    CompactFactBatchExtraction,
    CompactFactReview,
    CompactSemanticPlanResponse,
    DocumentFactExtraction,
    FactMappingResponse,
    FactMappingReview,
    FactReview,
    NumericCandidateExtraction,
    SemanticConceptPlan,
    SemanticEvidenceRef,
    SemanticFactRef,
    SemanticPlanResponse,
    SemanticValidationSpec,
    TextFactExtraction,
)
from app.core.config import Settings
from app.documents.models import DocumentLocation
from app.draft_review.facts import (
    TEXT_FACT_VALUE_TYPES,
    EvidenceValidationError,
    expand_compact_extraction,
    expand_document_overview,
    expand_fact_batch,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    location_key,
    numeric_candidate_indexes,
)

JsonValidator = Callable[[Any], dict[str, Any]]
Sleeper = Callable[[float], Awaitable[None]]
ResponseFormat = Literal["prompt_only", "json_object", "json_schema"]

_HTTP_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_HTTP_MAX_RETRIES = 4
_HTTP_TIMEOUT_MAX_ATTEMPTS = 2
_HTTP_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

_PROFILE_MAX_OUTPUT_TOKENS = 2048
_TEXT_MAX_OUTPUT_TOKENS = 8192
_MAPPING_MAX_OUTPUT_TOKENS = 12288
_ADVICE_MAX_OUTPUT_TOKENS = 8192


def _numeric_max_output_tokens(payload: dict[str, Any]) -> int:
    """Keep numeric wire requests proportional to their candidate count."""

    candidate_count = len(payload.get("numeric_candidates", []))
    return min(8192, max(2048, 512 + candidate_count * 256))


def _safe_usage_summary(value: Any) -> dict[str, int]:
    """Keep only aggregate token counters from a provider usage object."""

    if not isinstance(value, dict):
        return {}
    allowed = {"prompt_tokens", "completion_tokens", "total_tokens"}
    return {
        key: int(item)
        for key, item in value.items()
        if key in allowed and type(item) is int and item >= 0
    }

EXTRACTION_SYSTEM_PROMPT = (
    "你是合同事实抽取器。只返回一个 JSON 对象，不要 Markdown。"
    "开放式识别本次正文中的文档用途、业务字段和事实，不得使用固定字段清单。"
    "逐块扫描所有事实；优先逐项分类输入中的 numeric_candidates，"
    "不能遗漏同一字段在不同位置出现的不同原值。"
    "每个事实只返回 field_key、display_name、value_type、raw_value、location、"
    "confidence 和可选 concept_id。"
    "不要返回 evidence_text、source_file_id、normalized_hint、missing_field_keys、"
    "semantic_concepts 或 validation_specs。"
    "表格按行提供时，事实必须使用 table_index、row 或 table_index、row、column 位置；"
    "如果 table_location_mode 为 ROW_ONLY，只能使用 table_index、row 位置；"
    "不得返回当前输入中不存在的整表位置。"
    "raw_value 必须逐字来自 location 对应的输入证据块；不得推测、补全、换算、修正或改写原值。"
    "只返回 profile 和 facts，facts 不得超过 Schema 上限。"
)
PROFILE_SYSTEM_PROMPT = (
    "你是合同文档概况识别器。只返回 JSON 对象，不要 Markdown。"
    "根据输入的大纲开放式识别文档用途和标题；不得假设固定合同类型或资料类型。"
    "只能引用输入中存在的概况位置，不要返回文件 ID、事实、证据原文或其他字段。"
    "严格遵守 CompactDocumentOverview。"
)
FACT_BATCH_SYSTEM_PROMPT = (
    "你是合同事实抽取器。只返回 JSON 对象，不要 Markdown。"
    "当前输入是独立事实分片，不包含完整文档概况。逐块扫描开放式事实，"
    "不得使用固定字段清单，不得推测、补全、换算、修正或改写原值。"
    "每个事实只返回 field_key、display_name、value_type、raw_value、location、"
    "confidence 和可选 candidate_indices；不得返回 evidence_text、source_file_id、"
    "normalized_hint 或稳定事实 ID。必须逐项处理 numeric_candidates，"
    "每个候选恰好返回 FACT 或 IGNORE 决策；事实的 candidate_indices 必须准确对应。"
    "不得返回输入中不存在的位置。严格遵守 CompactFactBatchExtraction。"
)
NUMERIC_CANDIDATE_SYSTEM_PROMPT = (
    "你是开放式合同数值候选分类器。只返回 JSON 对象，不要 Markdown。"
    "逐项处理输入 numeric_candidates，每个 candidate_index 必须恰好返回一次。"
    "candidate_index 是必填整数，只能使用本批输入中实际给出的候选索引。"
    "只返回 semantic_key、display_name、value_type、decision、reason_code、confidence。"
    "不得返回原值、证据、位置、文件身份或任何未要求字段。"
    "不得使用固定合同类型或字段清单；无法可靠识别业务价值时返回 IGNORE。"
    "严格遵守 NumericCandidateExtraction。"
)
TEXT_FACT_SYSTEM_PROMPT = (
    "你是开放式合同非数值事实抽取器。只返回 JSON 对象，不要 Markdown。"
    "逐个扫描输入 units，事实只能引用输入中的候选 unit_id。quote 必须是该 unit 原文的精确子串。"
    "readonly_context 仅用于理解标题、字段标签和表头，绝不能作为事实来源或 quote 来源。"
    "只返回 has_more，以及 unit_id、semantic_key、display_name、value_type、quote、confidence。"
    "数值、日期和标识类事实由 numeric-v2 处理；本链只返回 TEXT、ENTITY 或 UNKNOWN。"
    "不得返回文件身份、位置、证据副本、稳定事实 ID 或原文外的推测。"
    "没有可可靠识别的非数值事实时，必须返回 JSON {\"items\":[],\"has_more\":false}，"
    "不得返回解释、拒绝语或自然语言。"
    "无法从候选 unit 原文唯一回查 quote 时，也必须返回 JSON {\"items\":[],\"has_more\":false}，"
    "不得猜测或改写。"
    "一次最多返回输入 requirements.max_items 指定的项目数；如果仍有更多可靠事实，"
    "返回 has_more=true；如果本批事实已经完整，即使恰好达到上限也返回 has_more=false。"
    "严格遵守 TextFactExtraction。"
)
SEMANTIC_PLAN_SYSTEM_PROMPT = (
    "你是合同事实语义规划器。只返回一个 JSON 对象，不要 Markdown。"
    "输入只包含已经通过事实评审的事实和已经通过映射评审的关系，且所有事实都有稳定 fact_id。"
    "只生成开放式语义概念和必要的声明式数值校验规则，不得发明输入中不存在的事实。"
    "概念只返回 fact_ids 引用事实，不要返回 source_file_id、位置或证据文本；"
    "程序会根据已验证事实回填这些字段。"
    "规则 AST 的 fact 节点必须同时使用 fact_id 和 source_file_id，"
    "禁止使用 field_key 或 concept_id。"
    "规则只返回 expression，不要返回 evidence_refs；程序会根据 AST 中的事实回填证据位置。"
    "校验规则只能使用允许的 numeric AST；不要输出代码、自然语言表达式或重复事实证据。"
    "只为当前输入目标事实批次生成必要的概念和规则；不要为每条事实机械创建概念，"
    "不要重复输入事实，也不要引用当前批次之外的目标事实。"
    "没有可靠概念或规则时返回空数组。严格遵守 CompactSemanticPlanResponse。"
)
REVIEW_SYSTEM_PROMPT = (
    "你是独立的合同事实评审器。逐项核验主模型给出的候选事实是否确实来自同一文件、"
    "字段语义是否正确、原值、位置和证据文本是否匹配，并独立检查动态数值规则。"
    "每条候选应独立判断：只要该条事实的原值、证据和位置真实匹配，就应 ACCEPT；"
    "不得因为正文其他位置存在不同值而拒绝任一真实事实，也不得选择所谓正确值。"
    "不得补全输入中不存在的事实。必须为每条输入候选恰好返回一个决策。"
    "只返回 JSON，符合 FactReview。"
)
MAPPING_REVIEW_SYSTEM_PROMPT = (
    "你是独立的跨文件事实映射评审器。逐项核验目标事实、辅助资料事实、位置证据、"
    "单位、时间范围和业务口径。映射不可靠时必须 REJECT 或 UNCERTAIN；"
    "不得补充不存在的映射或自动选择正确来源。"
)
ADVICE_SYSTEM_PROMPT = (
    "你只能根据输入中的既有风险、关联差异、文件名和证据位置生成建议，不得新增事实或结论。"
    "面向业务人员的建议不得出现 file_id、内部坐标或其他技术标识。"
    "必须在 risk_advices 中按 risk_id 为每条输入风险给出针对其差异文字、文件和位置的分析建议，"
    "每条建议只写一个无换行的简洁句子，不得使用列表、重复句或通用空话。"
    "不得使用同一句通用模板。只返回符合 AdviceResponse 的 JSON 对象。"
)


class LlmClientError(Exception):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        correction_message: str | None = None,
        failure_code: str | None = None,
        validation_summary: dict[str, Any] | None = None,
        request_attempts: int = 0,
        structure_retries: int = 0,
        finish_reason: str | None = None,
        content_chars: int | None = None,
        code_fence: bool | None = None,
        json_error_position: int | None = None,
        reasoning_content_chars: int | None = None,
        usage: dict[str, int] | None = None,
        max_tokens: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.correction_message = correction_message
        self.failure_code = failure_code
        self.validation_summary = validation_summary
        self.request_attempts = request_attempts
        self.structure_retries = structure_retries
        self.finish_reason = finish_reason
        self.content_chars = content_chars
        self.code_fence = code_fence
        self.json_error_position = json_error_position
        self.reasoning_content_chars = reasoning_content_chars
        self.usage = usage
        self.max_tokens = max_tokens
        self.http_status = http_status


def completion_body(
    *,
    model: str,
    system: str,
    payload: dict[str, Any],
    schema: type[BaseModel],
    max_tokens: int,
    response_format: str = "prompt_only",
    correction: bool = False,
    correction_message: str | None = None,
    response_schema: dict[str, Any] | None = None,
    disable_thinking: bool = False,
) -> dict[str, Any]:
    schema_definition = response_schema or schema.model_json_schema()
    schema_json = json.dumps(schema_definition, ensure_ascii=False, separators=(",", ":"))
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
    if correction:
        messages.append(
            {
                "role": "system",
                "content": correction_message
                or "上一响应未通过 JSON 结构校验。严格按指定结构重新返回。",
            }
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if disable_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if response_format == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": schema_definition,
            },
        }
    elif response_format == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif response_format != "prompt_only":
        raise ValueError(f"unsupported response format: {response_format}")
    return body


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    prefix = base if base.endswith("/v1") else f"{base}/v1"
    return f"{prefix}/{suffix.lstrip('/')}"


def _json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        error = LlmClientError(
            "LLM_INVALID_JSON",
            "模型未返回有效 JSON",
            json_error_position=exc.pos,
        )
        raise error from exc
    if not isinstance(value, dict):
        raise LlmClientError("LLM_INVALID_JSON", "模型返回值不是 JSON 对象")
    return value


def _safe_validation_summary(
    error: ValidationError,
    *,
    max_items: int = 8,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], int] = {}
    errors = error.errors()
    for item in errors:
        raw_location = item.get("loc", ())
        if isinstance(raw_location, (list, tuple)):
            location = raw_location
        else:
            location = (raw_location,)
        path_parts = [
            "*" if isinstance(part, int) else part
            for part in location
            if isinstance(part, (int, str))
        ]
        path = ".".join(path_parts) or "$"
        error_type = item.get("type")
        if not isinstance(error_type, str) or not error_type:
            error_type = "validation_error"
        key = (path, error_type)
        grouped[key] = grouped.get(key, 0) + 1

    items = [
        {"path": path, "error_type": error_type, "count": count}
        for (path, error_type), count in sorted(grouped.items())
    ]
    return {
        "error_count": len(errors),
        "items": items[:max_items],
        "truncated": len(items) > max_items,
    }


def _schema_correction_message(summary: dict[str, Any]) -> str:
    safe_summary = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    return (
        "上一响应未通过事实抽取 JSON Schema。仅根据以下安全结构摘要修正，"
        "并完整返回一个符合 Schema 的 JSON 对象："
        f"{safe_summary}。不得输出 Markdown、解释或证据正文；"
        "不得修改、推测、补全或改写事实原值和位置。"
    )


def _validate_extraction(value: Any) -> dict[str, Any]:
    try:
        return DocumentFactExtraction.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型事实结果不符合结构约束") from exc


def _validate_compact_extraction(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        compact = CompactDocumentFactExtraction.model_validate(value)
        return expand_compact_extraction(payload, compact).model_dump(mode="json")
    except EvidenceValidationError as exc:
        raise LlmClientError(
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "模型紧凑事实结果未通过安全证据校验",
            failure_code=exc.code,
        ) from exc
    except ValidationError as exc:
        summary = _safe_validation_summary(exc)
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型事实结果不符合结构约束",
            correction_message=_schema_correction_message(summary),
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=summary,
        ) from exc


def _validate_document_overview(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        overview = CompactDocumentOverview.model_validate(value)
        expand_document_overview(payload, overview.model_dump(mode="json"))
        return overview.model_dump(mode="json")
    except EvidenceValidationError as exc:
        raise LlmClientError(
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "模型文档概况位置未通过安全校验",
            correction_message=(
                "上一响应引用了不存在的概况位置。只返回输入大纲中的位置，"
                "不得输出文件 ID 或解释。"
            ),
            failure_code=exc.code,
        ) from exc
    except ValidationError as exc:
        summary = _safe_validation_summary(exc)
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型文档概况不符合结构约束",
            correction_message=_schema_correction_message(summary),
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=summary,
        ) from exc


def _validate_fact_batch(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        compact = CompactFactBatchExtraction.model_validate(value)
    except ValidationError as exc:
        summary = _safe_validation_summary(exc)
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型事实分片结果不符合结构约束",
            correction_message=_schema_correction_message(summary),
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=summary,
        ) from exc
    try:
        expand_fact_batch(payload, compact)
    except EvidenceValidationError as exc:
        raise LlmClientError(
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "模型事实分片未通过安全证据校验",
            correction_message=(
                "上一响应的事实位置或数值候选分类无效。只使用输入位置，"
                "逐项返回每个候选的 FACT 或 IGNORE，不得改写事实原值。"
            ),
            failure_code=exc.code,
        ) from exc
    return compact.model_dump(
        mode="json", exclude_none=True
    )


def _validate_numeric_candidates(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    index_summary = _numeric_candidate_response_summary(value, payload)
    try:
        response = NumericCandidateExtraction.model_validate(value)
        expand_numeric_candidate_response(payload, response)
    except EvidenceValidationError as exc:
        raise LlmClientError(
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "模型数值候选分类未通过安全校验",
            correction_message=(
                "上一响应的 candidate_index 未完整覆盖输入。只返回每个输入索引恰好一次，"
                "不要输出原值、位置或证据。"
            ),
            failure_code=exc.code,
            validation_summary=index_summary,
        ) from exc
    except ValidationError as exc:
        summary = {
            **index_summary,
            "schema_error_summary": _safe_validation_summary(exc),
        }
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型数值候选结果不符合结构约束",
            correction_message=_schema_correction_message(summary),
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=summary,
        ) from exc
    return response.model_dump(mode="json")


def _validate_text_facts(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = TextFactExtraction.model_validate(value)
        response = response.model_copy(
            update={
                "items": [
                    item
                    for item in response.items
                    if item.value_type in TEXT_FACT_VALUE_TYPES
                ]
            }
        )
        expand_text_fact_response(payload, response)
    except EvidenceValidationError as exc:
        raise LlmClientError(
            "LLM_EXTRACTION_EVIDENCE_INVALID",
            "模型非数值事实未通过安全证据校验",
            correction_message=_text_evidence_correction_message(exc.code),
            failure_code=exc.code,
        ) from exc
    except ValidationError as exc:
        summary = _safe_validation_summary(exc)
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型非数值事实结果不符合结构约束",
            correction_message=_schema_correction_message(summary),
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=summary,
        ) from exc
    return response.model_dump(mode="json")


def _text_fact_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the controller's per-unit fact limit to the wire schema."""

    try:
        max_items = int(payload.get("requirements", {}).get("max_items", 12))
    except (AttributeError, TypeError, ValueError):
        max_items = 12
    max_items = max(1, min(max_items, 12))
    schema = TextFactExtraction.model_json_schema()
    items_schema = schema.get("properties", {}).get("items")
    if isinstance(items_schema, dict):
        items_schema["maxItems"] = max_items
    schema["required"] = ["items", "has_more"]
    return schema


def _numeric_candidate_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Require one wire decision for every candidate in this exact batch."""

    candidates = payload.get("numeric_candidates", [])
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    if candidate_count == 0:
        raise ValueError("numeric candidate response schema requires at least one candidate")
    expected_indices = numeric_candidate_indexes(payload)
    schema = NumericCandidateExtraction.model_json_schema()
    items_schema = schema.get("properties", {}).get("items")
    if isinstance(items_schema, dict):
        items_schema["minItems"] = candidate_count
        items_schema["maxItems"] = candidate_count
    item_definition = schema.get("$defs", {}).get("NumericCandidateItem")
    if isinstance(item_definition, dict):
        properties = item_definition.get("properties")
        if isinstance(properties, dict):
            wire_properties = {
                key: properties[key]
                for key in (
                    "candidate_index",
                    "semantic_key",
                    "display_name",
                    "value_type",
                    "decision",
                    "reason_code",
                    "confidence",
                )
                if key in properties
            }
            wire_properties["candidate_index"] = {
                "type": "integer",
                "enum": expected_indices,
            }
            item_definition["properties"] = wire_properties
            item_definition["required"] = list(wire_properties)
    return schema


def _numeric_candidate_response_summary(
    value: Any, payload: dict[str, Any]
) -> dict[str, int]:
    """Return only count/index diagnostics for a numeric response."""

    candidates = payload.get("numeric_candidates", [])
    expected_count = len(candidates) if isinstance(candidates, list) else 0
    expected_indices = set(numeric_candidate_indexes(payload))
    items = value.get("items") if isinstance(value, dict) else None
    returned_items = items if isinstance(items, list) else []
    valid_indices: list[int] = []
    invalid_index_count = 0
    for item in returned_items:
        index = item.get("candidate_index") if isinstance(item, dict) else None
        if type(index) is not int or index not in expected_indices:
            invalid_index_count += 1
            continue
        valid_indices.append(index)
    duplicate_index_count = len(valid_indices) - len(set(valid_indices))
    missing_index_count = len(expected_indices - set(valid_indices))
    return {
        "expected_count": expected_count,
        "returned_count": len(returned_items),
        "missing_index_count": missing_index_count,
        "duplicate_index_count": duplicate_index_count,
        "invalid_index_count": invalid_index_count,
    }


def _text_evidence_correction_message(failure_code: str | None) -> str:
    guidance = {
        "FACT_UNIT_NOT_FOUND": "只引用本次输入 units 中实际存在的 unit_id。",
        "FACT_QUOTE_NOT_GROUNDED": (
            "quote 必须是对应 unit 原文的精确子串；无法唯一回查时不要返回该事实。"
        ),
        "FACT_IDENTITY_DUPLICATED": "同一个 unit_id 和 semantic_key 只能返回一次。",
    }.get(
        failure_code,
        "只引用本次输入 units 中实际存在且能唯一回查的事实。",
    )
    return (
        "上一响应未通过非数值事实证据校验。"
        f"{guidance}如果没有任何事实能严格通过校验，必须只返回 JSON {{\"items\":[]}}；"
        "不得输出解释、拒绝语、Markdown、原文外的推测或改写。"
    )


def _semantic_fact_catalog(
    payload: dict[str, Any],
) -> dict[tuple[str, str], DocumentLocation]:
    catalog: dict[tuple[str, str], DocumentLocation] = {}
    for document in payload.get("documents", []):
        if not isinstance(document, dict):
            continue
        for fact in document.get("facts", []):
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("fact_id")
            source_file_id = fact.get("source_file_id")
            location = fact.get("location")
            if not isinstance(fact_id, str) or not isinstance(source_file_id, str):
                continue
            if not isinstance(location, dict):
                continue
            key = (fact_id, source_file_id)
            parsed_location = DocumentLocation.model_validate(location)
            existing = catalog.get(key)
            if existing is not None and location_key(existing) != location_key(parsed_location):
                raise LlmClientError(
                    "LLM_SEMANTIC_PLAN_INVALID",
                    "语义规划输入包含冲突的事实身份",
                )
            catalog[key] = parsed_location
    return catalog


def _semantic_correction_error(message: str) -> LlmClientError:
    return LlmClientError(
        "LLM_SCHEMA_INVALID",
        message,
        correction_message=(
            "上一响应未通过语义事实引用校验。只引用当前输入 documents 中实际存在的 fact_id；"
            "概念只返回当前批次的 fact_ids，规则 AST 的 fact 节点必须使用当前输入中的"
            "fact_id 和 source_file_id。不要返回证据位置、文件外事实、解释或 Markdown。"
        ),
        failure_code="LLM_SEMANTIC_PLAN_INVALID",
    )


def _semantic_ast_refs(node: Any) -> set[tuple[str, str]]:
    if not isinstance(node, dict):
        raise _semantic_correction_error("语义规则 AST 不是对象")
    if node.get("op") == "fact":
        fact_id = node.get("fact_id")
        source_file_id = node.get("source_file_id")
        if not isinstance(fact_id, str) or not isinstance(source_file_id, str):
            raise _semantic_correction_error("语义规则事实节点缺少限定身份")
        return {(fact_id, source_file_id)}
    references: set[tuple[str, str]] = set()
    for key in ("args", "left", "right"):
        child = node.get(key)
        if isinstance(child, list):
            for item in child:
                references.update(_semantic_ast_refs(item))
        elif isinstance(child, dict):
            references.update(_semantic_ast_refs(child))
    return references


def _semantic_evidence_refs(
    references: set[tuple[str, str]],
    catalog: dict[tuple[str, str], DocumentLocation],
) -> list[SemanticEvidenceRef]:
    evidence: list[SemanticEvidenceRef] = []
    seen: set[tuple[str, tuple[object, ...]]] = set()
    for reference in sorted(references):
        location = catalog.get(reference)
        if location is None:
            raise _semantic_correction_error("语义规划引用了当前批次之外的事实")
        key = (reference[1], location_key(location))
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            SemanticEvidenceRef(
                source_file_id=reference[1],
                location=location,
            )
        )
    if not evidence:
        raise _semantic_correction_error("语义规划结果缺少可回查证据")
    return evidence


def _validate_compact_semantic_plan(
    value: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        compact = CompactSemanticPlanResponse.model_validate(value)
    except ValidationError as exc:
        summary = _safe_validation_summary(exc)
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型语义规划结果不符合结构约束",
            correction_message=_schema_correction_message(summary),
            validation_summary=summary,
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
        ) from exc
    if compact.file_id != payload.get("file_id"):
        raise _semantic_correction_error("模型语义规划文件身份不匹配")

    catalog = _semantic_fact_catalog(payload)
    by_fact_id: dict[str, tuple[str, str]] = {}
    for fact_id, source_file_id in catalog:
        existing = by_fact_id.get(fact_id)
        if existing is not None and existing[1] != source_file_id:
            raise LlmClientError(
                "LLM_SEMANTIC_PLAN_INVALID",
                "语义规划输入包含重复事实 ID",
            )
        by_fact_id[fact_id] = (fact_id, source_file_id)

    concepts: list[SemanticConceptPlan] = []
    for concept in compact.semantic_concepts:
        references: set[tuple[str, str]] = set()
        for fact_id in concept.fact_ids:
            reference = by_fact_id.get(fact_id)
            if reference is None:
                raise _semantic_correction_error("语义概念引用了当前批次之外的事实")
            references.add(reference)
        try:
            concepts.append(
                SemanticConceptPlan(
                    concept_id=concept.concept_id,
                    display_name=concept.display_name,
                    value_type=concept.value_type,
                    aliases=concept.aliases,
                    fact_refs=[
                        SemanticFactRef(fact_id=fact_id, source_file_id=source_file_id)
                        for fact_id, source_file_id in sorted(references)
                    ],
                    evidence_refs=_semantic_evidence_refs(references, catalog),
                    confidence=concept.confidence,
                )
            )
        except ValidationError as exc:
            summary = _safe_validation_summary(exc)
            raise LlmClientError(
                "LLM_SCHEMA_INVALID",
                "模型语义规划结果不符合结构约束",
                correction_message=_schema_correction_message(summary),
                validation_summary=summary,
                failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            ) from exc

    validation_specs: list[SemanticValidationSpec] = []
    for spec in compact.validation_specs:
        references = _semantic_ast_refs(spec.expression)
        try:
            validation_specs.append(
                SemanticValidationSpec(
                    validation_id=spec.validation_id,
                    display_name=spec.display_name,
                    expression=spec.expression,
                    evidence_refs=_semantic_evidence_refs(references, catalog),
                    confidence=spec.confidence,
                )
            )
        except ValidationError as exc:
            summary = _safe_validation_summary(exc)
            raise LlmClientError(
                "LLM_SCHEMA_INVALID",
                "模型语义规划结果不符合结构约束",
                correction_message=_schema_correction_message(summary),
                validation_summary=summary,
                failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            ) from exc
    return _validate_semantic_plan(
        SemanticPlanResponse(
            file_id=compact.file_id,
            semantic_concepts=concepts,
            validation_specs=validation_specs,
        ).model_dump(mode="json"),
        payload,
    )


def _validate_semantic_plan(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = SemanticPlanResponse.model_validate(value)
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型语义规划结果不符合结构约束") from exc
    if plan.file_id != payload.get("file_id"):
        raise LlmClientError("LLM_SEMANTIC_PLAN_INVALID", "模型语义规划文件身份不匹配")
    concept_ids = [item.concept_id for item in plan.semantic_concepts]
    validation_ids = [item.validation_id for item in plan.validation_specs]
    if len(concept_ids) != len(set(concept_ids)) or len(validation_ids) != len(set(validation_ids)):
        raise LlmClientError("LLM_SEMANTIC_PLAN_INVALID", "模型语义规划包含重复身份")
    return plan.model_dump(mode="json")


def _review_identity(value: dict[str, Any] | Any) -> tuple[object, ...]:
    if isinstance(value, dict):
        location = DocumentLocation.model_validate(value.get("location"))
        field_key = value.get("field_key")
        source_file_id = value.get("source_file_id")
    else:
        location = value.location
        field_key = value.field_key
        source_file_id = value.source_file_id
    return (
        field_key,
        source_file_id,
        location.page,
        location.paragraph_index,
        location.table_index,
        location.row,
        location.column,
    )


def _identity_payload(identity: tuple[object, ...]) -> dict[str, Any]:
    field_key, source_file_id, page, paragraph_index, table_index, row, column = identity
    location = {
        key: value
        for key, value in {
            "page": page,
            "paragraph_index": paragraph_index,
            "table_index": table_index,
            "row": row,
            "column": column,
        }.items()
        if value is not None
    }
    return {
        "field_key": field_key,
        "source_file_id": source_file_id,
        "location": location,
    }


def review_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    facts = payload.get("facts")
    if not isinstance(facts, list) or not facts:
        raise LlmClientError(
            "LLM_REVIEW_INPUT_INVALID",
            "事实评审至少需要一个候选事实",
        )
    identities = [_review_identity(fact) for fact in facts]
    if len(identities) != len(set(identities)):
        raise LlmClientError(
            "LLM_REVIEW_INPUT_INVALID",
            "事实评审输入包含重复候选身份",
        )
    schema = deepcopy(FactReview.model_json_schema())
    decisions = schema["properties"]["decisions"]
    decisions["minItems"] = len(identities)
    decisions["maxItems"] = len(identities)
    return schema


def compact_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Send only evidence needed for review; identity remains program-owned."""

    compact: dict[str, Any] = {
        "facts": [
            {
                "fact_index": index,
                "display_name": fact.get("display_name"),
                "value_type": fact.get("value_type"),
                "raw_value": fact.get("raw_value"),
                "evidence_text": fact.get("evidence_text"),
            }
            for index, fact in enumerate(payload.get("facts", []), start=1)
            if isinstance(fact, dict)
        ],
        "semantic_concepts": payload.get("semantic_concepts", []),
        "validation_specs": payload.get("validation_specs", []),
    }
    compact["review_requirements"] = {
        "required_decision_count": len(compact["facts"]),
        "one_decision_per_fact": True,
        "decisions_addressed_by_fact_index": True,
        "identity_and_evidence_are_program_owned": True,
    }
    return compact


def compact_review_response_schema(payload: dict[str, Any]) -> dict[str, Any]:
    facts = payload.get("facts")
    if not isinstance(facts, list) or not facts:
        raise LlmClientError(
            "LLM_REVIEW_INPUT_INVALID",
            "事实评审至少需要一个候选事实",
        )
    schema = deepcopy(CompactFactReview.model_json_schema())
    decisions = schema["properties"]["decisions"]
    decisions["minItems"] = len(facts)
    decisions["maxItems"] = len(facts)
    return schema


def compact_review_correction_message(
    payload: dict[str, Any], value: Any
) -> str:
    expected = set(range(1, len(payload.get("facts", [])) + 1))
    actual = (
        [
            item.get("fact_index")
            for item in value.get("decisions", [])
            if isinstance(item, dict)
        ]
        if isinstance(value, dict) and isinstance(value.get("decisions"), list)
        else []
    )
    valid_actual = {item for item in actual if isinstance(item, int)}
    missing = sorted(expected - valid_actual)
    duplicates = sorted({item for item in actual if actual.count(item) > 1})
    unexpected = sorted(valid_actual - expected)
    requirements = {
        "required_decision_count": len(expected),
        "missing_fact_indices": missing,
        "duplicate_fact_indices": duplicates,
        "unexpected_fact_indices": unexpected,
    }
    return (
        "上一响应未完整覆盖输入事实。必须为每个 fact_index 恰好返回一个 decisions 项，"
        "不得遗漏、重复或新增。只返回符合 Schema 的 JSON 对象："
        + json.dumps(requirements, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_compact_review(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        compact = CompactFactReview.model_validate(value)
    except ValidationError:
        # Keep compatibility with already deployed clients and test fixtures
        # that still return the verbose internal response shape.
        return _validate_review(value, payload)
    expected = set(range(1, len(payload.get("facts", [])) + 1))
    actual = [item.fact_index for item in compact.decisions]
    if len(actual) != len(expected) or len(actual) != len(set(actual)) or set(actual) != expected:
        raise LlmClientError(
            "LLM_REVIEW_INCOMPLETE",
            "模型评审未完整覆盖候选事实",
            correction_message=compact_review_correction_message(payload, value),
        )
    decisions = []
    for item in compact.decisions:
        fact = payload["facts"][item.fact_index - 1]
        decisions.append(
            {
                "field_key": fact["field_key"],
                "source_file_id": fact["source_file_id"],
                "location": fact["location"],
                "decision": item.decision,
                "evidence_text": fact.get("evidence_text", ""),
                "confidence": item.confidence,
                "reason_code": item.reason_code,
            }
        )
    return FactReview.model_validate(
        {
            "file_id": payload["file_id"],
            "decisions": decisions,
            "semantic_concepts": payload.get("semantic_concepts", []),
            "validation_specs": payload.get("validation_specs", []),
            "confidence": compact.confidence,
            "evidence_complete": compact.evidence_complete,
        }
    ).model_dump(mode="json")


def review_correction_message(payload: dict[str, Any], value: Any) -> str:
    expected = {_review_identity(fact) for fact in payload.get("facts", [])}
    actual: list[tuple[object, ...]] = []
    if isinstance(value, dict) and isinstance(value.get("decisions"), list):
        for decision in value["decisions"]:
            try:
                actual.append(_review_identity(decision))
            except (AttributeError, TypeError, ValidationError, ValueError):
                continue
    missing = sorted(expected - set(actual), key=repr)
    duplicates = sorted(
        {identity for identity in actual if actual.count(identity) > 1},
        key=repr,
    )
    extras = sorted(set(actual) - expected, key=repr)
    requirements = {
        "required_decision_count": len(expected),
        "missing_candidate_identities": [_identity_payload(item) for item in missing],
        "duplicate_candidate_identities": [_identity_payload(item) for item in duplicates],
        "unexpected_candidate_identities": [_identity_payload(item) for item in extras],
    }
    return (
        "上一响应未完整覆盖输入候选。必须为每个候选身份恰好返回一个 decisions 项，"
        "不得遗漏、重复或新增。纠错要求："
        + json.dumps(requirements, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_review(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        review = FactReview.model_validate(value)
    except ValidationError as exc:
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型评审结果不符合结构约束",
            correction_message=review_correction_message(payload, value),
        ) from exc
    expected = {_review_identity(fact) for fact in payload.get("facts", [])}
    actual = [_review_identity(decision) for decision in review.decisions]
    if len(actual) != len(expected) or len(actual) != len(set(actual)) or set(actual) != expected:
        raise LlmClientError(
            "LLM_REVIEW_INCOMPLETE",
            "模型评审未完整覆盖候选事实",
            correction_message=review_correction_message(payload, value),
        )
    return review.model_dump(mode="json")


def _validate_advice(value: Any) -> dict[str, Any]:
    try:
        return AdviceResponse.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError("LLM_SCHEMA_INVALID", "模型建议结果不符合结构约束") from exc


def _validate_mapping(value: Any) -> dict[str, Any]:
    try:
        return FactMappingResponse.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型事实映射不符合结构约束",
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=_safe_validation_summary(exc),
        ) from exc


def _validate_mapping_review(value: Any) -> dict[str, Any]:
    try:
        return FactMappingReview.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        raise LlmClientError(
            "LLM_SCHEMA_INVALID",
            "模型映射评审不符合结构约束",
            failure_code="LLM_RESPONSE_SCHEMA_INVALID",
            validation_summary=_safe_validation_summary(exc),
        ) from exc


class OpenAIContractLlmClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Sleeper = asyncio.sleep,
        text_response_format_override: ResponseFormat | None = None,
        text_model_override: str | None = None,
        numeric_model_override: str | None = None,
        advice_response_format_override: ResponseFormat | None = None,
    ) -> None:
        if not settings.LLM_BASE_URL.strip() or not settings.LLM_API_KEY.strip():
            raise ValueError("LLM base URL and API key are required")
        if text_response_format_override is not None and text_response_format_override not in {
            "prompt_only",
            "json_object",
            "json_schema",
        }:
            raise ValueError("unsupported Text response format override")
        if text_model_override is not None and not text_model_override.strip():
            raise ValueError("Text model override must not be empty")
        if numeric_model_override is not None and not numeric_model_override.strip():
            raise ValueError("Numeric model override must not be empty")
        if advice_response_format_override is not None and advice_response_format_override not in {
            "prompt_only",
            "json_object",
            "json_schema",
        }:
            raise ValueError("unsupported Advice response format override")
        self.settings = settings
        self.transport = transport
        self.sleeper = sleeper
        self.text_response_format_override = text_response_format_override
        self.text_model_override = text_model_override
        self.numeric_model_override = numeric_model_override
        self.advice_response_format_override = advice_response_format_override
        self._request_semaphore = asyncio.Semaphore(
            max(1, settings.LLM_MAX_CONCURRENCY)
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        }

    async def probe_models(self) -> list[str]:
        async with self._request_semaphore:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.settings.LLM_TIMEOUT_SECONDS,
                trust_env=False,
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

    async def extract_document_profile(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=PROFILE_SYSTEM_PROMPT,
            payload=payload,
            validator=lambda value: _validate_document_overview(value, payload),
            schema=CompactDocumentOverview,
            max_structure_retries=1,
            max_output_tokens=_PROFILE_MAX_OUTPUT_TOKENS,
        )

    async def extract_fact_batch(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=FACT_BATCH_SYSTEM_PROMPT,
            payload=payload,
            validator=lambda value: _validate_fact_batch(value, payload),
            schema=CompactFactBatchExtraction,
            max_structure_retries=1,
            max_output_tokens=_numeric_max_output_tokens(payload),
        )

    async def extract_numeric_candidates(
        self, payload: dict[str, Any], *, allow_structure_correction: bool = True
    ) -> LlmResult:
        candidates = payload.get("numeric_candidates")
        if not isinstance(candidates, list) or not candidates:
            return LlmResult(
                value={"items": []},
                configured_model=(
                    self.numeric_model_override or self.settings.LLM_EXTRACTION_MODEL
                ),
                actual_model=None,
                mock=True,
                duration_ms=0,
                request_attempts=0,
                structure_retries=0,
                finish_reason=None,
                response_format=self._response_format(),
            )
        model = self.numeric_model_override or self.settings.LLM_EXTRACTION_MODEL
        try:
            return await self._structured_completion(
                model=model,
                system=NUMERIC_CANDIDATE_SYSTEM_PROMPT,
                payload=payload,
                validator=lambda value: _validate_numeric_candidates(value, payload),
                schema=NumericCandidateExtraction,
                response_schema=_numeric_candidate_response_schema(payload),
                max_structure_retries=1 if allow_structure_correction else 0,
                invalid_json_structure_correction=False,
                max_output_tokens=_numeric_max_output_tokens(payload),
                disable_thinking=True,
            )
        except LlmClientError as first_error:
            # Some gateways reject chat_template_kwargs.  Only a singleton
            # Numeric request may use the explicit compatibility fallback;
            # all other failures retain their original error and retry policy.
            if len(candidates) != 1 or first_error.http_status != 400:
                raise
            try:
                result = await self._structured_completion(
                    model=model,
                    system=NUMERIC_CANDIDATE_SYSTEM_PROMPT,
                    payload=payload,
                    validator=lambda value: _validate_numeric_candidates(value, payload),
                    schema=NumericCandidateExtraction,
                    response_schema=_numeric_candidate_response_schema(payload),
                    max_structure_retries=1 if allow_structure_correction else 0,
                    invalid_json_structure_correction=False,
                    max_output_tokens=8192,
                    disable_thinking=False,
                )
            except LlmClientError as fallback_error:
                fallback_error.request_attempts += first_error.request_attempts
                raise
            return replace(
                result,
                request_attempts=result.request_attempts + first_error.request_attempts,
            )

    async def extract_text_facts(
        self, payload: dict[str, Any], *, allow_structure_correction: bool = True
    ) -> LlmResult:
        return await self._structured_completion(
            model=self.text_model_override or self.settings.LLM_EXTRACTION_MODEL,
            system=TEXT_FACT_SYSTEM_PROMPT,
            payload=payload,
            validator=lambda value: _validate_text_facts(value, payload),
            schema=TextFactExtraction,
            response_schema=_text_fact_response_schema(payload),
            response_format_override=self.text_response_format_override,
            max_structure_retries=1 if allow_structure_correction else 0,
            allow_evidence_correction=allow_structure_correction,
            invalid_json_structure_correction=False,
            max_output_tokens=_TEXT_MAX_OUTPUT_TOKENS,
        )

    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=payload,
            validator=lambda value: _validate_compact_extraction(value, payload),
            schema=CompactDocumentFactExtraction,
            max_structure_retries=min(self.settings.LLM_STRUCTURE_RETRY_ATTEMPTS, 1),
        )

    async def plan_semantics(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_EXTRACTION_MODEL,
            system=SEMANTIC_PLAN_SYSTEM_PROMPT,
            payload=payload,
            validator=lambda value: _validate_compact_semantic_plan(value, payload),
            schema=CompactSemanticPlanResponse,
        )

    async def review_facts(self, payload: dict[str, Any]) -> LlmResult:
        wire_payload = compact_review_payload(payload)
        response_schema = compact_review_response_schema(wire_payload)
        return await self._structured_completion(
            model=self.settings.LLM_REVIEW_MODEL,
            system=(
                REVIEW_SYSTEM_PROMPT
                + "评审响应使用紧凑协议：仅返回每个事实的 fact_index、decision、confidence、"
                "reason_code；不要返回文件身份、位置或证据文本。"
            ),
            payload=wire_payload,
            validator=lambda value: _validate_compact_review(value, payload),
            schema=CompactFactReview,
            response_schema=response_schema,
            max_structure_retries=1,
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
            max_output_tokens=_MAPPING_MAX_OUTPUT_TOKENS,
        )

    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_REVIEW_MODEL,
            system=MAPPING_REVIEW_SYSTEM_PROMPT,
            payload=payload,
            validator=_validate_mapping_review,
            schema=FactMappingReview,
            max_output_tokens=_MAPPING_MAX_OUTPUT_TOKENS,
        )

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        return await self._structured_completion(
            model=self.settings.LLM_ADVICE_MODEL,
            system=ADVICE_SYSTEM_PROMPT,
            payload=payload,
            validator=_validate_advice,
            schema=AdviceResponse,
            response_format_override=self.advice_response_format_override,
            max_output_tokens=_ADVICE_MAX_OUTPUT_TOKENS,
        )

    async def _structured_completion(
        self,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        validator: JsonValidator,
        schema: type[BaseModel],
        response_schema: dict[str, Any] | None = None,
        response_format_override: ResponseFormat | None = None,
        max_structure_retries: int | None = None,
        max_output_tokens: int | None = None,
        allow_structure_correction: bool = True,
        allow_evidence_correction: bool = True,
        invalid_json_structure_correction: bool = True,
        disable_thinking: bool = False,
    ) -> LlmResult:
        async with self._request_semaphore:
            return await self._structured_completion_unbounded(
                model=model,
                system=system,
                payload=payload,
                validator=validator,
                schema=schema,
                response_schema=response_schema,
                response_format_override=response_format_override,
                max_structure_retries=max_structure_retries,
                max_output_tokens=max_output_tokens,
                allow_structure_correction=allow_structure_correction,
                allow_evidence_correction=allow_evidence_correction,
                invalid_json_structure_correction=invalid_json_structure_correction,
                disable_thinking=disable_thinking,
            )

    async def _structured_completion_unbounded(
        self,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        validator: JsonValidator,
        schema: type[BaseModel],
        response_schema: dict[str, Any] | None = None,
        response_format_override: ResponseFormat | None = None,
        max_structure_retries: int | None = None,
        max_output_tokens: int | None = None,
        allow_structure_correction: bool = True,
        allow_evidence_correction: bool = True,
        invalid_json_structure_correction: bool = True,
        disable_thinking: bool = False,
    ) -> LlmResult:
        started = time.perf_counter()
        request_attempts = 0
        last_error: LlmClientError | None = None
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings.LLM_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            retry_limit = (
                self.settings.LLM_STRUCTURE_RETRY_ATTEMPTS
                if max_structure_retries is None
                else max_structure_retries
            )
            if not allow_structure_correction:
                retry_limit = 0
            for structure_attempt in range(retry_limit + 1):
                response_format = response_format_override or self._response_format()
                body = completion_body(
                    model=model,
                    system=system,
                    payload=payload,
                    schema=schema,
                    max_tokens=(
                        max_output_tokens
                        if max_output_tokens is not None
                        else self.settings.LLM_MAX_OUTPUT_TOKENS
                    ),
                    response_format=response_format,
                    correction=bool(structure_attempt),
                    correction_message=(
                        last_error.correction_message if last_error is not None else None
                    ),
                    response_schema=response_schema,
                    disable_thinking=disable_thinking,
                )
                try:
                    response, attempts = await self._request_with_count(
                        client,
                        "POST",
                        _endpoint(self.settings.LLM_BASE_URL, "chat/completions"),
                        json=body,
                    )
                except LlmClientError as exc:
                    exc.request_attempts = request_attempts + exc.request_attempts
                    exc.structure_retries = structure_attempt
                    raise
                request_attempts += attempts
                try:
                    response_body = response.json()
                    choice = response_body["choices"][0]
                    finish_reason = choice.get("finish_reason")
                    message = choice.get("message")
                    if not isinstance(message, dict):
                        raise LlmClientError(
                            "LLM_RESPONSE_INVALID", "模型响应缺少消息对象"
                        )
                    content = message.get("content")
                    reasoning_content = message.get("reasoning_content")
                    content_chars = len(content) if isinstance(content, str) else 0
                    reasoning_content_chars = (
                        len(reasoning_content)
                        if isinstance(reasoning_content, str)
                        else 0
                    )
                    usage = _safe_usage_summary(response_body.get("usage"))
                    actual_max_tokens = (
                        max_output_tokens
                        if max_output_tokens is not None
                        else self.settings.LLM_MAX_OUTPUT_TOKENS
                    )
                    code_fence = isinstance(content, str) and "```" in content
                    if finish_reason == "length":
                        raise LlmClientError(
                            "LLM_OUTPUT_TRUNCATED",
                            "模型输出达到长度上限",
                            finish_reason="length",
                            content_chars=content_chars,
                            code_fence=code_fence,
                            reasoning_content_chars=reasoning_content_chars,
                            usage=usage,
                            max_tokens=actual_max_tokens,
                        )
                    if not isinstance(content, str):
                        raise LlmClientError(
                            "LLM_RESPONSE_INVALID",
                            "模型响应缺少文本内容",
                            content_chars=content_chars,
                            code_fence=code_fence,
                            reasoning_content_chars=reasoning_content_chars,
                            usage=usage,
                            max_tokens=actual_max_tokens,
                        )
                    try:
                        value = validator(_json_object(content))
                    except LlmClientError as exc:
                        exc.content_chars = content_chars
                        exc.code_fence = code_fence
                        exc.reasoning_content_chars = reasoning_content_chars
                        exc.usage = usage
                        exc.max_tokens = actual_max_tokens
                        if exc.finish_reason is None:
                            exc.finish_reason = finish_reason
                        raise
                    return LlmResult(
                        value=value,
                        configured_model=model,
                        actual_model=response_body.get("model"),
                        mock=False,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        request_attempts=request_attempts,
                        structure_retries=structure_attempt,
                        finish_reason=finish_reason,
                        response_format=response_format,
                        response_metadata={
                            "content_chars": content_chars,
                            "code_fence": code_fence,
                            "json_error_position": None,
                            "reasoning_content_chars": reasoning_content_chars,
                            "usage": usage,
                            "max_tokens": actual_max_tokens,
                        },
                    )
                except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                    last_error = LlmClientError("LLM_RESPONSE_INVALID", "模型响应结构无效")
                    last_error.__cause__ = exc
                except LlmClientError as exc:
                    # Keep the first validated failure reason when a single
                    # allowed correction itself is malformed.  This preserves
                    # FACT_* evidence subcodes in safe diagnostics instead of
                    # collapsing them into the later transport/JSON wrapper.
                    if (
                        exc.failure_code is None
                        and last_error is not None
                        and last_error.failure_code is not None
                    ):
                        exc.failure_code = last_error.failure_code
                    last_error = exc
                    exc.request_attempts = request_attempts
                    exc.structure_retries = structure_attempt
                    if exc.max_tokens is None:
                        exc.max_tokens = (
                            max_output_tokens
                            if max_output_tokens is not None
                            else self.settings.LLM_MAX_OUTPUT_TOKENS
                        )
                    if isinstance(exc, LlmClientError) and exc.code == "LLM_OUTPUT_TRUNCATED":
                        exc.finish_reason = "length"
                        exc.request_attempts = request_attempts
                        raise exc
                    if not self._can_structure_correct(
                        exc,
                        finish_reason=finish_reason,
                        structure_attempt=structure_attempt,
                        retry_limit=retry_limit,
                        allow_evidence_correction=allow_evidence_correction,
                        invalid_json_structure_correction=invalid_json_structure_correction,
                    ):
                        raise exc
                    continue
        if last_error is not None:
            last_error.request_attempts = request_attempts
            last_error.structure_retries = retry_limit
            raise last_error
        raise LlmClientError("LLM_RESPONSE_INVALID", "模型响应结构无效")

    def _response_format(self) -> str:
        if self.settings.LLM_NATIVE_STRUCTURED_OUTPUT:
            return "json_schema"
        return getattr(self.settings, "LLM_RESPONSE_FORMAT", "prompt_only")

    @staticmethod
    def _can_structure_correct(
        error: LlmClientError,
        *,
        finish_reason: str | None,
        structure_attempt: int,
        retry_limit: int,
        allow_evidence_correction: bool = True,
        invalid_json_structure_correction: bool = True,
    ) -> bool:
        if structure_attempt >= retry_limit:
            return False
        if error.code == "LLM_INVALID_JSON":
            return invalid_json_structure_correction and finish_reason in {None, "stop"}
        if error.failure_code == "FACT_BATCH_SATURATED":
            return False
        if error.code == "LLM_EXTRACTION_EVIDENCE_INVALID" and not allow_evidence_correction:
            return False
        return error.code in {
            "LLM_SCHEMA_INVALID",
            "LLM_REVIEW_INCOMPLETE",
            "LLM_EXTRACTION_EVIDENCE_INVALID",
        } and bool(error.correction_message)

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
        max_retries = getattr(self.settings, "LLM_HTTP_RETRY_ATTEMPTS", _HTTP_MAX_RETRIES)
        max_retries = max(0, min(int(max_retries), _HTTP_MAX_RETRIES))
        max_attempts = max_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers,
                    **kwargs,
                )
            except httpx.TimeoutException as exc:
                if attempt < _HTTP_TIMEOUT_MAX_ATTEMPTS:
                    await self.sleeper(0.5 * attempt)
                    continue
                raise LlmClientError(
                    "LLM_TIMEOUT",
                    "模型服务请求超时",
                    retryable=True,
                    request_attempts=attempt,
                ) from exc
            except httpx.RequestError as exc:
                raise LlmClientError(
                    "LLM_NETWORK_ERROR",
                    "模型服务网络请求失败",
                    retryable=False,
                    request_attempts=attempt,
                ) from exc
            if response.status_code < 400:
                return response, attempt
            code, message, retryable = self._http_error(response.status_code)
            if (
                response.status_code in _HTTP_RETRYABLE_STATUSES
                and attempt < max_attempts
            ):
                backoff_index = min(attempt - 1, len(_HTTP_RETRY_BACKOFF_SECONDS) - 1)
                backoff = _HTTP_RETRY_BACKOFF_SECONDS[backoff_index]
                await self.sleeper(backoff + random.uniform(0.0, 0.25))
                continue
            raise LlmClientError(
                code,
                message,
                retryable=retryable,
                request_attempts=attempt,
                http_status=response.status_code,
            )
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
