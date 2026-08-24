from pathlib import Path

import httpx
import pytest
from docx import Document

from app.adapters.llm.base import LlmResult
from app.adapters.llm.openai_client import LlmClientError
from app.core.config import Settings
from app.core.enums import TaskStage, TaskType
from app.schemas.results import TaskResultData
from app.services.downloader import SafeFileDownloadService
from app.workflows.draft_review import DraftReviewWorkflowExecutor


class EvidencedLlm:
    async def probe_models(self) -> list[str]:
        return ["test-model"]

    async def generate_advice(self, payload: dict) -> LlmResult:
        raise AssertionError("advice is not used in this workflow slice")

    async def extract_facts(self, payload: dict) -> LlmResult:
        first = payload["blocks"][0]
        file_id = payload["file_id"]
        return LlmResult(
            value={
                "profile": {
                    "file_id": file_id,
                    "document_kind": "合同" if payload["role"] == "TARGET" else "辅助资料",
                    "title": None,
                    "confidence": 0.8,
                    "evidence_locations": [first["location"]],
                },
                "facts": [
                    {
                        "field_key": "document_title",
                        "display_name": "文档标题",
                        "value_type": "TEXT",
                        "raw_value": first["text"],
                        "normalized_hint": None,
                        "source_file_id": file_id,
                        "evidence_text": first["text"],
                        "location": first["location"],
                        "confidence": 0.8,
                    }
                ],
                "missing_field_keys": [],
            },
            configured_model="test-model",
            actual_model="test-model-v1",
            mock=False,
            duration_ms=10,
            request_attempts=1,
        )


def build_docx(path: Path, title: str, body: str) -> bytes:
    document = Document()
    document.add_heading(title, level=1)
    document.add_paragraph(body)
    document.save(path)
    return path.read_bytes()


