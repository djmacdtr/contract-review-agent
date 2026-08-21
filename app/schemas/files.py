from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.core.enums import ReferenceType


class RemoteFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl = Field(
        description="HTTP(S) 文件 URL；本阶段仅保存描述，不发起下载",
        examples=["https://files.example.com/contract.docx?signature=example"],
    )
    file_name: str = Field(
        min_length=1, max_length=500, description="原始文件名", examples=["融资租赁合同.docx"]
    )
    mime_type: str | None = Field(
        default=None, max_length=200, description="调用方声明的 MIME 类型"
    )
    reference_type: ReferenceType | None = Field(
        default=None,
        description="已弃用且被忽略；辅助资料类型由系统根据正文识别",
        deprecated=True,
    )
    display_name: str | None = Field(
        default=None, max_length=200, description="控制台友好名称"
    )


class TaskFileView(BaseModel):
    file_id: str
    role: str
    reference_type: str | None = None
    file_name: str
    safe_url: str
    sort_order: int
