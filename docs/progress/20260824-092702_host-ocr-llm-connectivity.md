# 任务进度：宿主机 OCR 与 LLM 连通性测试

## 基本信息

- 时间：2026-08-24 09:27:02 +08:00
- 状态：PARTIAL
- 任务类型：DIAGNOSE / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保留既有未提交开发修改，本次只新增诊断记录

## 用户目标

不经过 Docker，在本机 Python 环境中分别使用简单文件测试 OCR 和 LLM 连通性。

## 本次完成

- 在系统临时目录生成完全合成的单页 PDF 和 DOCX，未读取或发送真实合同。
- 使用宿主机 `.venv` 和当前 `.env` 直接执行 OCR `scan` probe。
- 使用宿主机 LLM Client 依次执行模型列表探测和合成 DOCX 事实抽取；抽取失败后未继续独立评审。
- 两个合成临时文件均已安全删除并确认不存在。

## 修改文件

- `docs/progress/20260824-092702_host-ocr-llm-connectivity.md`：记录本次宿主机诊断。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无；仅读取当前被 Git 忽略的 `.env`，未输出地址、凭据或完整响应。
- 兼容性：无。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 宿主机 `scripts/ocr_live_probe.py --mode scan <synthetic.pdf>` | 通过 | 1 页、2 blocks、0 tables；engine `3.20.11`；服务耗时 628 ms；响应 9,774 bytes；平均/最低置信度均为 0.988 |
| 宿主机 LLM `probe_models()` | 失败并安全映射 | `LLM_UPSTREAM_ERROR`；未发送文件内容 |
| 合成 DOCX 本地解析 → `extract_facts()` | 失败并安全映射 | 本地 DOCX 解析完成；模型调用返回 `LLM_UPSTREAM_ERROR` |
| `review_facts()` | 未执行 | 抽取未通过，不调用独立评审模型 |
| 临时文件清理 | 通过 | 合成 PDF、DOCX 均已删除，`exists=false` |

## Docker 与运行状态

- 本次未启动、停止或调用任何 Docker 服务。
- API、Worker、PostgreSQL 保持进入本次任务前的停止状态。
- 最终是否保持运行：否。

## 重要决策

- OCR 与 LLM 使用相互独立的宿主机调用验证，避免 Docker 网络影响判断。
- LLM 模型列表失败后仍执行一次合成 DOCX 的直接 completion，以排除仅 `/models` 端点异常；两个入口均得到同一上游错误。

## 已知问题与风险

- 宿主机 OCR 链路已经可用，不能据此替代最终部署环境中的容器网络验收。
- LLM 模型列表和 chat completion 均为 `LLM_UPSTREAM_ERROR`，说明当前问题不只限于 `/v1/models`，独立评审与完整 HYBRID 仍无法验证。

## 下一步建议

1. 由 LLM 网关管理员检查上游服务状态、反向代理和 OpenAI 兼容 `/v1/models`、`/v1/chat/completions` 路由。
2. 上游修复后先在宿主机重跑同样的无正文模型列表和单份合成 DOCX；成功后再验证独立评审模型。
3. 不需要继续排查宿主机 OCR；后续只需在实际部署网络中补一次同类合成 probe。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260824-091905_dynamic-fact-numeric-closure.md`
- `docs/progress/20260824-092702_host-ocr-llm-connectivity.md`
- `app/adapters/llm/openai_client.py`
- `scripts/ocr_live_probe.py`

## 交接摘要

宿主机 OCR 真实调用成功：1 页、2 blocks、置信度 0.988、服务耗时 628 ms。
LLM `/v1/models` 和合成 DOCX chat completion 均返回 `LLM_UPSTREAM_ERROR`。
抽取失败后未调用评审模型；未发送真实合同或输出凭据。
两个合成临时文件已删除；Docker 和数据库均未改动。
