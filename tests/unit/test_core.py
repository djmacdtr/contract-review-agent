import logging

from app.core.config import Settings
from app.core.ids import new_file_id, new_request_id, new_task_id
from app.core.logging import configure_logging
from app.services.url_security import sanitize_url


def test_prefixed_ulids_are_stable_length_and_unique() -> None:
    task_ids = {new_task_id() for _ in range(20)}
    assert len(task_ids) == 20
    assert all(value.startswith("tsk_") and len(value) == 30 for value in task_ids)
    assert new_file_id().startswith("fil_")
    assert new_request_id().startswith("req_")


def test_url_sanitizer_removes_credentials_query_and_fragment() -> None:
    safe = sanitize_url("https://user:pass@files.example.com:8443/a/合同.docx?token=secret#page=2")
    assert safe == "https://files.example.com:8443/a/合同.docx"
    assert "secret" not in safe
    assert "pass" not in safe


def test_llm_configuration_defaults() -> None:
    settings = Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://x:x@db/test")
    assert settings.LLM_ENABLED is False
    assert settings.LLM_EXTRACTION_MODEL == "GLM-5.2"
    assert settings.LLM_ADVICE_MODEL == "GLM-5.2"
    assert settings.LLM_ENABLE_EMBEDDING is False
    assert settings.LLM_ENABLE_RERANK is False


def test_http_client_access_logs_are_suppressed() -> None:
    configure_logging("INFO")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_ocr_readiness_requires_all_runtime_values() -> None:
    assert (
        Settings(
            _env_file=None,
            OCR_ENABLED=True,
            OCR_BASE_URL="",
            OCR_API_KEY="",
            OCR_AUTH_HEADER="",
        ).ocr_configured
        is False
    )
    configured = Settings(
        _env_file=None,
        OCR_ENABLED=True,
        OCR_BASE_URL="https://ocr.invalid",
        OCR_API_KEY="secret",
        OCR_AUTH_HEADER="x-api-key",
    )
    assert configured.ocr_configured is True
