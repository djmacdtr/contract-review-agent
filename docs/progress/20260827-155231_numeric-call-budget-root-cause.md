# Numeric 正式任务固定调用上限根因

日期：2026-08-27

## 结论

任务 `tsk_01M1127FS0ACRW0A3T0EBCMR63` 并非因六单元 numeric 批次的模型输出失败，而是命中固定的事实抽取逻辑调用上限。

- 任务在 `FACT_EXTRACTION / 75%` 失败。
- 持久化批次为 `batch_d29dfe2c9f5429fc72f03f26`，`unit_count=6`。
- 当前配置 `LLM_EXTRACTION_MAX_LOGICAL_CALLS_TOTAL=50`。
- 该任务恰好已保存 `50` 条 `numeric-v2 / SUCCEEDED` checkpoint 和 `3` 条 `profile-v2 / SUCCEEDED` checkpoint。
- `invoke_plan` 在达到逻辑调用上限时直接构造 `DYNAMIC_CHECK_INCOMPLETE`，因此对外详情没有保留“调用预算耗尽”这一真实原因。
- 任务事件显示 FACT_EXTRACTION 从 07:44:36 运行至 07:47:23；失败不是单批长时间阻塞。

三个最坏批次 Canary 均成功，说明六单元规划本身不是本次失败根因。

## 下一步边界

1. 保持 numeric 六单元规划和 `6 → 3 → 1` 截断恢复，不继续缩小批次。
2. 将首波计划调用与恢复调用分开计数：首波 cache miss 必须全部具备执行预算，恢复预算仍保持受限。
3. 执行前根据 profile/numeric/text 的实际 cache miss 数计算所需首波预算；超过硬上限时在零次 LLM 调用前以明确内部子码停止。
4. 达到运行时上限时记录 `EXTRACTION_CALL_BUDGET_EXHAUSTED`，不得继续包装成无来源的 `DYNAMIC_CHECK_INCOMPLETE`。
5. 修复后仅运行预算相关定向测试和零调用规划诊断，不再执行 Canary 或全量回归。
6. 正式验收只 retry 最新失败任务，复用其 50 条 numeric checkpoint，不创建全新任务。

本记录仅进行了只读数据库、配置和代码核查；未修改业务代码，未调用 LLM/OCR，未创建或重试任务。
