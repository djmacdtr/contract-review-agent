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


class ContractLlmClient(Protocol):
    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def review_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def map_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def review_mappings(self, payload: dict[str, Any]) -> LlmResult: ...
    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult: ...

    async def probe_models(self) -> list[str]: ...


class MockContractLlmClient:
    """No-network adapter used for milestones 0-1, regardless of gateway settings."""

    def __init__(self, model: str = "GLM-5.2") -> None:
        self.model = model

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

    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(
            value={
                "overall_advice": "模拟建议：请结合原始文件进行人工复核。",
                "priority_actions": ["核对金额、期限及合同主体"],
                "manual_review_focus": ["模拟风险与差异项"],
                "limitations": ["本结果未下载或解析任何合同，也未调用真实大模型"],
            },
            configured_model=self.model,
            actual_model=None,
            mock=True,
        )
