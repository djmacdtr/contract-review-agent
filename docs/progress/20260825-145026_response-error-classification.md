# 任务进度：响应错误分类修正与三文件诊断

## 基本信息

- 时间：2026-08-25 14:50:26 +08:00
- 状态：PARTIAL
- 任务类型：FIX / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 基线提交：`ab4f17a fix(draft-review): preserve safe validation diagnostics`
- 工作树状态：dirty；包含本次分类修正、既有未提交进度记录和本记录，`.real-diagnostic-temp` 未触碰。

## 本次修改

- `_dynamic_failure_code()` 不再把所有未知 `LlmClientError` 统一归为 Schema 错误。
- 现有错误映射调整为：
  - `LLM_INVALID_JSON` → `LLM_RESPONSE_JSON_INVALID`
  - `LLM_RESPONSE_INVALID` → `LLM_RESPONSE_ENVELOPE_INVALID`
  - `LLM_SCHEMA_INVALID` → `LLM_RESPONSE_SCHEMA_INVALID`
  - 上游错误继续映射为 `LLM_UPSTREAM_FAILED`。
- 工作流内部日志保留原始 `LlmClientError.code`；公开错误、API、数据库和业务检查逻辑未改变。
- 未修改 Prompt、抽取 Schema、ValueType、数组上限或业务字段清单。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 Python pytest | 通过 | 152 passed；1 个既有 LangGraph 弃用告警 |
| 变更 Python 文件 Ruff | 通过 | 无 lint 错误 |
| 变更 Python 文件定向 `compileall` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误 |
| 全仓 pytest / Docker / OCR / 前端 / MiniMax | 未执行 | 按范围排除 |

离线测试覆盖 JSON 语法错误、响应 envelope 错误、Pydantic Schema 错误、未知内部错误码、原始错误码日志保留和公开 `DYNAMIC_CHECK_INCOMPLETE` 边界。

## 唯一一次受控三文件真实诊断

固定使用目标合同、同名模板和 `项目方案确认函.docx`，设置 `LLM_SAME_MODEL_DIAGNOSTIC=true`、`LLM_MAX_OUTPUT_TOKENS=4096`、`LLM_STRUCTURE_RETRY_ATTEMPTS=1`、OCR 关闭；未增加辅助文件，总 LLM 请求上限 16 次，首次失败立即停止。

- 实际 LLM 请求：3 次；其中结构纠错 1 次。
- 失败阶段：`FACT_EXTRACTION`，目标合同第 2 个抽取分块。
- 总耗时：约 285,374 ms。
- 请求字符数：合计 145,628，单次最大 59,123。
- 响应字符数：合计 29,083。
- `finish_reason`：2 次 `length`、1 次 `stop`；截断：是。
- usage tokens：prompt 32,749；completion 9,396；total 42,145。
- 原始内部错误码：`LLM_INVALID_JSON`。
- 安全错误类别：`LLM_RESPONSE_JSON_INVALID`。
- `validation_summary`：不适用/为空；本次不是 Pydantic 字段约束失败。
- 未进入事实评审、跨文档映射、映射评审、语义规划、数值执行或建议生成。
- 正式差异、左右证据和 AI 建议：0 / 未生成。

本次只记录安全聚合指标和既有错误码；未输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL。

## 结论与下一步

- 摘要缺失原因已缩小为 JSON 输出不稳定/响应被截断，而非 Pydantic Schema 字段错误。
- 本阶段不继续修改 Prompt 或 Schema；可在后续阶段评估缩小抽取分块或保留一次 JSON 纠错的最小运行策略。
- 本记录与分类修正可形成后续本地阶段提交；不推送、不扩展辅助文件。
