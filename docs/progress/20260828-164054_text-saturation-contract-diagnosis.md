# 任务进度：Text 饱和协议诊断

## 基本信息

- 时间：2026-08-28 16:40:54 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改、历史进度和 `.real-diagnostic-temp/`

## 用户目标

解释 text 16 单元及两个 8 单元 Canary 连续触发 `FACT_BATCH_SATURATED` 的原因，判断项目是否因校验过严而长期无法闭环。

## 本次完成

- 核对 text wire Schema、Prompt、响应展开和恢复逻辑。
- 确认 text Schema 只允许最多 12 项，响应协议没有 `has_more` 或等价完整性字段。
- 确认生产代码使用 `len(response.items) >= max_items` 作为饱和依据，因此任何合法返回恰好 12 项的结果都会被判为失败。
- 确认两个 8 单元 Canary 均命中同一确定性边界，而非 HTTP、上下文、JSON 或证据引用失败。
- 发现现有未提交测试已改为接受恰好 12 项，但生产实现仍拒绝，当前代码与测试意图不一致；本轮 6 项定向测试未覆盖该用例。

## 修改文件

- `docs/progress/20260828-164054_text-saturation-contract-diagnosis.md`：记录只读诊断结论。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| `TextFactExtraction` | 确认 | `items` 最大长度为 12，无完整性标志 |
| `_text_fact_response_schema()` | 确认 | 仅设置 `maxItems`，不表达是否存在更多事实 |
| `expand_text_fact_response()` | 确认问题 | `len(items) >= max_items` 无条件抛出 `FACT_BATCH_SATURATED` |
| 相关测试 diff | 不一致 | 测试预期接受 12 项，生产代码仍拒绝 |

## Docker 与运行状态

- 本次未改变 API、Worker、PostgreSQL 或控制台状态。

## 重要决策

- 严格 quote、unit_id、证据回查和 JSON Schema 校验应继续保留；它们不是当前阻塞原因。
- 不能再以“结果数量等于上限”直接证明存在遗漏。
- text wire 响应应增加必填完整性标志，例如 `has_more`；仅 `has_more=true` 时触发二分恢复，`has_more=false` 时允许恰好 12 项通过。
- text wire Schema 应只包含 Prompt 实际要求的紧凑字段，避免可选长字段放大输出。

## 已知问题与风险

- `has_more` 协议及紧凑 wire Schema 尚未实现和验证。
- 正式恢复任务仍未创建；控制台正式报告尚未闭环。

## 下一步建议

1. 为 text 内部 wire 响应增加必填 `has_more: bool`，不改变公开 API Schema。
2. 饱和规则改为：`has_more=true` 才拆分；`has_more=false` 时允许 `len(items)==max_items`。
3. 保留 maxItems、逐项 quote 回查、unit_id、事实身份和 Pydantic 严格校验。
4. text wire Schema 移除 Prompt 未要求的长可选字段，由程序回填固定属性。
5. 用 Mock 定向覆盖完整/有剩余/非法证据/二分恢复后，再只重放两个既有 8 单元 Canary；通过后创建唯一正式恢复任务。

## 下一会话首先阅读

- `docs/progress/20260828-163802_text-balanced-recovery-canary-report.md`
- `docs/progress/20260828-164054_text-saturation-contract-diagnosis.md`
- `app/adapters/llm/schemas.py`
- `app/adapters/llm/openai_client.py`
- `app/draft_review/facts.py`
- `app/draft_review/extraction.py`

## 交接摘要

当前 text 连续失败来自饱和协议缺陷：Schema 最多 12 项，代码却把恰好 12 项无条件判为不完整，而模型没有字段声明是否仍有遗漏。严格证据校验无需放宽；应增加 `has_more` 并只在其为 true 时二分。现有测试和生产实现已出现预期不一致，下一步应先离线修正该内部协议，再做两个 8 单元恢复 Canary。
