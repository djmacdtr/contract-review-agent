from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

from app.main import app

TAG_NAMES = {
    "health": "健康检查",
    "draft-reviews": "起草检查",
    "final-comparisons": "定稿比对",
    "tasks": "任务管理",
}

TAG_DESCRIPTIONS = {
    "健康检查": "检查 API 进程及数据库依赖是否可用。",
    "起草检查": "提交目标合同、制式模板和辅助资料，异步执行起草阶段检查。",
    "定稿比对": "提交审批申请版与待比对文件，异步执行确定性的版本差异检查。",
    "任务管理": "查询异步任务、读取正式结果，以及为失败任务创建重试任务。",
}

OPERATION_DESCRIPTIONS = {
    ("/health", "get"): "检查 API 进程是否正常响应。该接口不检查数据库、OCR 或 LLM。",
    ("/ready", "get"): (
        "检查数据库连接是否可用。`ocr_configured` 与 `llm_configured` 只表示相应能力是否完成配置，"
        "不影响本接口的就绪判定。数据库不可用时返回 HTTP 503。"
    ),
    ("/api/v1/draft-reviews", "post"): (
        "创建起草检查任务。`target_file` 为待检查合同，`template_file` 为制式模板，"
        "`reference_files` 为一份或多份辅助资料。接口只创建任务并返回 HTTP 202，实际文件下载、"
        "解析、比对、事实抽取、规则检查与建议生成由 Worker 异步执行。"
        "辅助资料的运行时默认上限为 20 份，"
        "最终以部署配置 `MAX_REFERENCE_FILES` 为准。"
    ),
    ("/api/v1/final-comparisons", "post"): (
        "创建定稿比对任务。`baseline_file` 为审批申请版/原文件，"
        "`target_file` 为放款或盖章阶段待比对文件。"
        "接口返回 HTTP 202；调用方应使用响应中的 `status_url` 查询进度。"
    ),
    ("/api/v1/tasks", "get"): (
        "分页查询历史任务，可按任务类型、状态、调用方业务标识及创建时间范围筛选。"
        "`created_from`、`created_to` 使用 ISO 8601 日期时间。"
    ),
    ("/api/v1/tasks/{task_id}", "get"): (
        "查询任务状态、处理阶段、进度和安全错误详情。建议任务执行期间以 2～5 秒间隔轮询，"
        "直到状态进入 `SUCCEEDED`、`FAILED` 或 `CANCELLED`。"
    ),
    ("/api/v1/tasks/{task_id}/result", "get"): (
        "获取成功任务的正式结果（当前 `schema_version` 为 2.1）。任务尚未成功完成时返回 HTTP 409；"
        "失败原因应从任务详情接口读取。"
    ),
    ("/api/v1/tasks/{task_id}/retry", "post"): (
        "为一个 `FAILED` 任务创建新的重试任务。原任务不会被覆盖；新任务响应中的 `source_task_id`"
        "指向原任务。非失败状态调用时返回 HTTP 409。"
    ),
}


def api_response(data: Any, *, message: str = "success") -> dict[str, Any]:
    return {
        "code": "0",
        "message": message,
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": data,
        "error": None,
    }


ACCEPTED_EXAMPLE = api_response(
    {
        "task_id": "tsk_01M2EXAMPLE00000000000000",
        "task_type": "DRAFT_REVIEW",
        "status": "PENDING",
        "progress": 0,
        "created_at": "2026-09-01T10:00:00+08:00",
        "status_url": "/api/v1/tasks/tsk_01M2EXAMPLE00000000000000",
        "result_url": "/api/v1/tasks/tsk_01M2EXAMPLE00000000000000/result",
        "source_task_id": None,
    },
    message="accepted",
)

