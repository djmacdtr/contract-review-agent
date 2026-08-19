from app.adapters.documents.base import MockDocumentParser
from app.adapters.downloader import MockFileDownloadService
from app.adapters.llm.base import MockContractLlmClient
from app.adapters.ocr.base import DisabledOcrAdapter


async def test_all_milestone_adapters_are_no_network_mocks() -> None:
    prepared = await MockFileDownloadService().prepare(
        [{"file_id": "fil_1", "file_name": "a.docx", "safe_url": "https://example.com/a.docx"}]
    )
    parsed = await MockDocumentParser().parse(prepared[0].file_id, prepared[0].file_name)
    ocr = await DisabledOcrAdapter().recognize(prepared[0].file_id)
    llm = await MockContractLlmClient().generate_advice({})
    assert parsed.parser_name == "mock-parser"
    assert ocr.mock is True
    assert llm.mock is True and llm.actual_model is None

