from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiErrorBody(BaseModel):
    details: Any | None = Field(default=None, description="脱敏后的错误详情")


class ApiResponse[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="业务码；0 表示成功", examples=["0"])
    message: str = Field(description="面向调用方的简短消息", examples=["success"])
    request_id: str = Field(description="请求追踪 ID", examples=["req_01K2EXAMPLE"])
    data: T | None = Field(default=None, description="业务响应数据")
    error: ApiErrorBody | None = Field(default=None, description="失败详情")


class HealthData(BaseModel):
    status: str = Field(description="服务状态", examples=["ok"])


class ReadyData(BaseModel):
    status: str = Field(description="就绪状态", examples=["ready"])
    database: str = Field(description="数据库状态", examples=["ok"])
    ocr_configured: bool = Field(description="是否配置 OCR；不影响 API 就绪")
    llm_configured: bool = Field(description="LLM 是否启用且连接参数完整")
