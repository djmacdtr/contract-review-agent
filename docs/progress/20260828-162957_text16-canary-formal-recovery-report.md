# Text 16 单元 Canary 与正式恢复门禁记录

## 结论

本轮唯一 text Canary 未通过，因此未创建正式恢复任务，未调用 retry 接口，也未追加模型请求。

## Canary 安全摘要

- 配置：`json_schema`、`LLM_NATIVE_STRUCTURED_OUTPUT=true`、text 最大 16 个结构单元、最大输出 8192 tokens、payload 最大 24000 字符、并发 1。
- Canary 数量：1。
- chain：`text`。
- batch_id：`batch_5c6fa953dbdee61ded0b2f2b`。
- unit_count：16。
- candidate_count：0。
- HTTP 调用：1 次；HTTP 200：1 次。
- finish_reason：未形成可接受结果。
- 结构重试：0 次。
- 安全失败码：`FACT_BATCH_SATURATED`。
- 外层错误码：`LLM_EXTRACTION_EVIDENCE_INVALID`。
- 耗时：约 20.735 秒。

## 正式验收状态

- 来源任务：`tsk_01M13PH5H5EAWJXJRCFKH00PH0`。
- 正式恢复任务：未创建。
- 39 项差异、4 项通过、页码、建议覆盖率：未执行验收。
- 未修改 OCR、页码、checkpoint、差异算法或公开接口。

## 未完成项

需要在后续独立处理 text 批次饱和/证据校验问题后，再决定是否按 text 12 单元重新进行门禁；本轮不再调用甲方 LLM，不创建恢复任务。
