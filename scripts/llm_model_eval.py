from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.llm.openai_client import (
    ADVICE_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    MAPPING_REVIEW_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    completion_body,
    review_correction_message,
    review_response_schema,
)
from app.adapters.llm.schemas import (
    AdviceResponse,
    CompactDocumentFactExtraction,
    DocumentFactExtraction,
    FactMappingReview,
    FactReview,
)
from app.core.config import Settings
from app.documents.models import DocumentLocation
from app.documents.normalization import normalize_text
from app.documents.parsers import DocxParser
from app.draft_review.facts import (
    compact_extraction_payload,
    expand_compact_extraction,
    location_key,
    validate_extraction_evidence,
)
from app.results.advice import advice_payload, merge_model_advice
from app.services.downloader import DOCX_MIME, LocalFile

TEXT_MODELS = ("Qwen3.8-27B", "MiniMax-M2.7", "DeepSeek-V4-Flash-0731")
DEEPSEEK_MODEL = "DeepSeek-V4-Flash-0731"
ResponseMode = Literal["prompt_only", "json_object", "json_schema"]
Role = Literal["extraction", "review", "mapping_review", "advice"]


@dataclass
class CallBudget:
    limit: int = 44
    used: int = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError("LLM evaluation call budget exhausted")
        self.used += 1


@dataclass
class Attempt:
    model: str
    role: Role
    response_mode: ResponseMode
    correction: int
    http_status: int | None = None
    first_byte_ms: int | None = None
    total_ms: int = 0
    actual_model: str | None = None
    finish_reason: str | None = None
    content_length: int = 0
    request_body_chars: int = 0
    schema_chars: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    request_metrics: dict[str, Any] = field(default_factory=dict, repr=False)
    strict_json: bool = False
    schema_valid: bool = False
    fenced: bool = False
    explanatory_prefix: bool = False
    truncated: bool = False
    error_code: str | None = None
    value: BaseModel | None = field(default=None, repr=False)
    raw_value: Any = field(default=None, repr=False)
    output_metrics: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def transport_ok(self) -> bool:
        return self.http_status is not None and self.http_status < 400

    @property
    def base_compliant(self) -> bool:
        return (
            self.transport_ok
            and self.actual_model == self.model
            and self.strict_json
            and self.schema_valid
            and not self.fenced
            and not self.explanatory_prefix
            and not self.truncated
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "role": self.role,
            "response_mode": self.response_mode,
            "correction": self.correction,
            "http_status": self.http_status,
            "first_byte_ms": self.first_byte_ms,
            "total_ms": self.total_ms,
            "actual_model": self.actual_model,
            "finish_reason": self.finish_reason,
            "content_length": self.content_length,
            "request_body_chars": self.request_body_chars,
            "schema_chars": self.schema_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_metrics": self.request_metrics,
            "strict_json": self.strict_json,
            "schema_valid": self.schema_valid,
            "fenced": self.fenced,
            "explanatory_prefix": self.explanatory_prefix,
            "truncated": self.truncated,
            "error_code": self.error_code,
            "output_metrics": self.output_metrics,
        }


@dataclass
class LogicalResult:
    model: str
    role: Role
    response_mode: ResponseMode
    attempts: list[Attempt]
    quality_score: float = 0.0
    evidence_valid: bool = False
    identity_valid: bool = False
    fingerprint: str | None = None
    detail_counts: dict[str, int | float | bool] = field(default_factory=dict)

    @property
    def final(self) -> Attempt:
        return self.attempts[-1]

    @property
    def passed(self) -> bool:
        return (
            self.final.base_compliant
            and self.evidence_valid
            and self.identity_valid
            and self.quality_score >= 100
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "role": self.role,
            "response_mode": self.response_mode,
            "passed": self.passed,
            "quality_score": round(self.quality_score, 2),
            "evidence_valid": self.evidence_valid,
            "identity_valid": self.identity_valid,
            "fingerprint": self.fingerprint,
            "detail_counts": self.detail_counts,
            "attempts": [attempt.safe_dict() for attempt in self.attempts],
        }


def endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    prefix = base if base.endswith("/v1") else f"{base}/v1"
    return f"{prefix}/chat/completions"


