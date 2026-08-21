from pydantic import BaseModel, ConfigDict, Field

from app.schemas.files import RemoteFile


class DraftReviewOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ignore_formatting: bool = Field(default=True, description="忽略纯格式差异")
    ignore_headers_footers: bool = Field(default=True, description="忽略页眉页脚")
    check_blank_fields: bool = Field(default=True, description="检查疑似未填写字段")
    check_asset_schedule: bool = Field(default=True, description="检查租赁物附表")
    check_rent_schedule: bool = Field(default=True, description="检查租金计划")


class DraftReviewCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "client_reference_id": "project-2026-001-contract-01",
                "target_file": {
                    "url": "https://files.example.com/draft.docx?signature=example",
                    "file_name": "融资租赁合同-待检查.docx",
                },
                "template_file": {
                    "url": "https://files.example.com/template.docx?signature=example",
                    "file_name": "融资租赁合同模板.docx",
                },
                "reference_files": [
                    {
                        "url": "https://files.example.com/review.pdf?signature=example",
                        "file_name": "评审意见表.pdf",
                    }
                ],
            }
        },
    )

    client_reference_id: str | None = Field(default=None, max_length=128)
    target_file: RemoteFile = Field(description="需要检查的合同")
    template_file: RemoteFile = Field(description="对应制式模板")
    reference_files: list[RemoteFile] = Field(
        min_length=1,
        max_length=100,
        description="辅助资料列表；实际数量上限由 MAX_REFERENCE_FILES 配置",
    )
    options: DraftReviewOptions = Field(default_factory=DraftReviewOptions)


class FinalCompareOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ignore_formatting: bool = True
    ignore_headers_footers: bool = True
    numeric_sensitive: bool = True


class FinalComparisonCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "client_reference_id": "project-2026-001-final-01",
                "baseline_file": {
                    "url": "https://files.example.com/approved.docx?signature=example",
                    "file_name": "审批申请版合同.docx",
                },
                "target_file": {
                    "url": "https://files.example.com/signed.pdf?signature=example",
                    "file_name": "双方盖章扫描件.pdf",
                },
            }
        },
    )

    client_reference_id: str | None = Field(default=None, max_length=128)
    baseline_file: RemoteFile = Field(description="原文件或申请版")
    target_file: RemoteFile = Field(description="需要比对的文件")
    options: FinalCompareOptions = Field(default_factory=FinalCompareOptions)
