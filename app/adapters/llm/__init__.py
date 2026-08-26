"""LLM adapter exports with lazy client imports.

Fact planning imports wire schemas while the OpenAI client imports evidence
validators. Lazy client exports keep that dependency direction acyclic.
"""
from app.adapters.llm.schemas import (
    AdviceResponse,
    CompactDocumentFactExtraction,
    CompactDocumentProfile,
    CompactFactCandidate,
    DocumentFactExtraction,
    DocumentProfile,
    FactCandidate,
    FactMapping,
    FactMappingResponse,
    FactMappingReview,
    FactReview,
    FactReviewDecision,
    SemanticConcept,
    SemanticPlanResponse,
    ValidationSpec,
)

__all__ = [
    "DocumentFactExtraction",
    "CompactDocumentFactExtraction",
    "CompactDocumentProfile",
    "CompactFactCandidate",
    "DocumentProfile",
    "FactCandidate",
    "FactMapping",
    "FactMappingResponse",
    "FactMappingReview",
    "FactReview",
    "FactReviewDecision",
    "SemanticConcept",
    "SemanticPlanResponse",
    "ValidationSpec",
    "AdviceResponse",
    "LlmClientError",
    "OpenAIContractLlmClient",
]


def __getattr__(name: str):
    if name in {"LlmClientError", "OpenAIContractLlmClient"}:
        from app.adapters.llm.openai_client import LlmClientError, OpenAIContractLlmClient

        return {
            "LlmClientError": LlmClientError,
            "OpenAIContractLlmClient": OpenAIContractLlmClient,
        }[name]
    raise AttributeError(name)