def canonical_location(value: DocumentLocation | dict[str, Any]) -> str:
    if isinstance(value, DocumentLocation):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fact_eval_id(fact: Any) -> str:
    parts = (
        fact.source_file_id,
        fact.field_key,
        fact.value_type,
        canonical_location(fact.location),
        fact.raw_value,
        fact.evidence_text,
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def safe_fingerprint(parts: list[str]) -> str:
    return hashlib.sha256("\x1e".join(sorted(parts)).encode("utf-8")).hexdigest()[:20]


def safe_value_metrics(value: Any) -> dict[str, Any]:
    """Summarize structure only; never return strings or model values."""

    array_counts: dict[str, int] = {}
    string_max_lengths: dict[str, int] = {}
    ast_node_count = 0
    ast_depth = 0

    def visit(node: Any, path: str, depth: int = 0) -> int:
        nonlocal ast_node_count, ast_depth
        if isinstance(node, dict):
            if "op" in node:
                ast_node_count += 1
                ast_depth = max(ast_depth, depth)
            for key, child in node.items():
                visit(child, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(node, list):
            array_counts[path] = len(node)
            for child in node:
                visit(child, f"{path}[]", depth)
        elif isinstance(node, str):
            string_max_lengths[path] = max(string_max_lengths.get(path, 0), len(node))
        return 0

    visit(value, "")
    return {
        "array_counts": array_counts,
        "string_max_lengths": string_max_lengths,
        "ast_node_count": ast_node_count,
        "ast_depth": ast_depth,
    }


async def send_attempt(
    settings: Settings,
    budget: CallBudget,
    *,
    model: str,
    role: Role,
    response_mode: ResponseMode,
    system: str,
    payload: dict[str, Any],
    schema: type[BaseModel],
    correction: int,
    max_tokens: int | None = None,
    response_schema: dict[str, Any] | None = None,
    correction_message: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Attempt:
    budget.take()
    attempt = Attempt(
        model=model,
        role=role,
        response_mode=response_mode,
        correction=correction,
    )
    body = completion_body(
        model=model,
        system=system,
        payload=payload,
        schema=schema,
        max_tokens=max_tokens or settings.LLM_MAX_OUTPUT_TOKENS,
        response_format=response_mode,
        correction=bool(correction),
        correction_message=correction_message,
        response_schema=response_schema,
    )
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    attempt.request_body_chars = len(body_json)
    if role == "extraction":
        candidate_metrics = payload.get("numeric_candidate_metrics")
        if isinstance(candidate_metrics, dict):
            attempt.request_metrics = {
                key: candidate_metrics.get(key)
                for key in (
                    "candidate_total",
                    "candidate_unique",
                    "suppressed_count",
                    "structural_suppressed_count",
                    "duplicate_suppressed_count",
                    "type_counts",
                    "batch_count",
                )
                if key in candidate_metrics
            }
        elif isinstance(payload.get("numeric_candidates"), list):
            attempt.request_metrics = {
                "candidate_unique": len(payload["numeric_candidates"]),
                "batch_count": 1,
            }
    schema_value = body.get("response_format", {}).get("json_schema", {}).get("schema")
    if schema_value is not None:
        attempt.schema_chars = len(
            json.dumps(schema_value, ensure_ascii=False, separators=(",", ":"))
        )
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            request = client.build_request(
                "POST",
                endpoint(settings.LLM_BASE_URL),
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response = await client.send(request, stream=True)
            attempt.http_status = response.status_code
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if chunk and attempt.first_byte_ms is None:
                    attempt.first_byte_ms = round((time.perf_counter() - started) * 1000)
                content.extend(chunk)
            await response.aclose()
    except httpx.TimeoutException:
        attempt.error_code = "TIMEOUT"
        attempt.total_ms = round((time.perf_counter() - started) * 1000)
        print(
            json.dumps({"event": "attempt", **attempt.safe_dict()}, ensure_ascii=False), flush=True
        )
        return attempt
    except httpx.RequestError:
        attempt.error_code = "NETWORK_ERROR"
        attempt.total_ms = round((time.perf_counter() - started) * 1000)
        print(
            json.dumps({"event": "attempt", **attempt.safe_dict()}, ensure_ascii=False), flush=True
        )
        return attempt
    attempt.total_ms = round((time.perf_counter() - started) * 1000)
    attempt.content_length = len(content)
    if attempt.http_status is None or attempt.http_status >= 400:
        attempt.error_code = f"HTTP_{attempt.http_status}"
        print(
            json.dumps({"event": "attempt", **attempt.safe_dict()}, ensure_ascii=False), flush=True
        )
        return attempt
    try:
        outer = json.loads(content)
        attempt.actual_model = outer.get("model")
        usage = outer.get("usage")
        if isinstance(usage, dict):
            for key, attribute in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = usage.get(key)
                if isinstance(value, int):
                    setattr(attempt, attribute, value)
        choice = outer["choices"][0]
        attempt.finish_reason = choice.get("finish_reason")
        attempt.truncated = attempt.finish_reason == "length"
        model_content = choice["message"]["content"]
        if not isinstance(model_content, str):
            raise TypeError("model content is not text")
        stripped = model_content.strip()
        attempt.fenced = bool(re.fullmatch(r"```(?:json)?\s*.*?\s*```", stripped, re.I | re.S))
        attempt.explanatory_prefix = not stripped.startswith("{")
        parsed = json.loads(stripped)
        attempt.raw_value = parsed
        attempt.strict_json = isinstance(parsed, dict)
        if attempt.strict_json:
            if role == "extraction":
                compact = CompactDocumentFactExtraction.model_validate(parsed)
                attempt.value = expand_compact_extraction(payload, compact)
            else:
                attempt.value = schema.model_validate(parsed)
            attempt.schema_valid = True
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        attempt.error_code = "INVALID_JSON_OR_RESPONSE"
    except (ValidationError, ValueError):
        attempt.error_code = "SCHEMA_INVALID"
    if attempt.schema_valid and attempt.value is not None:
        attempt.output_metrics = safe_value_metrics(attempt.value.model_dump(mode="json"))
    print(json.dumps({"event": "attempt", **attempt.safe_dict()}, ensure_ascii=False), flush=True)
    return attempt


def extraction_fixture() -> dict[str, Any]:
    payload = {
        "file_id": "fil_synthetic_reference",
        "role": "REFERENCE",
        "blocks": [
            {
                "block_id": "syn_p000000",
                "type": "PARAGRAPH",
                "text": "项目审批摘要",
                "location": {"paragraph_index": 0},
            },
            {
                "block_id": "syn_p000001",
                "type": "PARAGRAPH",
                "text": "批准主体为甲公司，项目编号为SYN-2026-001。",
                "location": {"paragraph_index": 1},
            },
            {
                "block_id": "syn_p000002",
                "type": "PARAGRAPH",
                "text": "融资金额为人民币1200万元，租赁期限24个月，年利率4.25%。",
                "location": {"paragraph_index": 2},
            },
            {
                "block_id": "syn_p000003",
                "type": "PARAGRAPH",
                "text": "方案日期为2026年8月20日，设备数量8台，首付款比例10%。",
                "location": {"paragraph_index": 3},
            },
        ],
    }
    payload["evidence_blocks"] = list(payload["blocks"])
    payload["numeric_candidates"] = [
        {
            "candidate_id": f"numeric_{index:04d}",
            "raw_value": raw_value,
            "location": {"paragraph_index": paragraph_index},
        }
        for index, (raw_value, paragraph_index) in enumerate(
            (
                ("2026", 1),
                ("001", 1),
                ("1200万元", 2),
                ("24个月", 2),
                ("4.25%", 2),
                ("2026年8月20日", 3),
                ("8台", 3),
                ("10%", 3),
            ),
            start=1,
        )
    ]
    return payload


def review_fixture() -> tuple[dict[str, Any], dict[tuple[Any, ...], set[str]]]:
    blocks = extraction_fixture()["blocks"] + [
        {
            "block_id": "syn_p000004",
            "type": "PARAGRAPH",
            "text": "另一处材料记载租赁期限36个月。",
            "location": {"paragraph_index": 4},
        }
    ]
    facts = [
        {
            "field_key": "financing_amount",
            "display_name": "融资金额",
            "value_type": "MONEY",
            "raw_value": "人民币1200万元",
            "normalized_hint": "12000000",
            "source_file_id": "fil_synthetic_reference",
            "evidence_text": blocks[2]["text"],
            "location": {"paragraph_index": 2},
            "confidence": 0.95,
        },
        {
            "field_key": "mismatched_date",
            "display_name": "日期",
            "value_type": "DATE",
            "raw_value": "2026年8月20日",
            "normalized_hint": "2026-08-20",
            "source_file_id": "fil_synthetic_reference",
            "evidence_text": blocks[1]["text"],
            "location": {"paragraph_index": 3},
            "confidence": 0.8,
        },
        {
            "field_key": "altered_quantity",
            "display_name": "设备数量",
            "value_type": "QUANTITY",
            "raw_value": "9台",
            "normalized_hint": "9",
            "source_file_id": "fil_synthetic_reference",
            "evidence_text": blocks[3]["text"],
            "location": {"paragraph_index": 3},
            "confidence": 0.8,
        },
        {
            "field_key": "lease_term_primary",
            "display_name": "租赁期限",
            "value_type": "DURATION",
            "raw_value": "24个月",
            "normalized_hint": "24 months",
            "source_file_id": "fil_synthetic_reference",
            "evidence_text": blocks[2]["text"],
            "location": {"paragraph_index": 2},
            "confidence": 0.8,
        },
        {
            "field_key": "lease_term_conflict",
            "display_name": "租赁期限",
            "value_type": "DURATION",
            "raw_value": "36个月",
            "normalized_hint": "36 months",
            "source_file_id": "fil_synthetic_reference",
            "evidence_text": blocks[4]["text"],
            "location": {"paragraph_index": 4},
            "confidence": 0.8,
        },
    ]
    payload = {
        "file_id": "fil_synthetic_reference",
        "role": "REFERENCE",
        "blocks": blocks,
        "facts": facts,
        "semantic_concepts": [],
        "validation_specs": [],
        "review_requirements": {
            "required_decision_count": len(facts),
            "one_decision_per_fact": True,
            "evaluate_each_fact_independently": True,
        },
    }
    expected = {
        ("financing_amount", "fil_synthetic_reference", location_key({"paragraph_index": 2})): {
            "ACCEPT"
        },
        ("mismatched_date", "fil_synthetic_reference", location_key({"paragraph_index": 3})): {
            "REJECT",
            "UNCERTAIN",
        },
        ("altered_quantity", "fil_synthetic_reference", location_key({"paragraph_index": 3})): {
            "REJECT",
            "UNCERTAIN",
        },
        ("lease_term_primary", "fil_synthetic_reference", location_key({"paragraph_index": 2})): {
            "ACCEPT"
        },
        ("lease_term_conflict", "fil_synthetic_reference", location_key({"paragraph_index": 4})): {
            "ACCEPT"
        },
    }
    return payload, expected


def mapping_review_fixture() -> dict[str, Any]:
    target = {
        "target_fact_id": "target_fact_000001",
        "field_key": "financing_amount",
        "display_name": "融资金额",
        "value_type": "MONEY",
        "raw_value": "人民币1200万元",
        "normalized_hint": "12000000",
        "source_file_id": "fil_target",
        "evidence_text": "融资金额为人民币1200万元。",
        "location": {"paragraph_index": 1},
        "confidence": 0.95,
    }
    reference = {
        "field_key": "approved_amount",
        "display_name": "批复金额",
        "value_type": "MONEY",
        "raw_value": "人民币1500万元",
        "normalized_hint": "15000000",
        "source_file_id": "fil_reference",
        "evidence_text": "批复金额为人民币1500万元。",
        "location": {"paragraph_index": 2},
        "confidence": 0.95,
    }
    proposal = {
        "target_fact_id": "target_fact_000001",
        "reference_field_key": "approved_amount",
        "source_file_id": "fil_reference",
        "reference_location": {"paragraph_index": 2},
        "decision": "MATCH",
        "confidence": 0.95,
        "reason_code": "SAME_AMOUNT",
    }
    return {
        "reference_file_id": "fil_reference",
        "reference_profile": {
            "file_id": "fil_reference",
            "document_kind": "审批摘要",
            "title": "合成审批摘要",
            "confidence": 0.95,
            "evidence_locations": [{"paragraph_index": 0}],
        },
        "target_facts": [target],
        "reference_facts": [reference],
        "proposed_mapping": {
            "reference_file_id": "fil_reference",
            "mappings": [proposal],
            "missing_requirements": [],
        },
    }


def advice_fixture() -> tuple[dict[str, Any], dict[str, tuple[str, ...]]]:
    result = {
        "files": [
            {"file_id": "fil_base", "file_name": "基准文件.docx", "role": "BASELINE"},
            {"file_id": "fil_target", "file_name": "当前文件.docx", "role": "TARGET"},
        ],
        "risk_items": [
            {
                "risk_id": "risk_term_change",
                "risk_type": "ADDITION_OR_CHANGE",
                "title": "租赁期限发生变化",
                "description": "期限由24个月调整为36个月。",
                "related_diff_ids": ["diff_term"],
                "source_evidence": [],
            },
            {
                "risk_id": "risk_guarantee_missing",
                "risk_type": "DELETION_OR_MISSING",
                "title": "担保条款缺失",
                "description": "当前文本未保留担保约定。",
                "related_diff_ids": ["diff_guarantee"],
                "source_evidence": [],
            },
            {
                "risk_id": "risk_payment_ratio",
                "risk_type": "ADDITION_OR_CHANGE",
                "title": "首付款比例发生变化",
                "description": "首付款比例由10%调整为15%。",
                "related_diff_ids": ["diff_ratio"],
                "source_evidence": [],
            },
        ],
        "diff_items": [
            {
                "diff_id": "diff_term",
                "title": "租赁期限变化",
                "baseline": {
                    "file_id": "fil_base",
                    "text": "租赁期限24个月",
                    "location": {"paragraph_index": 1},
                },
                "target": {
                    "file_id": "fil_target",
                    "text": "租赁期限36个月",
                    "location": {"paragraph_index": 1},
                },
                "segments": [],
            },
            {
                "diff_id": "diff_guarantee",
                "title": "担保条款缺失",
                "baseline": {
                    "file_id": "fil_base",
                    "text": "担保约定",
                    "location": {"paragraph_index": 2},
                },
                "target": None,
                "segments": [],
            },
            {
                "diff_id": "diff_ratio",
                "title": "首付款比例变化",
                "baseline": {
                    "file_id": "fil_base",
                    "text": "首付款比例10%",
                    "location": {"paragraph_index": 3},
                },
                "target": {
                    "file_id": "fil_target",
                    "text": "首付款比例15%",
                    "location": {"paragraph_index": 3},
                },
                "segments": [],
            },
        ],
    }
    anchors = {
        "risk_term_change": ("期限", "24", "36"),
        "risk_guarantee_missing": ("担保", "缺失", "补充"),
        "risk_payment_ratio": ("首付", "比例", "10", "15"),
    }
    return result, anchors


def source_by_location(payload: dict[str, Any]) -> dict[tuple[Any, ...], str]:
    return {
        location_key(block["location"]): normalize_text(block["text"])
        for block in payload.get("evidence_blocks", payload.get("blocks", []))
    }


def assess_extraction(
    result: LogicalResult, payload: dict[str, Any], *, real: bool = False
) -> None:
    value = result.final.value
    if not isinstance(value, DocumentFactExtraction):
        return
    sources = source_by_location(payload)
    valid = value.profile.file_id == payload["file_id"] and all(
        location_key(location) in sources for location in value.profile.evidence_locations
    )
    ids: list[str] = []
    value_types: set[str] = set()
    for fact in value.facts:
        source = sources.get(location_key(fact.location), "")
        evidence = normalize_text(fact.evidence_text)
        raw = normalize_text(fact.raw_value)
        valid = valid and fact.source_file_id == payload["file_id"]
        valid = valid and bool(source) and evidence in source and raw in evidence and raw in source
        ids.append(fact_eval_id(fact))
        value_types.add(fact.value_type)
    for concept in value.semantic_concepts:
        for location in concept.evidence_locations:
            valid = valid and location_key(location) in sources
    for spec in value.validation_specs:
        for location in spec.evidence_locations:
            valid = valid and location_key(location) in sources
    identity_valid = len(ids) == len(set(ids))
    expected_types = {"MONEY", "DATE", "DURATION", "QUANTITY"}
    has_rate = bool(value_types & {"RATE", "PERCENTAGE"})
    if real:
        coverage = 1.0 if value.facts else 0.0
    else:
        coverage = (len(value_types & expected_types) + int(has_rate)) / 5
    result.evidence_valid = valid
    result.identity_valid = identity_valid
    result.quality_score = 100 * coverage if valid and identity_valid else 0
    result.fingerprint = safe_fingerprint(ids)
    result.detail_counts = {
        "fact_count": len(value.facts),
        "unique_fact_eval_ids": len(set(ids)),
        "value_type_count": len(value_types),
        "coverage": round(coverage, 4),
    }


def assess_real_extraction(result: LogicalResult, payload: dict[str, Any], document: Any) -> None:
    assess_extraction(result, payload, real=True)
    if isinstance(result.final.value, DocumentFactExtraction):
        try:
            validate_extraction_evidence(document, result.final.value)
        except ValueError:
            result.evidence_valid = False
            result.quality_score = 0


def review_identity(fact: dict[str, Any] | Any) -> tuple[Any, ...]:
    if isinstance(fact, dict):
        return (fact["field_key"], fact["source_file_id"], location_key(fact["location"]))
    return (fact.field_key, fact.source_file_id, location_key(fact.location))


def assess_review(
    result: LogicalResult,
    payload: dict[str, Any],
    expected: dict[tuple[Any, ...], set[str]] | None,
) -> None:
    value = result.final.value
    if not isinstance(value, FactReview):
        return
    candidates = {review_identity(fact): fact for fact in payload["facts"]}
    decisions: dict[tuple[Any, ...], Any] = {}
    evidence_valid = value.file_id == payload["file_id"]
    correct = 0
    for decision in value.decisions:
        key = review_identity(decision)
        candidate = candidates.get(key)
        if key in decisions or candidate is None:
            evidence_valid = False
            continue
        decisions[key] = decision
        if decision.evidence_text and normalize_text(decision.evidence_text) not in normalize_text(
            candidate["evidence_text"]
        ):
            evidence_valid = False
        if expected is None or decision.decision in expected[key]:
            correct += 1
    identity_valid = set(decisions) == set(candidates)
    evidence_valid = evidence_valid and identity_valid
    score = 100 * correct / max(1, len(candidates))
    parts = [f"{key!r}:{decision.decision}" for key, decision in decisions.items()]
    result.evidence_valid = evidence_valid
    result.identity_valid = identity_valid
    result.quality_score = score if evidence_valid and identity_valid else 0
    result.fingerprint = safe_fingerprint(parts)
    result.detail_counts = {
        "candidate_count": len(candidates),
        "decision_count": len(decisions),
        "correct_decisions": correct,
    }


def assess_mapping_review(result: LogicalResult, payload: dict[str, Any]) -> None:
    value = result.final.value
    if not isinstance(value, FactMappingReview):
        return
    proposals = payload["proposed_mapping"]["mappings"]
    expected = {
        (
            item["target_fact_id"],
            item["reference_field_key"],
            item["source_file_id"],
            location_key(item["reference_location"]),
        )
        for item in proposals
    }
    actual = {
        (
            item.target_fact_id,
            item.reference_field_key,
            item.source_file_id,
            location_key(item.reference_location),
        ): item.decision
        for item in value.decisions
    }
    identity_valid = (
        set(actual) == expected and value.reference_file_id == payload["reference_file_id"]
    )
    decision_valid = bool(actual) and all(
        item in {"REJECT", "UNCERTAIN"} for item in actual.values()
    )
    result.evidence_valid = decision_valid
    result.identity_valid = identity_valid
    result.quality_score = 100 if identity_valid and decision_valid else 0
    result.fingerprint = safe_fingerprint([f"{key!r}:{value}" for key, value in actual.items()])
    result.detail_counts = {"proposal_count": len(expected), "decision_count": len(actual)}


def assess_advice(
    result: LogicalResult,
    source_result: dict[str, Any],
    anchors: dict[str, tuple[str, ...]],
) -> None:
    value = result.final.value
    if not isinstance(value, AdviceResponse):
        return
    expected_ids = set(anchors)
    actual_ids = [item.risk_id for item in value.risk_advices]
    identity_valid = len(actual_ids) == len(set(actual_ids)) and set(actual_ids) == expected_ids
    technical = {
        *(item["file_id"] for item in source_result["files"]),
        *(item["diff_id"] for item in source_result["diff_items"]),
        *TEXT_MODELS,
        "paragraph_index",
        "table_index",
    }
    texts = [item.analysis_advice for item in value.risk_advices]
    safe_text = all(not any(token in text for token in technical) for text in texts)
    distinct = len({normalize_text(text) for text in texts}) == len(texts)
    specific = all(
        any(anchor in item.analysis_advice for anchor in anchors[item.risk_id])
        for item in value.risk_advices
        if item.risk_id in anchors
    )
    merge_valid = True
    try:
        merge_model_advice(deepcopy(source_result), value)
    except ValueError:
        merge_valid = False
    result.evidence_valid = safe_text and distinct and specific and merge_valid
    result.identity_valid = identity_valid
    result.quality_score = 100 if result.evidence_valid and identity_valid else 0
    result.fingerprint = safe_fingerprint(
        [
            f"{item.risk_id}:{hashlib.sha256(item.analysis_advice.encode()).hexdigest()}"
            for item in value.risk_advices
        ]
    )
    result.detail_counts = {
        "expected_risks": len(expected_ids),
        "returned_risks": len(actual_ids),
        "distinct_advices": len(set(texts)),
        "specific": specific,
        "safe_text": safe_text,
    }


async def evaluate_logical(
    settings: Settings,
    budget: CallBudget,
    *,
    model: str,
    role: Role,
    response_mode: ResponseMode,
    system: str,
    payload: dict[str, Any],
    schema: type[BaseModel],
    allow_correction: bool,
    assessor: Any,
    response_schema: dict[str, Any] | None = None,
    correct_identity_failure: bool = False,
    max_tokens: int | None = None,
) -> LogicalResult:
    attempts = [
        await send_attempt(
            settings,
            budget,
            model=model,
            role=role,
            response_mode=response_mode,
            system=system,
            payload=payload,
            schema=schema,
            correction=0,
            response_schema=response_schema,
            max_tokens=max_tokens,
        )
    ]
    first = attempts[0]
    logical = LogicalResult(model=model, role=role, response_mode=response_mode, attempts=attempts)
    assessor(logical)
    if (
        allow_correction
        and first.transport_ok
        and (
            not first.strict_json
            or not first.schema_valid
            or (correct_identity_failure and not logical.identity_valid)
        )
        and not first.truncated
    ):
        correction_message = (
            review_correction_message(payload, first.raw_value)
            if role == "review"
            else None
        )
        attempts.append(
            await send_attempt(
                settings,
                budget,
                model=model,
                role=role,
                response_mode=response_mode,
                system=system,
                payload=payload,
                schema=schema,
                correction=1,
                response_schema=response_schema,
                correction_message=correction_message,
                max_tokens=max_tokens,
            )
        )
        assessor(logical)
    print(json.dumps({"event": "logical", **logical.safe_dict()}, ensure_ascii=False), flush=True)
    return logical


def initial_rank(results: dict[str, LogicalResult]) -> list[str]:
    return sorted(
        results,
        key=lambda model: (
            not results[model].passed,
            -results[model].quality_score,
            len(results[model].attempts),
            results[model].final.total_ms,
            results[model].final.first_byte_ms or 10**9,
        ),
    )


def aggregate_rank(models: list[str], runs: dict[str, list[LogicalResult]]) -> list[str]:
    def key(model: str) -> tuple[Any, ...]:
        values = runs[model]
        passed = sum(item.passed for item in values)
        fingerprints = [item.fingerprint for item in values if item.fingerprint]
        stability = max(Counter(fingerprints).values(), default=0) / max(1, len(values))
        median_ms = statistics.median(item.final.total_ms for item in values)
        return (
            -passed,
            -statistics.mean(item.quality_score for item in values),
            -stability,
            median_ms,
        )

    return sorted(models, key=key)


def aggregate_safe(runs: dict[str, list[LogicalResult]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for model, values in runs.items():
        fingerprints = [item.fingerprint for item in values if item.fingerprint]
        summary[model] = {
            "logical_runs": len(values),
            "passed": sum(item.passed for item in values),
            "success_rate": round(sum(item.passed for item in values) / max(1, len(values)), 4),
            "median_total_ms": round(statistics.median(item.final.total_ms for item in values)),
            "max_total_ms": max(item.final.total_ms for item in values),
            "median_first_byte_ms": round(
                statistics.median(
                    item.final.first_byte_ms or item.final.total_ms for item in values
                )
            ),
            "stability_rate": round(
                max(Counter(fingerprints).values(), default=0) / max(1, len(values)), 4
            ),
            "schema_rate": round(
                sum(item.final.schema_valid for item in values) / max(1, len(values)), 4
            ),
            "evidence_rate": round(
                sum(item.evidence_valid for item in values) / max(1, len(values)), 4
            ),
        }
    return summary


def stopped_by_transport(result: LogicalResult) -> bool:
    return result.final.error_code in {"TIMEOUT", "NETWORK_ERROR"} or (
        result.final.http_status is not None and result.final.http_status >= 500
    )


async def parse_real_sample(path: Path) -> tuple[Any, dict[str, Any]]:
    content = path.read_bytes()
    local = LocalFile(
        file_id="fil_live_reference",
        role="REFERENCE",
        file_name=path.name,
        safe_url="local-live-eval://redacted",
        path=path,
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        detected_mime_type=DOCX_MIME,
    )
    document = await DocxParser().parse(local)
    return document, compact_extraction_payload(document, document.blocks)


async def run_deepseek_followup(
    args: argparse.Namespace,
    settings: Settings,
) -> int:
    budget = CallBudget(min(args.max_calls, 11))
    review_payload, review_expected = review_fixture()
    synthetic_review_schema = review_response_schema(review_payload)
    probe: LogicalResult | None = None
    review_runs: list[LogicalResult] = []
    probe = await evaluate_logical(
        settings,
        budget,
        model=DEEPSEEK_MODEL,
        role="review",
        response_mode="json_schema",
        system=REVIEW_SYSTEM_PROMPT,
        payload=review_payload,
        schema=FactReview,
        allow_correction=True,
        assessor=lambda item, payload=review_payload, expected=review_expected: assess_review(
            item, payload, expected
        ),
        response_schema=synthetic_review_schema,
        correct_identity_failure=True,
    )
    if probe.passed and not stopped_by_transport(probe):
        for _ in range(3):
            result = await evaluate_logical(
                settings,
                budget,
                model=DEEPSEEK_MODEL,
                role="review",
                response_mode="json_schema",
                system=REVIEW_SYSTEM_PROMPT,
                payload=review_payload,
                schema=FactReview,
                allow_correction=True,
                assessor=(
                    lambda item, payload=review_payload, expected=review_expected: assess_review(
                        item, payload, expected
                    )
                ),
                response_schema=synthetic_review_schema,
                correct_identity_failure=True,
            )
            review_runs.append(result)
            if stopped_by_transport(result):
                break

    review_gate = (
        probe is not None
        and probe.passed
        and len(review_runs) == 3
        and all(item.passed for item in review_runs)
        and all(item.final.total_ms < 240_000 for item in review_runs)
    )
    real_safe: dict[str, Any] = {"executed": False}
    if review_gate and args.real_sample:
        document, real_payload = await parse_real_sample(args.real_sample)
        real_extraction = await evaluate_logical(
            settings,
            budget,
            model=DEEPSEEK_MODEL,
            role="extraction",
            response_mode="json_schema",
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=real_payload,
            schema=CompactDocumentFactExtraction,
            allow_correction=False,
            assessor=lambda item, payload=real_payload, document=document: assess_real_extraction(
                item, payload, document
            ),
        )
        real_safe = {
            "executed": True,
            "review_mode": "SAME_MODEL_DIAGNOSTIC",
            "independent_review": False,
            "sample_name": args.real_sample.name,
            "block_count": len(document.blocks),
            "character_count": sum(len(block.raw_text) for block in document.blocks),
            "extraction": real_extraction.safe_dict(),
            "review": None,
        }
        if real_extraction.passed and isinstance(
            real_extraction.final.value, DocumentFactExtraction
        ):
            extracted = real_extraction.final.value
            real_review_payload = {
                "file_id": document.file_id,
                "role": document.role,
                "blocks": real_payload["blocks"],
                "facts": [fact.model_dump(mode="json") for fact in extracted.facts],
                "semantic_concepts": [
                    item.model_dump(mode="json") for item in extracted.semantic_concepts
                ],
                "validation_specs": [
                    item.model_dump(mode="json") for item in extracted.validation_specs
                ],
                "review_requirements": {
                    "required_decision_count": len(extracted.facts),
                    "one_decision_per_fact": True,
                    "evaluate_each_fact_independently": True,
                },
            }
            real_review = await evaluate_logical(
                settings,
                budget,
                model=DEEPSEEK_MODEL,
                role="review",
                response_mode="json_schema",
                system=REVIEW_SYSTEM_PROMPT,
                payload=real_review_payload,
                schema=FactReview,
                allow_correction=True,
                assessor=lambda item, payload=real_review_payload: assess_review(
                    item, payload, None
                ),
                response_schema=review_response_schema(real_review_payload),
                correct_identity_failure=True,
            )
            real_safe["review"] = real_review.safe_dict()

    summary = {
        "mode": "deepseek_review_followup",
        "model": DEEPSEEK_MODEL,
        "response_mode": "json_schema",
        "http_calls": budget.used,
        "http_call_limit": budget.limit,
        "probe": probe.safe_dict() if probe is not None else None,
        "review_gate": bool(review_gate),
        "review": (
            aggregate_safe({DEEPSEEK_MODEL: review_runs}).get(DEEPSEEK_MODEL, {})
            if review_runs
            else {}
        ),
        "real_sample": real_safe,
    }
    print(json.dumps({"event": "final_summary", **summary}, ensure_ascii=False), flush=True)
    real_review_safe = real_safe.get("review")
    real_passed = not args.real_sample or bool(
        real_safe.get("executed")
        and real_safe.get("extraction", {}).get("passed")
        and isinstance(real_review_safe, dict)
        and real_review_safe.get("passed")
    )
    return 0 if review_gate and real_passed else 2


async def run_deepseek_compact_real(
    args: argparse.Namespace,
    settings: Settings,
) -> int:
    if not args.real_sample:
        raise RuntimeError("--deepseek-compact-real requires --real-sample")
    budget = CallBudget(min(args.max_calls, 3))
    document, real_payload = await parse_real_sample(args.real_sample)
    extraction = await evaluate_logical(
        settings,
        budget,
        model=DEEPSEEK_MODEL,
        role="extraction",
        response_mode="json_schema",
        system=EXTRACTION_SYSTEM_PROMPT,
        payload=real_payload,
        schema=CompactDocumentFactExtraction,
        allow_correction=False,
        assessor=lambda item, payload=real_payload, document=document: assess_real_extraction(
            item, payload, document
        ),
    )
    review: LogicalResult | None = None
    if extraction.passed and isinstance(extraction.final.value, DocumentFactExtraction):
        extracted = extraction.final.value
        review_payload = {
            "file_id": document.file_id,
            "role": document.role,
            "blocks": real_payload["blocks"],
            "facts": [fact.model_dump(mode="json") for fact in extracted.facts],
            "semantic_concepts": [
                item.model_dump(mode="json") for item in extracted.semantic_concepts
            ],
            "validation_specs": [
                item.model_dump(mode="json") for item in extracted.validation_specs
            ],
            "review_requirements": {
                "required_decision_count": len(extracted.facts),
                "one_decision_per_fact": True,
                "evaluate_each_fact_independently": True,
            },
        }
        review = await evaluate_logical(
            settings,
            budget,
            model=DEEPSEEK_MODEL,
            role="review",
            response_mode="json_schema",
            system=REVIEW_SYSTEM_PROMPT,
            payload=review_payload,
            schema=FactReview,
            allow_correction=True,
            assessor=lambda item, payload=review_payload: assess_review(item, payload, None),
            response_schema=review_response_schema(review_payload),
            correct_identity_failure=True,
        )
    summary = {
        "mode": "deepseek_compact_real",
        "model": DEEPSEEK_MODEL,
        "response_mode": "json_schema",
        "max_output_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
        "http_calls": budget.used,
        "http_call_limit": budget.limit,
        "review_mode": "SAME_MODEL_DIAGNOSTIC",
        "independent_review": False,
        "sample_name": args.real_sample.name,
        "block_count": len(document.blocks),
        "character_count": sum(len(block.raw_text) for block in document.blocks),
        "extraction": extraction.safe_dict(),
        "review": review.safe_dict() if review is not None else None,
    }
    print(json.dumps({"event": "final_summary", **summary}, ensure_ascii=False), flush=True)
    return 0 if extraction.passed and review is not None and review.passed else 2


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.llm_configured:
        raise RuntimeError("LLM is not configured")
    if args.deepseek_compact_real:
        return await run_deepseek_compact_real(args, settings)
    if args.deepseek_followup:
        return await run_deepseek_followup(args, settings)
    budget = CallBudget(args.max_calls)
    extraction_payload = extraction_fixture()
    review_payload, review_expected = review_fixture()
    source_advice, advice_anchors = advice_fixture()
    synthetic_advice_payload = advice_payload(source_advice)
    active = set(TEXT_MODELS)
    modes: dict[str, ResponseMode] = {}
    extraction_initial: dict[str, LogicalResult] = {}

    for model in TEXT_MODELS:
        extraction_initial[model] = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="extraction",
            response_mode="prompt_only",
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=extraction_payload,
            schema=CompactDocumentFactExtraction,
            allow_correction=True,
            assessor=lambda item, payload=extraction_payload: assess_extraction(item, payload),
        )
        if stopped_by_transport(extraction_initial[model]):
            active.discard(model)

    capability: dict[str, dict[str, LogicalResult]] = {model: {} for model in TEXT_MODELS}
    for model in list(active):
        schema_result = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="extraction",
            response_mode="json_schema",
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=extraction_payload,
            schema=CompactDocumentFactExtraction,
            allow_correction=False,
            assessor=lambda item, payload=extraction_payload: assess_extraction(item, payload),
        )
        capability[model]["json_schema"] = schema_result
        if schema_result.passed:
            modes[model] = "json_schema"
            continue
        object_result = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="extraction",
            response_mode="json_object",
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=extraction_payload,
            schema=CompactDocumentFactExtraction,
            allow_correction=False,
            assessor=lambda item, payload=extraction_payload: assess_extraction(item, payload),
        )
        capability[model]["json_object"] = object_result
        if object_result.passed:
            modes[model] = "json_object"
        elif extraction_initial[model].passed:
            modes[model] = "prompt_only"
        else:
            active.discard(model)

    review_initial: dict[str, LogicalResult] = {}
    advice_initial: dict[str, LogicalResult] = {}
    for model in TEXT_MODELS:
        if model not in active:
            continue
        mode = modes[model]
        review_initial[model] = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="review",
            response_mode=mode,
            system=REVIEW_SYSTEM_PROMPT,
            payload=review_payload,
            schema=FactReview,
            allow_correction=True,
            assessor=lambda item, payload=review_payload, expected=review_expected: assess_review(
                item, payload, expected
            ),
        )
        advice_initial[model] = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="advice",
            response_mode=mode,
            system=ADVICE_SYSTEM_PROMPT,
            payload=synthetic_advice_payload,
            schema=AdviceResponse,
            allow_correction=True,
            assessor=lambda item, source=source_advice, anchors=advice_anchors: assess_advice(
                item, source, anchors
            ),
        )

    extraction_top = [
        model for model in initial_rank(extraction_initial) if extraction_initial[model].passed
    ][:2]
    review_top = [model for model in initial_rank(review_initial) if review_initial[model].passed][
        :2
    ]
    advice_top = [model for model in initial_rank(advice_initial) if advice_initial[model].passed][
        :2
    ]
    extraction_runs = {model: [extraction_initial[model]] for model in extraction_top}
    review_runs = {model: [review_initial[model]] for model in review_top}
    advice_runs = {model: [advice_initial[model]] for model in advice_top}

    for model in extraction_top:
        for _ in range(3):
            extraction_runs[model].append(
                await evaluate_logical(
                    settings,
                    budget,
                    model=model,
                    role="extraction",
                    response_mode=modes[model],
                    system=EXTRACTION_SYSTEM_PROMPT,
                    payload=extraction_payload,
                    schema=CompactDocumentFactExtraction,
                    allow_correction=False,
                    assessor=lambda item, payload=extraction_payload: assess_extraction(
                        item, payload
                    ),
                )
            )
    for model in review_top:
        for _ in range(3):
            review_runs[model].append(
                await evaluate_logical(
                    settings,
                    budget,
                    model=model,
                    role="review",
                    response_mode=modes[model],
                    system=REVIEW_SYSTEM_PROMPT,
                    payload=review_payload,
                    schema=FactReview,
                    allow_correction=False,
                    assessor=lambda item, payload=review_payload, expected=review_expected: (
                        assess_review(item, payload, expected)
                    ),
                )
            )
    for model in advice_top:
        for _ in range(2):
            advice_runs[model].append(
                await evaluate_logical(
                    settings,
                    budget,
                    model=model,
                    role="advice",
                    response_mode=modes[model],
                    system=ADVICE_SYSTEM_PROMPT,
                    payload=synthetic_advice_payload,
                    schema=AdviceResponse,
                    allow_correction=False,
                    assessor=lambda item, source=source_advice, anchors=advice_anchors: (
                        assess_advice(item, source, anchors)
                    ),
                )
            )

    mapping_results: dict[str, LogicalResult] = {}
    mapping_payload = mapping_review_fixture()
    for model in review_top:
        mapping_results[model] = await evaluate_logical(
            settings,
            budget,
            model=model,
            role="mapping_review",
            response_mode=modes[model],
            system=MAPPING_REVIEW_SYSTEM_PROMPT,
            payload=mapping_payload,
            schema=FactMappingReview,
            allow_correction=False,
            assessor=lambda item, payload=mapping_payload: assess_mapping_review(item, payload),
        )

    extraction_rank = aggregate_rank(extraction_top, extraction_runs)
    review_rank = [
        model
        for model in aggregate_rank(review_top, review_runs)
        if mapping_results.get(model) and mapping_results[model].passed
    ]
    advice_rank = aggregate_rank(advice_top, advice_runs)
    selected: dict[str, str] = {}
    for extraction_model in extraction_rank:
        for review_model in review_rank:
            if extraction_model == review_model or modes[extraction_model] != modes[review_model]:
                continue
            for advice_model in advice_rank:
                if modes[advice_model] != modes[extraction_model]:
                    continue
                ext_summary = aggregate_safe(extraction_runs)[extraction_model]
                review_summary = aggregate_safe(review_runs)[review_model]
                advice_summary = aggregate_safe(advice_runs)[advice_model]
                latency_ok = (
                    ext_summary["median_total_ms"] + review_summary["median_total_ms"] <= 180_000
                    and ext_summary["max_total_ms"] < 240_000
                    and review_summary["max_total_ms"] < 240_000
                    and advice_summary["median_total_ms"] <= 60_000
                )
                quality_ok = (
                    ext_summary["success_rate"] == 1
                    and review_summary["success_rate"] == 1
                    and advice_summary["success_rate"] == 1
                )
                if latency_ok and quality_ok:
                    selected = {
                        "extraction": extraction_model,
                        "review": review_model,
                        "advice": advice_model,
                        "response_mode": modes[extraction_model],
                    }
                    break
            if selected:
                break
        if selected:
            break

    real_safe: dict[str, Any] = {"executed": False}
    if selected and args.real_sample:
        document, real_payload = await parse_real_sample(args.real_sample)
        real_extraction = await evaluate_logical(
            settings,
            budget,
            model=selected["extraction"],
            role="extraction",
            response_mode=selected["response_mode"],
            system=EXTRACTION_SYSTEM_PROMPT,
            payload=real_payload,
            schema=CompactDocumentFactExtraction,
            allow_correction=False,
            assessor=lambda item, payload=real_payload, document=document: assess_real_extraction(
                item, payload, document
            ),
        )
        real_safe = {
            "executed": True,
            "sample_name": args.real_sample.name,
            "block_count": len(document.blocks),
            "character_count": sum(len(block.raw_text) for block in document.blocks),
            "extraction": real_extraction.safe_dict(),
            "review": None,
        }
        if real_extraction.passed and isinstance(
            real_extraction.final.value, DocumentFactExtraction
        ):
            extracted = real_extraction.final.value
            real_review_payload = {
                "file_id": document.file_id,
                "role": document.role,
                "blocks": real_payload["blocks"],
                "facts": [fact.model_dump(mode="json") for fact in extracted.facts],
                "semantic_concepts": [
                    item.model_dump(mode="json") for item in extracted.semantic_concepts
                ],
                "validation_specs": [
                    item.model_dump(mode="json") for item in extracted.validation_specs
                ],
            }
            real_review = await evaluate_logical(
                settings,
                budget,
                model=selected["review"],
                role="review",
                response_mode=selected["response_mode"],
                system=REVIEW_SYSTEM_PROMPT,
                payload=real_review_payload,
                schema=FactReview,
                allow_correction=False,
                assessor=lambda item, payload=real_review_payload: assess_review(
                    item, payload, None
                ),
            )
            real_safe["review"] = real_review.safe_dict()
            selected["real_sample_passed"] = str(real_review.passed).lower()
        else:
            selected["real_sample_passed"] = "false"

    summary = {
        "models": list(TEXT_MODELS),
        "http_calls": budget.used,
        "http_call_limit": budget.limit,
        "response_modes": modes,
        "capability": {
            model: {mode: value.safe_dict() for mode, value in results.items()}
            for model, results in capability.items()
        },
        "extraction": aggregate_safe(extraction_runs),
        "review": aggregate_safe(review_runs),
        "mapping_review": {model: value.safe_dict() for model, value in mapping_results.items()},
        "advice": aggregate_safe(advice_runs),
        "ranking": {
            "extraction": extraction_rank,
            "review": review_rank,
            "advice": advice_rank,
        },
        "selected": selected or None,
        "real_sample": real_safe,
    }
    print(json.dumps({"event": "final_summary", **summary}, ensure_ascii=False), flush=True)
    return (
        0
        if selected and (not args.real_sample or selected.get("real_sample_passed") == "true")
        else 2
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely evaluate text models for contract LLM roles"
    )
    parser.add_argument("--real-sample", type=Path)
    parser.add_argument("--max-calls", type=int, default=44)
    parser.add_argument(
        "--deepseek-followup",
        action="store_true",
        help="probe DeepSeek review, repeat three times, then run diagnostic sample (max 11 calls)",
    )
    parser.add_argument(
        "--deepseek-compact-real",
        action="store_true",
        help="run one compact first-phase real sample extraction, then diagnostic review",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
