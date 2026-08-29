# 任务进度：REFERENCE 非法 JSON 门禁诊断

## 基本信息

- 时间：2026-08-28 17:12:43 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改、历史进度和 `.real-diagnostic-temp/`

## 用户目标

解释正式范围 Canary 中 TARGET 成功、REFERENCE 因 `LLM_INVALID_JSON` 阻塞的原因，并判断是否仍是业务校验规则问题。

## 本次完成

- 核对 Canary 安全结果：TARGET 16 单元成功并产生 7 条事实；REFERENCE 16 单元 HTTP 200，但响应无法解析为 JSON。
- 确认该错误不是 unit、quote、身份、证据或 `has_more` 校验造成，而是模型/网关单次返回内容不符合 JSON。
- 对比 Canary 与正式恢复语义：正式工作流将多单元 `LLM_INVALID_JSON` 纳入可恢复集合并执行平衡二分；Canary 的 `RECOVERABLE_TEXT_CODES` 只包含 `FACT_BATCH_SATURATED` 和 `LLM_OUTPUT_TRUNCATED`。
- 确认 Canary 将正式流程可恢复的错误误标为 `recoverable=false`，因此本次不应继续阻断正式恢复任务。

## 修改文件

- `docs/progress/20260828-171243_reference-invalid-json-gate-diagnosis.md`：记录只读诊断结论。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| TARGET 正式范围 Canary | 通过 | 16 单元、7 条事实、HTTP 200、`finish_reason=stop` |
| REFERENCE 正式范围 Canary | 可恢复 | 16 单元、HTTP 200、`LLM_INVALID_JSON` |
| 正式 `recovery_groups()` 与 recoverable code | 确认 | 多单元非法 JSON 可拆成 8+8 |
| Canary `RECOVERABLE_TEXT_CODES` | 不一致 | 缺少 `LLM_INVALID_JSON` 等正式可恢复代码 |

## Docker 与运行状态

- 本次未改变 API、Worker、PostgreSQL 或控制台状态。

## 重要决策

- 不放宽业务证据校验，也不重复调用已执行的 REFERENCE Canary。
- Canary 门禁应与正式恢复语义一致；多单元 `LLM_INVALID_JSON` 应标记为 `RECOVERABLE`。
- 当前已有足够证据进入唯一正式恢复任务；只有二分后不可再拆的子批次仍非法 JSON，才构成真实阻塞。

## 已知问题与风险

- Canary 可恢复代码集合尚未与正式工作流统一，未来可能再次漂移。
- GLM/vLLM 的 `json_schema` 仍可能偶发返回非法 JSON，需依靠正式二分和 checkpoint 恢复，而不能假设绝对不会发生。

## 下一步建议

1. 让 Canary 复用正式恢复判断，至少补齐多单元 `LLM_INVALID_JSON`，并增加定向测试防止再次漂移。
2. 不重跑本次 Canary；将现有结果重新判定为 TARGET `SUCCEEDED`、REFERENCE `RECOVERABLE`。
3. 使用宿主机 Worker，从最新失败任务创建唯一正式恢复任务，保持 json_schema、8192 输出、numeric 12、text 16、payload 24000、并发 1。
4. 正式任务若在可拆分父批次失败，由生产流程自动二分；仅在不可恢复叶子失败时停止。

## 下一会话首先阅读

- `docs/progress/20260828-171052_text-production-scope-canary-report.md`
- `docs/progress/20260828-171243_reference-invalid-json-gate-diagnosis.md`
- `scripts/expanded_fact_canary.py`
- `app/draft_review/extraction.py`

## 交接摘要

REFERENCE 的 `LLM_INVALID_JSON` 是 HTTP 200 下的模型格式异常，不是业务证据校验失败。正式流程会把 16 单元非法 JSON 二分恢复，但 Canary 漏列该错误并错误阻断。无需再调用 Canary；同步门禁语义后即可进入唯一正式恢复任务。
