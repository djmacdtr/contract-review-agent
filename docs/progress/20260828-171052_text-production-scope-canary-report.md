# Text 生产范围 Canary 报告

## 基本信息

- 时间：2026-08-28 17:10:52 +08:00
- 状态：BLOCKED
- 分支：`feat/draft-review-multidoc`
- 代码提交：`5af48ac`
- 真实正式任务：未创建
- 外部 LLM 请求：2 次，均 HTTP 200

## 本次实现

- `scripts/expanded_fact_canary.py` 已改为复用正式规划范围：
  - TARGET 使用模板分析、模板差异候选构建和 `plan_text_candidate_batches`。
  - REFERENCE 使用 `plan_text_document_batches`。
- 离线重建结果：TARGET 49 个候选、4 批 `[16,16,16,1]`；REFERENCE 2 批 `[16,10]`。
- Canary 只选择正式范围内 payload 最大的 TARGET 和 REFERENCE 批次各调用一次。
- 多单元 `FACT_BATCH_SATURATED` 和 `LLM_OUTPUT_TRUNCATED` 保留为生产可恢复状态；未放宽单元、引用、身份、证据或 Schema 校验。

## Canary 结果

| 范围 | batch_id | 单元数 | 结果 | 安全摘要 |
|---|---|---:|---|---|
| TARGET | `batch_9bbc5e3d0350f7994ca41a24` | 16 | 通过 | `GLM-5.3-Flash`、`stop`、7 条事实 |
| REFERENCE | `batch_f57810a5ad92b58ff532c99f` | 16 | 阻塞 | `LLM_INVALID_JSON`、1 次请求、0 次结构重试 |

TARGET 请求已成功完成。REFERENCE 的非法 JSON 不是本轮已确认的 `has_more` 或截断可恢复信号，因此按唯一 Canary 止损规则停止；没有重放该批次，没有调用 retry，也没有创建正式任务。

## 验证

- 规划定向测试：3 passed，45 deselected。
- `compileall`：通过。
- `ruff check scripts/expanded_fact_canary.py`：通过。
- `git diff --check`：通过。
- 安全输出：`.real-diagnostic-temp/text-production-scope-canary-20260828.json`；不含合同正文、完整响应、URL 或密钥。

## 未完成项

- REFERENCE 正式范围批次仍需一次明确的结构化输出诊断或按既有生产纠错策略确认后，才能进入正式恢复任务。
- 因 Canary 未全部通过，本轮未停止/启动 Worker，未创建正式任务，未进行控制台验收。
- 保留现有未提交修改及 `.real-diagnostic-temp/`；未执行 reset、clean、commit、push。