class ConsensusFixtureLlm:
    def __init__(
        self,
        *,
        extraction_confidence: float = 0.95,
        review_confidence: float = 0.95,
        evidence_complete: bool = True,
        extraction_model: str = "extractor-v1",
        review_model: str = "reviewer-v1",
        review_failure: bool = False,
        include_numeric_rule: bool = False,
        review_rule_mismatch: bool = False,
        invalid_advice_evidence: bool = False,
    ) -> None:
        self.extraction_confidence = extraction_confidence
        self.review_confidence = review_confidence
        self.evidence_complete = evidence_complete
        self.extraction_model = extraction_model
        self.review_model = review_model
        self.review_failure = review_failure
        self.include_numeric_rule = include_numeric_rule
        self.review_rule_mismatch = review_rule_mismatch
        self.invalid_advice_evidence = invalid_advice_evidence

    async def probe_models(self) -> list[str]:
        return [self.extraction_model, self.review_model]

    async def extract_facts(self, payload: dict) -> LlmResult:
        first = payload["blocks"][0]
        file_id = payload["file_id"]
        spec = (
            {
                "validation_id": "amount_matches_literal",
                "display_name": "金额校验",
                "expression": {
                    "op": "equals",
                    "left": {"op": "fact", "concept_id": "amount"},
                    "right": {"op": "literal", "value": "100"},
                },
                "evidence_locations": [first["location"]],
                "confidence": self.extraction_confidence,
            }
            if self.include_numeric_rule
            else None
        )
        return LlmResult(
            value={
                "profile": {
                    "file_id": file_id,
                    "document_kind": "合同",
                    "title": None,
                    "confidence": self.extraction_confidence,
                    "evidence_locations": [first["location"]],
                },
                "facts": [
                    {
                        "field_key": "amount",
                        "display_name": "金额",
                        "value_type": "MONEY",
                        "raw_value": "100",
                        "normalized_hint": None,
                        "source_file_id": file_id,
                        "evidence_text": first["text"],
                        "location": first["location"],
                        "confidence": self.extraction_confidence,
                    }
                ],
                "missing_field_keys": [],
                "semantic_concepts": [],
                "validation_specs": [spec] if spec else [],
            },
            configured_model=self.extraction_model,
            actual_model=self.extraction_model,
            mock=False,
        )

    async def review_facts(self, payload: dict) -> LlmResult:
        if self.review_failure:
            raise LlmClientError("LLM_UPSTREAM_ERROR", "模型评审不可用")
        fact = payload["facts"][0]
        validation_specs = payload.get("validation_specs", [])
        if self.review_rule_mismatch and validation_specs:
            validation_specs = [
                {
                    **validation_specs[0],
                    "expression": {
                        **validation_specs[0]["expression"],
                        "right": {"op": "literal", "value": "101"},
                    },
                }
            ]
        return LlmResult(
            value={
                "file_id": payload["file_id"],
                "decisions": [
                    {
                        "field_key": fact["field_key"],
                        "source_file_id": fact["source_file_id"],
                        "location": fact["location"],
                        "decision": "ACCEPT",
                        "evidence_text": fact["evidence_text"],
                        "confidence": self.review_confidence,
                        "reason_code": "EVIDENCE_MATCHED",
                    }
                ],
                "semantic_concepts": [],
                "validation_specs": validation_specs,
                "confidence": self.review_confidence,
                "evidence_complete": self.evidence_complete,
            },
            configured_model=self.review_model,
            actual_model=self.review_model,
            mock=False,
        )

    async def map_facts(self, payload: dict) -> LlmResult:
        fact = payload["reference_facts"][0]
        return LlmResult(
            value={
                "reference_file_id": payload["reference_file_id"],
                "mappings": [
                    {
                        "target_fact_id": payload["target_facts"][0]["target_fact_id"],
                        "reference_field_key": fact["field_key"],
                        "source_file_id": fact["source_file_id"],
                        "reference_location": fact["location"],
                        "decision": "MATCH",
                        "confidence": self.extraction_confidence,
                        "reason_code": "SAME_BUSINESS_FACT",
                    }
                ],
                "missing_requirements": [],
            },
            configured_model=self.extraction_model,
            actual_model=self.extraction_model,
            mock=False,
        )

    async def review_mappings(self, payload: dict) -> LlmResult:
        proposal = payload["proposed_mapping"]["mappings"][0]
        return LlmResult(
            value={
                "reference_file_id": payload["reference_file_id"],
                "decisions": [
                    {
                        "target_fact_id": proposal["target_fact_id"],
                        "reference_field_key": proposal["reference_field_key"],
                        "source_file_id": proposal["source_file_id"],
                        "reference_location": proposal["reference_location"],
                        "decision": "ACCEPT",
                        "confidence": self.review_confidence,
                        "reason_code": "MAPPING_VERIFIED",
                    }
                ],
                "missing_requirement_decisions": [],
                "confidence": self.review_confidence,
                "evidence_complete": self.evidence_complete,
            },
            configured_model=self.review_model,
            actual_model=self.review_model,
            mock=False,
        )

    async def generate_advice(self, payload: dict) -> LlmResult:
        refs = list(payload.get("evidence_refs", []))
        if self.invalid_advice_evidence:
            refs.append({"file_id": "fil_unknown", "location": {"paragraph_index": 99}})
        return LlmResult(
            value={
                "overall_advice": "请复核已有证据。",
                "priority_actions": [],
                "manual_review_focus": [],
                "limitations": [],
                "evidence_refs": refs,
            },
            configured_model=self.extraction_model,
            actual_model=self.extraction_model,
            mock=False,
        )


async def run_consensus_fixture(
    tmp_path: Path,
    llm: ConsensusFixtureLlm,
    *,
    options: dict | None = None,
) -> dict:
    bodies = {
        "/target.docx": build_docx(tmp_path / "target.docx", "合同", "金额100"),
        "/template.docx": build_docx(tmp_path / "template.docx", "合同", "金额100"),
        "/reference.docx": build_docx(tmp_path / "reference.docx", "资料", "金额100"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path], request=request)

    async def no_progress(*_args) -> None:
        return None

    settings = Settings(
        _env_file=None,
        TEMP_ROOT=str(tmp_path / "workspaces"),
        ALLOW_HTTP_DOWNLOADS=True,
        DOWNLOAD_HOST_ALLOWLIST="fixture-server",
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.invalid",
        LLM_API_KEY="unused",
    )
    executor = DraftReviewWorkflowExecutor(
        settings,
        downloader=SafeFileDownloadService(
            settings, transport=httpx.MockTransport(handler), resolver=resolver
        ),
        llm=llm,
    )
    output = await executor.run(
        task_id="tsk_consensus_fixture",
        task_type=TaskType.DRAFT_REVIEW,
        files=[
            {
                "file_id": "fil_target",
                "role": "TARGET",
                "file_name": "target.docx",
                "url": "http://fixture-server/target.docx",
                "safe_url": "http://fixture-server/target.docx",
            },
            {
                "file_id": "fil_template",
                "role": "TEMPLATE",
                "file_name": "template.docx",
                "url": "http://fixture-server/template.docx",
                "safe_url": "http://fixture-server/template.docx",
            },
            {
                "file_id": "fil_reference",
                "role": "REFERENCE",
                "file_name": "reference.docx",
                "url": "http://fixture-server/reference.docx",
                "safe_url": "http://fixture-server/reference.docx",
            },
        ],
        options=options or {},
        progress_callback=no_progress,
    )
    return output.result


