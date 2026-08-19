from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LlmResult:
    value: dict[str, Any]
    configured_model: str
    actual_model: str | None
    mock: bool


class ContractLlmClient(Protocol):
    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult: ...
    async def generate_advice(self, payload: dict[str, Any]) -> LlmResult: ...


class MockContractLlmClient:
    """No-network adapter used for milestones 0-1, regardless of gateway settings."""

    def __init__(self, model: str = "GLM-5.2") -> None:
        self.model = model

    async def extract_facts(self, payload: dict[str, Any]) -> LlmResult:
        return LlmResult(value={"facts": []}, configured_model=self.model, actual_model=None, mock=True)

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

