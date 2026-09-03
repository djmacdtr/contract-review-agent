from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LlmResult:
    value: dict[str, Any]
    configured_model: str
    actual_model: str | None
    mock: bool
    duration_ms: int = 0
    request_attempts: int = 0
    structure_retries: int = 0
    finish_reason: str | None = None
    response_format: str = "prompt_only"
    response_metadata: dict[str, Any] | None = None


class ContractLlmClient(Protocol):
    async def extract_document_profile(self, payload: dict[str, Any]) -> LlmResult: ...
    async def extract_fact_batch(self, payload: dict[str, Any]) -> LlmResult: ...
    async def extract_numeric_candidates(self, payload: dict[str, Any]) -> LlmResult: ...
    async def extract_text_facts(
        self,
        payload: dict[str, Any],
        *,
        allow_structure_correction: bool = True,
    ) -> LlmResult: ...

    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def plan_semantics(self, payload: dict[str, Any]) -> LlmResult: ...
    async def review_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def map_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def map_fact_candidates(self, payload: dict[str, Any]) -> LlmResult: ...
    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult: ...
    async def cross_validate_candidates(self, payload: dict[str, Any]) -> LlmResult: ...
    async def validate_final_compare_candidates(self, payload: dict[str, Any]) -> LlmResult: ...
    async def validate_final_compare_duplicate_clusters(
        self, payload: dict[str, Any]
    ) -> LlmResult: ...
    async def generate_delivery_advice(self, payload: dict[str, Any]) -> LlmResult: ...
    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult: ...

    async def probe_models(self) -> list[str]: ...


class MockContractLlmClient:
    """No-network adapter used for milestones 0-1, regardless of gateway settings."""

    def __init__(self, model: str = "GLM-5.3-Flash") -> None:
        self.model = model

    async def extract_document_profile(self, payload: dict[str, Any]) -> LlmResult:
        first = payload.get("overview_blocks", [{}])[0]
        return LlmResult(
            value={
                "document_kind": "UNKNOWN",
                "title": None,
                "confidence": 0.0,
                "evidence_locations": [first.get("location", {"paragraph_index": 0})],
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def extract_fact_batch(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "facts": [],
                "numeric_candidate_decisions": [
                    {
                        "candidate_index": index,
                        "decision": "IGNORE",
                        "reason_code": "NO_FACT",
                    }
                    for index, _candidate in enumerate(
                        payload.get("numeric_candidates", []), start=1
                    )
                ],
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def extract_numeric_candidates(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "items": [
                    {
                        "candidate_index": index,
                        "semantic_key": "numeric_candidate",
                        "display_name": "数值候选",
                        "value_type": "UNKNOWN",
                        "decision": "IGNORE",
                        "reason_code": "MOCK_IGNORE",
                        "confidence": 0.0,
                    }
                    for index, _candidate in enumerate(
                        payload.get("numeric_candidates", []), start=1
                    )
                ]
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def extract_text_facts(
        self,
        payload: dict[str, Any],
        *,
        allow_structure_correction: bool = True,
    ) -> LlmResult:
        return LlmResult(
            value={"items": [], "has_more": False},
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={"facts": []},
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def probe_models(self) -> list[str]:
        return [self.model]

    async def review_facts(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "file_id": payload.get("file_id", "unknown"),
                "decisions": [],
                "semantic_concepts": [],
                "validation_specs": [],
                "confidence": 0.0,
                "evidence_complete": False,
            },
            configured_model=f"{self.model}-reviewer",
            actual_model=None,
            mock=True,
        )

    async def plan_semantics(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "file_id": payload.get("file_id", "unknown"),
                "semantic_concepts": [],
                "validation_specs": [],
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def map_facts(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "reference_file_id": payload.get("reference_file_id", "unknown"),
                "mappings": [],
                "missing_requirements": [],
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "reference_file_id": payload.get("reference_file_id", "unknown"),
                "decisions": [],
                "missing_requirement_decisions": [],
                "confidence": 0.0,
                "evidence_complete": False,
            },
            configured_model=f"{self.model}-reviewer",
            actual_model=None,
            mock=True,
        )

    async def cross_validate_candidates(self, payload: dict[str, Any]) -> LlmResult:
        items = [
            {
                "candidate_id": group["candidate_id"],
                "decision": (
                    "MATCH"
                    if any(
                        reference["normalized_value"] == group["target"]["normalized_value"]
                        for references in group.get("references", {}).values()
                        for reference in references
                    )
                    else "CONFLICT"
                ),
                "reason": "模拟候选值判断",
            }
            for group in payload.get("candidates", [])
            if isinstance(group, dict) and group.get("candidate_id")
        ]
        return LlmResult(
            value={"items": items},
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )

    async def validate_final_compare_candidates(self, payload: dict[str, Any]) -> LlmResult:
        decisions = [
            {
                "candidate_id": item["candidate_id"],
                "decision": "KEEP_CHANGE",
                "duplicate_of": None,
                "reason_code": "MOCK_KEEP",
                "confidence": 1.0,
            }
            for item in payload.get("candidates", [])
            if isinstance(item, dict) and item.get("candidate_id")
        ]
        return LlmResult(
            value={"decisions": decisions},
            configured_model=self.model,
            actual_model=None,
            mock=True,
            response_format="json_schema",
        )

    async def validate_final_compare_duplicate_clusters(
        self, payload: dict[str, Any]
    ) -> LlmResult:
        groups = []
        for cluster in payload.get("groups", payload.get("clusters", [])):
            candidate_ids = cluster.get("candidate_ids", [])
            if not candidate_ids:
                continue
            groups.append(
                {
                    "group_id": cluster.get("group_id", cluster.get("cluster_id")),
                    "candidate_ids": candidate_ids,
                    "decision": "DISTINCT_CHANGES",
                    "reason_code": "MOCK_KEEP",
                    "confidence": 1.0,
                }
            )
        return LlmResult(
            value={"groups": groups},
            configured_model=self.model,
            actual_model=None,
            mock=True,
            response_format="json_schema",
        )

    async def generate_delivery_advice(self, payload: dict[str, Any]) -> LlmResult:
        return await self.generate_advice(payload)

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        risk_advices = [
            {
                "risk_id": item["risk_id"],
                "analysis_advice": f"请核对“{item.get('title', '该差异')}”的来源文件和对应位置。",
            }
            for item in payload.get("risk_items", [])
        ]
        return LlmResult(
            value={
                "overall_advice": "模拟建议：请结合原始文件进行人工复核。",
                "priority_actions": ["核对金额、期限及合同主体"],
                "manual_review_focus": ["模拟风险与差异项"],
                "limitations": ["本结果未下载或解析任何合同，也未调用真实大模型"],
                "risk_advices": risk_advices,
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )
