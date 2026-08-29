"""Run one worst numeric and one worst text Canary for expanded batches."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient
from app.core.config import Settings
from app.documents.parsers import DocxParser
from app.draft_review.extraction import TEXT_RECOVERABLE_FAILURE_CODES
from app.draft_review.facts import (
    EvidenceValidationError,
    build_template_text_candidates,
    expand_numeric_candidate_response,
    expand_text_fact_response,
    plan_numeric_document_batches,
    plan_text_candidate_batches,
    plan_text_document_batches,
    rehydrate_numeric_fact_evidence,
)
from app.draft_review.template_checks import analyze_template
from app.services.downloader import DOCX_MIME, LocalFile
from scripts.draft_review_llm_readiness import CountingTransport

SAMPLE_DIR = Path(r"D:\work\contract_review\脱敏真实合同")
FILES = (
    ("融资租赁合同（回租）.docx", "TARGET"),
    ("融资租赁合同（回租）模版.docx", "TEMPLATE"),
    ("项目方案确认函.docx", "REFERENCE"),
)


def is_recoverable_text_canary(failure_code: str, unit_count: int) -> bool:
    """Mirror production: only a multi-unit text failure can be split."""

    return unit_count > 1 and failure_code in TEXT_RECOVERABLE_FAILURE_CODES


def safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, LlmClientError):
        return {
            "error_code": exc.code,
            "failure_code": exc.failure_code,
            "request_attempts": exc.request_attempts,
            "structure_retries": exc.structure_retries,
            "finish_reason": exc.finish_reason,
        }
    if isinstance(exc, EvidenceValidationError):
        return {"error_code": "EVIDENCE_VALIDATION", "failure_code": exc.code}
    return {"error_code": type(exc).__name__}


def rank_plan(plan: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        len(json.dumps(plan["payload"], ensure_ascii=False, separators=(",", ":"))),
        int(plan.get("numeric_candidate_count", 0)),
        len(plan.get("blocks", [])),
        str(plan.get("batch_id", "")),
    )


async def parse_documents() -> list[Any]:
    documents = []
    for file_name, role in FILES:
        path = SAMPLE_DIR / file_name
        raw = path.read_bytes()
        local_file = LocalFile(
            file_id=f"canary_{role.lower()}",
            role=role,
            file_name=file_name,
            safe_url="local-canary://redacted",
            path=path,
            file_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            detected_mime_type=DOCX_MIME,
        )
        documents.append(await DocxParser().parse(local_file))
    return documents


async def run(output: Path, *, text_only: bool = False) -> dict[str, Any]:
    base = Settings()
    settings = base.model_copy(
        update={
            "LLM_ENABLED": True,
            "LLM_MAX_OUTPUT_TOKENS": 8192,
            "LLM_EXTRACTION_PAYLOAD_MAX_CHARS": 24000,
            "LLM_EXTRACTION_MAX_NUMERIC_UNITS": 12,
            "LLM_EXTRACTION_MAX_TEXT_UNITS": 16,
            "LLM_MAX_CONCURRENCY": 1,
            "LLM_EXTRACTION_TASK_CONCURRENCY": 1,
            "LLM_HTTP_RETRY_ATTEMPTS": 0,
            "LLM_STRUCTURE_RETRY_ATTEMPTS": 0,
            "LLM_RESPONSE_FORMAT": "json_schema",
            "LLM_NATIVE_STRUCTURED_OUTPUT": True,
            "OCR_ENABLED": False,
            "DOCX_PAGE_LOCATION_ENABLED": False,
        }
    )
    if not settings.llm_configured:
        return {"status": "BLOCKED", "reason_code": "LLM_NOT_CONFIGURED"}

    documents = await parse_documents()
    target = next(document for document in documents if document.role == "TARGET")
    template = next(document for document in documents if document.role == "TEMPLATE")
    references = [
        document
        for document in documents
        if document.role == "REFERENCE" and document.blocks
    ]
    template_review = analyze_template(
        template,
        target,
        ignore_formatting=True,
        ignore_headers_footers=True,
        check_blank_fields=True,
        ocr_low_confidence_threshold=settings.OCR_LOW_CONFIDENCE_THRESHOLD,
        page_missing_min_equivalent=settings.PAGE_MISSING_MIN_EQUIVALENT,
        page_missing_min_anchor_similarity=settings.PAGE_MISSING_MIN_ANCHOR_SIMILARITY,
        page_missing_min_structure_units=settings.PAGE_MISSING_MIN_STRUCTURE_UNITS,
    )
    target_candidates = build_template_text_candidates(template_review, target)
    effective_text_units = min(
        getattr(settings, "LLM_EXTRACTION_MAX_TEXT_UNITS", 16),
        16,
    )
    effective_text_facts = min(
        getattr(settings, "LLM_EXTRACTION_MAX_TEXT_FACTS", 12),
        12,
    )
    estimated_output_tokens = min(
        settings.LLM_EXTRACTION_SIMPLIFIED_ESTIMATED_OUTPUT_TOKENS,
        2000,
    )
    target_plans = plan_text_candidate_batches(
        target,
        target_candidates,
        max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
        max_candidates=effective_text_units,
        max_text_facts=effective_text_facts,
        estimated_output_token_limit=estimated_output_tokens,
    )
    reference_plans = [
        plan
        for document in references
        for plan in plan_text_document_batches(
            document,
            max_payload_chars=settings.LLM_EXTRACTION_PAYLOAD_MAX_CHARS,
            max_text_units=effective_text_units,
            max_text_facts=effective_text_facts,
            estimated_output_token_limit=estimated_output_tokens,
        )
    ]
    numeric_plans = plan_numeric_document_batches(
        target,
        max_payload_chars=24000,
        max_numeric_candidates=24,
        max_numeric_units=12,
        estimated_output_token_limit=2000,
    )
    if text_only:
        selected = []
        if target_plans:
            selected.append(("text", "TARGET", max(target_plans, key=rank_plan)))
        if reference_plans:
            selected.append(("text", "REFERENCE", max(reference_plans, key=rank_plan)))
    else:
        text_plans = [*target_plans, *reference_plans]
        selected = [
            ("text", "TEXT", max(text_plans, key=rank_plan)),
            ("numeric", "TARGET", max(numeric_plans, key=rank_plan)),
        ]
    transport = CountingTransport()
    client = OpenAIContractLlmClient(settings, transport=transport)
    canaries: list[dict[str, Any]] = []
    try:
        for chain, scope, plan in selected:
            started = time.monotonic()
            payload = dict(plan["payload"])
            payload.update(
                {
                    "batch_depth": 0,
                    "parent_batch_id": None,
                    "planned_batch_count": plan.get("planned_batch_count", 0),
                    "extraction_version": plan["extraction_version"],
                }
            )
            safe: dict[str, Any] = {
                "chain": chain,
                "scope": scope,
                "batch_id": plan["batch_id"],
                "unit_count": len(plan["blocks"]),
                "candidate_count": (
                    len(plan["blocks"])
                    if chain == "text"
                    else int(plan.get("numeric_candidate_count", 0))
                ),
            }
            try:
                if chain == "numeric":
                    response = await client.extract_numeric_candidates(
                        payload, allow_structure_correction=False
                    )
                    facts, classified = expand_numeric_candidate_response(
                        payload, response.value
                    )
                    rehydrate_numeric_fact_evidence(target, facts)
                    safe.update(
                        {
                            "status": "SUCCEEDED",
                            "actual_model": response.actual_model,
                            "finish_reason": response.finish_reason,
                            "request_attempts": response.request_attempts,
                            "classified_count": len(classified),
                        }
                    )
                else:
                    response = await client.extract_text_facts(
                        payload, allow_structure_correction=False
                    )
                    facts = expand_text_fact_response(payload, response.value)
                    safe.update(
                        {
                            "status": "SUCCEEDED",
                            "actual_model": response.actual_model,
                            "finish_reason": response.finish_reason,
                            "request_attempts": response.request_attempts,
                            "fact_count": len(facts),
                        }
                    )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                error = safe_error(exc)
                failure_code = error.get("failure_code") or error.get("error_code")
                recoverable = (
                    chain == "text"
                    and is_recoverable_text_canary(failure_code, len(plan["blocks"]))
                )
                safe.update(
                    {
                        "status": "RECOVERABLE" if recoverable else "FAILED",
                        "recoverable": recoverable,
                        **error,
                    }
                )
            safe["elapsed_seconds"] = round(time.monotonic() - started, 3)
            canaries.append(safe)
    finally:
        await transport.close_all()

    report = {
        "status": "SUCCEEDED"
        if all(item["status"] in {"SUCCEEDED", "RECOVERABLE"} for item in canaries)
        else "FAILED",
        "expanded": {
            "max_output_tokens": 8192,
            "max_payload_chars": 24000,
            "max_numeric_units": 12,
            "max_text_units": 16,
            "concurrency": 1,
        },
        "canary_count": len(canaries),
        "formal_planning": {
            "target_candidate_count": len(target_candidates),
            "target_batch_count": len(target_plans),
            "reference_batch_count": len(reference_plans),
            "total_text_batch_count": len(target_plans) + len(reference_plans),
            "target_max_units": effective_text_units,
            "reference_max_units": effective_text_units,
        },
        "llm_http_calls": transport.http_calls,
        "llm_status_counts": dict(sorted(transport.statuses.items())),
        "canaries": canaries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args.output, text_only=args.text_only))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