async def resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


async def test_draft_review_downloads_and_parses_every_file_without_mocking(
    tmp_path: Path,
) -> None:
    bodies = {
        "/target.docx": build_docx(
            tmp_path / "target.docx", "融资租赁合同", "第一条 融资金额为1000万元。"
        ),
        "/template.docx": build_docx(
            tmp_path / "template.docx", "融资租赁合同", "第一条 融资金额为##{融资金额}万元。"
        ),
        "/reference.docx": build_docx(
            tmp_path / "reference.docx", "任意辅助资料", "仅参与本阶段解析。"
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path], request=request)

    settings = Settings(
        _env_file=None,
        TEMP_ROOT=str(tmp_path / "workspaces"),
        ALLOW_HTTP_DOWNLOADS=True,
        DOWNLOAD_HOST_ALLOWLIST="fixture-server",
    )
    executor = DraftReviewWorkflowExecutor(
        settings,
        downloader=SafeFileDownloadService(
            settings,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        ),
    )
    updates: list[tuple[TaskStage, int]] = []

    async def progress(stage: TaskStage, value: int, message: str) -> None:
        updates.append((stage, value))

    output = await executor.run(
        task_id="tsk_draft_parse",
        task_type=TaskType.DRAFT_REVIEW,
        files=[
            {
                "file_id": "fil_target",
                "role": "TARGET",
                "file_name": "target.docx",
                "url": "http://fixture-server/target.docx?token=secret",
                "safe_url": "http://fixture-server/target.docx",
            },
            {
                "file_id": "fil_template",
                "role": "TEMPLATE",
                "file_name": "template.docx",
                "url": "http://fixture-server/template.docx?token=secret",
                "safe_url": "http://fixture-server/template.docx",
            },
            {
                "file_id": "fil_reference",
                "role": "REFERENCE",
                "file_name": "reference.docx",
                "url": "http://fixture-server/reference.docx?token=secret",
                "safe_url": "http://fixture-server/reference.docx",
            },
        ],
        options={},
        progress_callback=progress,
    )

    result = output.result
    TaskResultData.model_validate(result)
    assert result["mock"] is False
    assert result["metadata"]["execution_mode"] == "RULE_BASED"
    assert result["schema_version"] == "2.1"
    assert result["metadata"]["workflow_version"] == "0.5.1"
    assert result["metadata"]["rules_version"] == "0.4.1"
    assert result["metadata"]["primary_model"] is None
    assert result["conclusion"] == "PASS"
    assert result["diff_items"] == []
    assert result["rule_checks"] == []
    assert result["metadata"]["template_diagnostics"]["filtered_diff_count"] == 1
    assert len({item["code"] for item in result["warnings"]}) == len(result["warnings"])
    assert len(result["files"]) == 3
    assert all(item["parser_name"] == "python-docx" for item in result["files"])
    assert all(item["document_profile"]["document_kind"] == "UNKNOWN" for item in result["files"])
    assert all(item["content_structure"]["block_count"] == 2 for item in result["files"])
    assert result["files"][0]["content_structure"]["sample_locations"]
    assert result["warnings"][-1]["code"] == "DRAFT_REVIEW_RULE_BASED_LIMITATION"
    assert "token=secret" not in str(result)
    assert updates[-1][0] == TaskStage.PERSISTING_RESULT
    assert any(stage == TaskStage.TEMPLATE_COMPARE for stage, _value in updates)
    assert not any((tmp_path / "workspaces").iterdir())
    assert len(output.file_metadata) == 3


