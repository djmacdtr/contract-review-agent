from app.core.config import Settings
from app.core.ids import new_file_id, new_request_id, new_task_id
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

