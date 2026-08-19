from functools import lru_cache

from pydantic import Field
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
    MAX_REFERENCE_FILES: int = Field(default=10, ge=1, le=100)
    DOWNLOAD_TIMEOUT_SECONDS: float = 120.0
    DOWNLOAD_MAX_REDIRECTS: int = 3
    PDF_MIN_TEXT_CHARS_PER_PAGE: int = Field(default=20, ge=1)
    ALLOW_HTTP_DOWNLOADS: bool = False
    DOWNLOAD_HOST_ALLOWLIST: str = ""

    OCR_ENABLED: bool = False
    OCR_BASE_URL: str = ""
    OCR_API_KEY: str = ""
    OCR_TIMEOUT_SECONDS: float = 600.0

    LLM_ENABLED: bool = False
    LLM_PROTOCOL: str = "openai"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_EXTRACTION_MODEL: str = "GLM-5.2"
    LLM_ADVICE_MODEL: str = "GLM-5.2"
    LLM_EMBEDDING_MODEL: str = "embedding"
    LLM_RERANK_MODEL: str = "rerank"
    LLM_TIMEOUT_SECONDS: float = 300.0
    LLM_MAX_CONCURRENCY: int = 1
    LLM_MAX_OUTPUT_TOKENS: int = 4096
    LLM_STRUCTURE_RETRY_ATTEMPTS: int = 2
    LLM_NATIVE_STRUCTURED_OUTPUT: bool = False
    LLM_ENABLE_EMBEDDING: bool = False
    LLM_ENABLE_RERANK: bool = False

    RESULT_SCHEMA_VERSION: str = "1.0"
    WORKFLOW_VERSION: str = "0.1.0"
    RULES_VERSION: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