async def test_draft_review_uses_evidenced_llm_results_without_losing_rule_results(
    tmp_path: Path,
) -> None:
    bodies = {
        "/target.docx": build_docx(tmp_path / "target.docx", "合同", "固定条款"),
        "/template.docx": build_docx(tmp_path / "template.docx", "合同", "固定条款"),
        "/reference.docx": build_docx(tmp_path / "reference.docx", "合同", "辅助资料"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path], request=request)

    configured = Settings(
        _env_file=None,
        TEMP_ROOT=str(tmp_path / "workspaces"),
        ALLOW_HTTP_DOWNLOADS=True,
        DOWNLOAD_HOST_ALLOWLIST="fixture-server",
        LLM_ENABLED=True,
        LLM_BASE_URL="https://llm.invalid",
        LLM_API_KEY="unused",
    )
    executor = DraftReviewWorkflowExecutor(
        configured,
        downloader=SafeFileDownloadService(
            configured, transport=httpx.MockTransport(handler), resolver=resolver
        ),
        llm=EvidencedLlm(),
    )

    async def progress(stage: TaskStage, value: int, message: str) -> None:
        return None

    output = await executor.run(
        task_id="tsk_hybrid",
        task_type=TaskType.DRAFT_REVIEW,
        files=[
            {
                "file_id": "fil_target",
                "role": "TARGET",
                "file_name": "target.docx",
                "url": "http://fixture-server/target.docx",
                "safe_url": "http://fixture-server/target.docx",
            },
            {
                "file_id": "fil_template",
                "role": "TEMPLATE",
                "file_name": "template.docx",
                "url": "http://fixture-server/template.docx",
                "safe_url": "http://fixture-server/template.docx",
            },
            {
                "file_id": "fil_reference",
                "role": "REFERENCE",
                "file_name": "reference.docx",
                "url": "http://fixture-server/reference.docx",
                "safe_url": "http://fixture-server/reference.docx",
            },
        ],
        options={},
        progress_callback=progress,
    )

    result = output.result
    assert result["metadata"]["execution_mode"] == "HYBRID"
    TaskResultData.model_validate(result)
    assert result["metadata"]["primary_model"] == "test-model-v1"
    assert len(result["metadata"]["model_runs"]) == 2
    assert result["fact_matrix"][0]["status"] == "CONSISTENT"
    assert any(item["module_code"] == "FACT_CONSISTENCY" for item in result["passed_checks"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"review_failure": True},
        {"extraction_model": "same-model", "review_model": "same-model"},
        {"extraction_confidence": 0.84},
        {"review_confidence": 0.84},
        {"evidence_complete": False},
    ],
)
async def test_consensus_gate_never_auto_accepts_unsafe_fact_result(
    tmp_path: Path, kwargs: dict
) -> None:
    result = await run_consensus_fixture(tmp_path, ConsensusFixtureLlm(**kwargs))

    fact_reviews = [
        item for item in result["review_items"] if item["module_code"] == "FACT_CONSISTENCY"
    ]
    assert fact_reviews
    assert not any(item["module_code"] == "FACT_CONSISTENCY" for item in result["risk_items"])
    assert not any(item["module_code"] == "FACT_CONSISTENCY" for item in result["passed_checks"])
    assert result["conclusion"] == "REVIEW_REQUIRED"


async def test_invalid_advice_evidence_is_filtered_and_requires_review(tmp_path: Path) -> None:
    result = await run_consensus_fixture(
        tmp_path,
        ConsensusFixtureLlm(invalid_advice_evidence=True),
    )

    assert result["advice"]["evidence_refs"] == []
    assert "部分建议证据引用未能回查，已移除。" in result["advice"]["limitations"]
    assert any(
        item["reason_code"] == "LLM_ADVICE_EVIDENCE_UNVERIFIED"
        for item in result["review_items"]
    )
    assert result["conclusion"] == "REVIEW_REQUIRED"
    assert result["summary"]["statistics"]["review_count"] == len(result["review_items"])
    assert any(
        warning["code"] == "LLM_ADVICE_EVIDENCE_REVIEW_REQUIRED"
        for warning in result["warnings"]
    )


async def test_numeric_consistency_false_skips_dynamic_rules(tmp_path: Path) -> None:
    result = await run_consensus_fixture(
        tmp_path,
        ConsensusFixtureLlm(include_numeric_rule=True),
        options={"check_numeric_consistency": False},
    )

    assert not any(
        item.get("module_code") == "NUMERIC_CONSISTENCY" for item in result["risk_items"]
    )
    assert not any(
        item.get("module_code") == "NUMERIC_CONSISTENCY" for item in result["review_items"]
    )
    assert not any(
        item.get("module_code") == "NUMERIC_CONSISTENCY" for item in result["passed_checks"]
    )
    assert not any(
        check["rule_id"] == "amount_matches_literal" for check in result["rule_checks"]
    )


async def test_numeric_rule_requires_exact_primary_and_reviewer_consensus(
    tmp_path: Path,
) -> None:
    result = await run_consensus_fixture(
        tmp_path,
        ConsensusFixtureLlm(include_numeric_rule=True, review_rule_mismatch=True),
    )
    assert any(
        item["reason_code"] == "NUMERIC_RULE_UNCERTAIN"
        for item in result["review_items"]
    ), [item["reason_code"] for item in result["review_items"]]
    assert not any(
        item.get("module_code") == "NUMERIC_CONSISTENCY" for item in result["risk_items"]
    )
