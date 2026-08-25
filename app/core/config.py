from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "postgresql+asyncpg://contract:contract@postgres:5432/contract_review"

    WORKER_ID: str = "worker-1"
    WORKER_POLL_INTERVAL_SECONDS: float = 1.0
    WORKER_HEARTBEAT_INTERVAL_SECONDS: float = 15.0
    WORKER_STALE_AFTER_SECONDS: float = 120.0
    WORKER_MAX_CONCURRENT_TASKS: int = 1
    TASK_MAX_ATTEMPTS: int = 2
    MOCK_STAGE_DELAY_SECONDS: float = 0.15

    TEMP_ROOT: str = "/tmp/contract-review"
    MAX_FILE_SIZE_MB: float = Field(default=200, gt=0)
    MAX_REFERENCE_FILES: int = Field(default=20, ge=1, le=100)
    DOWNLOAD_TIMEOUT_SECONDS: float = 120.0
    DOWNLOAD_MAX_REDIRECTS: int = 3
    PDF_MIN_TEXT_CHARS_PER_PAGE: int = Field(default=20, ge=1)
    ALLOW_HTTP_DOWNLOADS: bool = False
    DOWNLOAD_HOST_ALLOWLIST: str = ""

    OCR_ENABLED: bool = False
    OCR_BASE_URL: str = ""
    OCR_API_KEY: str = ""
    OCR_AUTH_HEADER: str = ""
    OCR_TIMEOUT_SECONDS: float = 600.0
    OCR_MAX_RESPONSE_MB: float = Field(default=50, gt=0)
    OCR_HTTP_RETRY_ATTEMPTS: int = Field(default=2, ge=0, le=5)
    OCR_RETRY_BACKOFF_SECONDS: float = Field(default=0.5, ge=0, le=30)
    OCR_LOW_CONFIDENCE_THRESHOLD: float = Field(default=0.8, ge=0, le=1)

    PAGE_MISSING_MIN_EQUIVALENT: float = Field(default=0.8, gt=0)
    PAGE_MISSING_MIN_ANCHOR_SIMILARITY: float = Field(default=0.85, ge=0, le=1)
    PAGE_MISSING_MIN_STRUCTURE_UNITS: int = Field(default=2, ge=1)

    LLM_ENABLED: bool = False
    LLM_PROTOCOL: str = "openai"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_EXTRACTION_MODEL: str = "GLM-5.2"
    LLM_REVIEW_MODEL: str = "GLM-5.2-reviewer"
    LLM_ADVICE_MODEL: str = "GLM-5.2"
    LLM_EMBEDDING_MODEL: str = "embedding"
    LLM_RERANK_MODEL: str = "rerank"
    LLM_TIMEOUT_SECONDS: float = 300.0
    LLM_MAX_CONCURRENCY: int = 1
    LLM_MAX_OUTPUT_TOKENS: int = 4096
    LLM_CHUNK_MAX_CHARS: int = Field(default=12000, ge=1000, le=100000)
    LLM_REVIEW_BATCH_MAX_CHARS: int = Field(default=12000, ge=1000, le=100000)
    LLM_REVIEW_CONTEXT_BLOCKS: int = Field(default=1, ge=0, le=10)
    LLM_STRUCTURE_RETRY_ATTEMPTS: int = 2
    LLM_CONSENSUS_MIN_CONFIDENCE: float = Field(default=0.85, ge=0, le=1)
    LLM_REQUIRE_INDEPENDENT_MODEL: bool = True
    LLM_SAME_MODEL_DIAGNOSTIC: bool = False
    LLM_NATIVE_STRUCTURED_OUTPUT: bool = False
    LLM_ENABLE_EMBEDDING: bool = False
    LLM_ENABLE_RERANK: bool = False

    RESULT_SCHEMA_VERSION: str = "2.1"
    WORKFLOW_VERSION: str = "0.1.0"
    RULES_VERSION: str = "0.1.0"

    @property
    def ocr_configured(self) -> bool:
        return self.OCR_ENABLED and all(
            value.strip() for value in (self.OCR_BASE_URL, self.OCR_API_KEY, self.OCR_AUTH_HEADER)
        )

    @property
    def llm_configured(self) -> bool:
        return self.LLM_ENABLED and all(
            value.strip() for value in (self.LLM_BASE_URL, self.LLM_API_KEY)
        )

    @model_validator(mode="after")
    def validate_llm_review_mode(self) -> Self:
        if not self.LLM_REQUIRE_INDEPENDENT_MODEL:
            raise ValueError(
                "LLM_REQUIRE_INDEPENDENT_MODEL must remain true; use "
                "LLM_SAME_MODEL_DIAGNOSTIC for non-consensus development runs"
            )
        if self.LLM_SAME_MODEL_DIAGNOSTIC and self.APP_ENV.casefold() not in {
            "development",
            "test",
            "evaluation",
        }:
            raise ValueError(
                "LLM_SAME_MODEL_DIAGNOSTIC is only allowed in development, test, or evaluation"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
