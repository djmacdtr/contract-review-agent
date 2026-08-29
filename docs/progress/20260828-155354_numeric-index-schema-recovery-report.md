# 数值候选索引 Schema 与唯一恢复记录

日期：2026-08-28

## 实现

- 数值候选 payload 增加 `required_decision_count`。
- 数值候选响应 Schema 按本批候选数设置 `minItems=maxItems`。
- `candidate_index` 在数值专用 wire Schema 中设为必填整数，并限制为当前批次的索引枚举。
- 保留程序侧严格全集校验，漏项不转为 `IGNORE`。
- 增加安全索引统计：`expected_count`、`returned_count`、`missing_index_count`、`duplicate_index_count`、`invalid_index_count`。
- 诊断入口支持显式批次，仅在来源失败详情与重建批次完全一致时调用一次。

## 离线门禁

- 动态 Schema、索引统计和请求 payload 定向测试：3 passed。
- 严格候选全集回归：1 passed。
- `compileall`、变更相关 Ruff、`git diff --check`：通过。
- 未运行全量测试，未调用 OCR。

## 唯一 Canary

- 来源任务：`tsk_01M13MBF329VRRKTKA4DRR0K04`
- 批次：`batch_82f33fc688268e0620568ec0`
- 重建规模：6 个结构单元、1 个数值候选。
- LLM 调用：1 次。
- 结果：成功，严格索引校验通过。
- 安全输出：`.real-diagnostic-temp/numeric-batch-canary-20260828-index-schema.json`。

## 唯一 checkpoint 恢复

- 通过公开 retry 接口创建唯一恢复任务：`tsk_01M13NE8NXXFAGC1XFQVQC3303`。
- 来源任务：`tsk_01M13MBF329VRRKTKA4DRR0K04`。
- 来源成功 checkpoint：8 条（3 条 profile、5 条 numeric）。
- 新任务成功 checkpoint：78 条。
- LLM HTTP 调用：102 次，其中 80 次返回 200、22 次返回 500。
- 结果：`FAILED / FACT_EXTRACTION / 75`。
- 首个安全失败：`batch_depth=1`、`unit_count=1`、`batch_id=batch_79e5768df5a7920335c383b2`、`failure_code=LLM_UPSTREAM_ERROR`。
- 安全输出：`.real-diagnostic-temp/numeric-schema-recovery-20260828.json`。
- 未调用第二次 retry，未创建第二个恢复任务。

## 当前结论

数值索引 Schema 修复已通过离线测试和指定批次 Canary。唯一恢复在后续单结构 numeric 请求遇到上游错误后失败，当前不能宣称正式任务闭环；不再扩大本轮范围。Docker Worker 已恢复，API、PostgreSQL 和 Worker 均处于运行状态。工作区和 `.real-diagnostic-temp/` 保留，未执行 commit、push、reset 或 clean。