TASK_DETAIL_EXAMPLE = api_response(
    {
        "task_id": "tsk_01M2EXAMPLE00000000000000",
        "task_type": "DRAFT_REVIEW",
        "client_reference_id": "project-2026-001-contract-01",
        "status": "RUNNING",
        "stage": "FACT_EXTRACTION",
        "stage_message": "正在抽取合同事实",
        "progress": 60,
        "attempt_count": 1,
        "created_at": "2026-09-01T10:00:00+08:00",
        "started_at": "2026-09-01T10:00:02+08:00",
        "updated_at": "2026-09-01T10:01:30+08:00",
        "finished_at": None,
        "error": None,
    }
)

TASK_LIST_EXAMPLE = api_response(
    {
        "items": [
            {
                "task_id": "tsk_01M2EXAMPLE00000000000000",
                "task_type": "FINAL_COMPARE",
                "client_reference_id": "project-2026-001-final-01",
                "status": "SUCCEEDED",
                "progress": 100,
                "conclusion": "RISK_FOUND",
                "risk_count": 1,
                "review_count": 0,
                "legacy_statistics": False,
                "created_at": "2026-09-01T09:00:00+08:00",
                "finished_at": "2026-09-01T09:03:12+08:00",
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }
)

TASK_RESULT_EXAMPLE = api_response(
    {
        "schema_version": "2.1",
        "task_id": "tsk_01M2EXAMPLE00000000000000",
        "task_type": "FINAL_COMPARE",
        "conclusion": "RISK_FOUND",
        "summary": {
            "title": "合同版本比对完成",
            "description": "发现 1 项需关注的内容变更。",
            "statistics": {
                "risk_count": 1,
                "deletion_or_missing_count": 0,
                "addition_or_change_count": 1,
                "review_count": 0,
                "passed_check_count": 2,
                "legacy_statistics": False,
            },
        },
        "files": [
            {
                "file_id": "fil_baseline_example",
                "role": "BASELINE",
                "file_name": "审批申请版合同.docx",
                "safe_url": "https://files.example.com/approved.docx",
                "sha256": "d34db33fd34db33fd34db33fd34db33fd34db33fd34db33fd34db33fd34db33f",
                "page_count": 12,
                "parser_name": "local-docx",
                "parse_status": "SUCCEEDED",
                "parse_warnings": [],
                "parser_metadata": {},
            }
        ],
        "stamp_images": [],
        "risk_items": [
            {
                "risk_id": "risk_000001",
                "module_code": "VERSION_COMPARE",
                "risk_type": "ADDITION_OR_CHANGE",
                "change_type": "NUMERIC_CHANGED",
                "title": "租金金额发生变化",
                "description": "目标文件中的租金金额与审批申请版不一致。",
                "source_evidence": [],
                "related_diff_ids": ["diff_000001"],
                "related_rule_ids": [],
                "requires_manual_action": True,
                "analysis_advice": "请核对审批依据并确认金额变更是否已获授权。",
                "validation_status": "CONFIRMED",
                "validation_source": "RULE",
                "validation_reason_code": None,
            }
        ],
        "review_items": [],
        "passed_checks": [
            {
                "check_id": "check_000001",
                "module_code": "VERSION_COMPARE",
                "title": "主体名称一致",
                "description": "双方主体名称未发现差异。",
            }
        ],
        "diff_items": [],
        "fact_matrix": [],
        "rule_checks": [],
        "warnings": [],
        "advice": {"summary": "请人工确认金额变更。"},
        "metadata": {
            "execution_mode": "RULE_BASED",
            "workflow_version": "0.4.1",
            "rules_version": "0.4.1",
            "primary_model": None,
            "model_runs": [],
            "independent_review": None,
            "review_mode": "NOT_RUN",
        },
        "mock": False,
    }
)


ERROR_EXAMPLES = {
    "400": {
        "code": "INVALID_REQUEST",
        "message": "请求参数不合法",
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": None,
        "error": {
            "details": [
                {
                    "field": "target_file.url",
                    "reason": "url_parsing",
                    "message": "Input should be a valid URL",
                }
            ]
        },
    },
    "404": {
        "code": "TASK_NOT_FOUND",
        "message": "任务不存在",
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": None,
        "error": {"details": {"task_id": "tsk_missing"}},
    },
    "409": {
        "code": "TASK_NOT_FINISHED",
        "message": "任务尚未成功完成，结果不可用",
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": None,
        "error": {
            "details": {
                "task_id": "tsk_01M2EXAMPLE00000000000000",
                "current_status": "RUNNING",
            }
        },
    },
    "500": {
        "code": "INTERNAL_ERROR",
        "message": "服务内部错误",
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": None,
        "error": {"details": None},
    },
    "503": {
        "code": "SERVICE_NOT_READY",
        "message": "数据库尚未就绪",
        "request_id": "req_01M2EXAMPLE00000000000000",
        "data": None,
        "error": {"details": None},
    },
}


def response(
    content_schema: dict[str, Any], description: str, example: dict[str, Any]
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": content_schema, "example": example}},
    }


def error_response(status_code: str) -> dict[str, Any]:
    descriptions = {
        "400": "请求参数不合法",
        "404": "任务不存在",
        "409": "任务状态不允许当前操作",
        "500": "服务内部错误",
        "503": "服务尚未就绪",
    }
    return response(
        {"$ref": "#/components/schemas/ApiErrorResponse"},
        descriptions[status_code],
        ERROR_EXAMPLES[status_code],
    )


def operation_example(path: str, method: str) -> dict[str, Any] | None:
    examples = {
        ("/health", "get"): api_response({"status": "ok"}),
        ("/ready", "get"): api_response(
            {"status": "ready", "database": "ok", "ocr_configured": True, "llm_configured": True}
        ),
        ("/api/v1/draft-reviews", "post"): ACCEPTED_EXAMPLE,
        ("/api/v1/final-comparisons", "post"): copy.deepcopy(ACCEPTED_EXAMPLE),
        ("/api/v1/tasks", "get"): TASK_LIST_EXAMPLE,
        ("/api/v1/tasks/{task_id}", "get"): TASK_DETAIL_EXAMPLE,
        ("/api/v1/tasks/{task_id}/result", "get"): TASK_RESULT_EXAMPLE,
        ("/api/v1/tasks/{task_id}/retry", "post"): copy.deepcopy(ACCEPTED_EXAMPLE),
    }
    example = examples.get((path, method))
    if path == "/api/v1/final-comparisons" and example:
        example["data"]["task_type"] = "FINAL_COMPARE"
    if path == "/api/v1/tasks/{task_id}/retry" and example:
        example["data"]["source_task_id"] = "tsk_01M2SOURCE000000000000000"
    return example


def build_spec() -> dict[str, Any]:
    spec = copy.deepcopy(app.openapi())
    spec["info"] = {
        "title": "合同智能对比系统 API",
        "version": app.version,
        "description": """
本系统提供两类合同检查能力：**起草检查（DRAFT_REVIEW）**与**定稿比对（FINAL_COMPARE）**。

> 系统输出用于辅助业务人员定位差异、风险和待复核事项，不构成合同审查或法律意见。

## 调用流程

1. 调用创建接口并取得 HTTP `202` 响应中的 `task_id`、`status_url` 和 `result_url`。
2. 轮询 `status_url`；状态进入 `SUCCEEDED`、`FAILED` 或 `CANCELLED` 后停止轮询。
3. `SUCCEEDED` 时访问 `result_url` 获取正式结果；
   `FAILED` 时读取任务详情中的 `error`，必要时创建重试任务。

## 通用约定

- 请求与响应均使用 `application/json`，日期时间采用 ISO 8601 格式。
- 所有响应使用统一外层结构：`code` 为 `0` 表示请求成功，`request_id` 用于日志追踪。
- 调用方可传入请求头 `X-Request-ID`（仅限字母、数字、下划线、连字符，最长 64 位）；
  未提供或格式非法时由系统生成。响应头和响应体都会返回最终追踪 ID。
- 文件以 HTTP(S) URL 提交，服务端必须能从部署网络访问该 URL。
  带签名的下载地址应覆盖任务排队与处理时长；系统对外结果只返回脱敏后的 `safe_url`。
- 默认单文件上限为 200 MB；起草检查辅助资料默认最多 20 份，实际值以生产部署配置为准。
- 当前应用未在 OpenAPI 中定义业务鉴权头。
  生产环境应由甲方 API 网关、反向代理或网络访问控制实施鉴权；如部署另有约定，以部署配置为准。

## 任务状态

`PENDING` 等待处理；`RUNNING` 正在处理；`SUCCEEDED` 成功；`FAILED` 失败；`CANCELLED` 已取消。

## 常用业务错误码

| HTTP | 业务码 | 含义 |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | 字段格式、数量或业务约束不合法 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 409 | `TASK_NOT_FINISHED` | 任务尚未成功完成，结果不可读取 |
| 409 | `TASK_NOT_RETRYABLE` | 当前任务不是失败状态，不能重试 |
| 500 | `INTERNAL_ERROR` | 服务内部错误；请携带 `request_id` 排查 |
| 503 | `SERVICE_NOT_READY` | 数据库尚未就绪 |
""".strip(),
    }
    spec["servers"] = [
        {
            "url": "{scheme}://{host}:{port}",
            "description": "部署环境 API 地址",
            "variables": {
                "scheme": {"default": "http", "enum": ["http", "https"]},
                "host": {"default": "127.0.0.1"},
                "port": {"default": "8000"},
            },
        }
    ]
    spec["tags"] = [
        {"name": name, "description": TAG_DESCRIPTIONS[name]}
        for name in ("健康检查", "起草检查", "定稿比对", "任务管理")
    ]
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas["ApiErrorResponse"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["code", "message", "request_id", "data", "error"],
        "properties": {
            "code": {
                "type": "string",
                "description": "非零业务错误码",
                "example": "INVALID_REQUEST",
            },
            "message": {"type": "string", "description": "面向调用方的安全错误消息"},
            "request_id": {"type": "string", "description": "请求追踪 ID"},
            "data": {"type": "null"},
            "error": {"$ref": "#/components/schemas/ApiErrorBody"},
        },
    }
    schemas["RemoteFile"]["properties"]["url"]["description"] = (
        "HTTP(S) 文件下载地址；创建任务时保存，Worker 执行阶段下载。"
        "地址须在任务完成前保持有效，且必须能从服务端部署网络访问。"
    )
    schemas["DraftReviewCreate"]["properties"]["client_reference_id"]["description"] = (
        "调用方业务标识，可用于任务列表筛选和业务侧关联。"
    )
    schemas["FinalComparisonCreate"]["properties"]["client_reference_id"]["description"] = (
        "调用方业务标识，可用于任务列表筛选和业务侧关联。"
    )
    schemas["FinalCompareOptions"]["properties"]["ignore_formatting"]["description"] = (
        "是否忽略字体、字号、颜色等纯格式差异。"
    )
    schemas["FinalCompareOptions"]["properties"]["ignore_headers_footers"]["description"] = (
        "是否忽略页眉和页脚差异。"
    )
    schemas["FinalCompareOptions"]["properties"]["numeric_sensitive"]["description"] = (
        "是否对数字变化进行敏感识别。"
    )

    parameter_descriptions = {
        "task_id": "任务 ID。",
        "page": "页码，从 1 开始。",
        "page_size": "每页数量，范围 1～100。",
        "task_type": "任务类型：起草检查或定稿比对。",
        "status": "任务状态筛选。",
        "client_reference_id": "调用方业务标识精确筛选。",
        "created_from": "创建时间下界（含），ISO 8601 日期时间。",
        "created_to": "创建时间上界（含），ISO 8601 日期时间。",
    }

    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation["tags"] = [TAG_NAMES.get(tag, tag) for tag in operation.get("tags", [])]
            operation["description"] = OPERATION_DESCRIPTIONS[(path, method)]
            parameters = operation.setdefault("parameters", [])
            parameters.insert(
                0,
                {
                    "name": "X-Request-ID",
                    "in": "header",
                    "required": False,
                    "description": "可选请求追踪 ID；格式为 `[A-Za-z0-9_-]{1,64}`。",
                    "schema": {"type": "string", "maxLength": 64},
                    "example": "req_client_20260901_0001",
                },
            )
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            success_code = "202" if method == "post" else "200"
            example = operation_example(path, method)
            if example and success_code in responses:
                responses[success_code]["description"] = (
                    "任务已接受" if success_code == "202" else "请求成功"
                )
                responses[success_code]["content"]["application/json"]["example"] = example
            for parameter in parameters:
                name = parameter.get("name")
                if name in parameter_descriptions:
                    parameter["description"] = parameter_descriptions[name]
            responses.setdefault("500", error_response("500"))
            if path == "/ready":
                responses["503"] = error_response("503")
            if path.startswith("/api/v1/"):
                responses["400"] = error_response("400")
            if "{task_id}" in path:
                responses["404"] = error_response("404")
            if path.endswith("/result") or path.endswith("/retry"):
                responses["409"] = error_response("409")
            if path.endswith("/retry"):
                retry_error = responses["409"]["content"]["application/json"]["example"]
                retry_error["code"] = "TASK_NOT_RETRYABLE"
                retry_error["message"] = "仅失败任务允许重试"

    return spec


def extract_redoc_bundle(reference_html: Path) -> str:
    html = reference_html.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?: [^>]*)?>([\s\S]*?)</script>", html, flags=re.IGNORECASE)
    if not scripts or "Redoc" not in scripts[0]:
        raise RuntimeError("参考 HTML 中未找到可复用的 ReDoc standalone 脚本")
    return scripts[0]


def build_html(spec: dict[str, Any], redoc_bundle: str) -> str:
    spec_json = json.dumps(spec, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="description" content="合同智能对比系统 API 接口文档" />
  <title>合同智能对比系统 API 接口文档</title>
  <style>
    body {{ margin: 0; padding: 0; }}
    #redoc-container {{ min-height: 100vh; }}
  </style>
</head>
<body>
  <div id="redoc-container"></div>
  <script>{redoc_bundle}</script>
  <script>
    const contractReviewOpenApi = {spec_json};
    Redoc.init(contractReviewOpenApi, {{
      disableSearch: false,
      expandResponses: "200,202",
      hideDownloadButton: false,
      nativeScrollbars: false,
      pathInMiddlePanel: true,
      requiredPropsFirst: true,
      sortPropsAlphabetically: false,
      jsonSampleExpandLevel: 3,
      generatedPayloadSamplesMaxDepth: 5,
      theme: {{
        colors: {{ primary: {{ main: "#1677ff" }}, success: {{ main: "#16a34a" }} }},
        typography: {{ fontSize: "15px", lineHeight: "1.6", headings: {{ fontWeight: "650" }} }},
        sidebar: {{ width: "280px", backgroundColor: "#f7f9fc" }},
        rightPanel: {{ backgroundColor: "#18202b", width: "40%" }}
      }}
    }}, document.getElementById("redoc-container"));
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成合同智能对比系统单文件 ReDoc 接口文档")
    parser.add_argument("--reference-html", type=Path, required=True)
    parser.add_argument("--output-html", type=Path, required=True)
    parser.add_argument("--output-openapi", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    spec = build_spec()
    redoc_bundle = extract_redoc_bundle(args.reference_html)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(build_html(spec, redoc_bundle), encoding="utf-8")
    if args.output_openapi:
        args.output_openapi.parent.mkdir(parents=True, exist_ok=True)
        args.output_openapi.write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"generated_html={args.output_html.resolve()}")
    print(f"paths={len(spec['paths'])}")
    print(f"schemas={len(spec['components']['schemas'])}")


if __name__ == "__main__":
    main()
