# Text 饱和拆分与恢复预算隔离

## 背景

任务 `tsk_01M1GD1QFJH5YD0XMW2SW5KQ8R` 在 Text 阶段因 `TEXT_RECOVERY_BUDGET_EXHAUSTED` 失败。首个底层错误为 `FACT_BATCH_SATURATED`，模型返回正常 `finish_reason=stop`，表明 `has_more=true` 是容量信号，而非非法响应或证据不可靠。

## 修改

- `FACT_BATCH_SATURATED` 的结构拆分继续受 `TEXT_MAX_RECOVERY_DEPTH`、全局逻辑调用上限和单文档绝对调用上限约束，但不再消耗普通异常恢复预算。
- `LLM_INVALID_JSON`、Schema、证据等异常仍使用原有恢复预算和安全失败策略。
- 保留既有 `recovery_count` 统计，并增加内部 `saturation_split_counts` 诊断统计；未修改公开 API、数据库 Schema、checkpoint 身份或其他抽取链路。

## 验证

- Text Client 与 grounding filter：`61 passed`
- Text 恢复/饱和拆分定向场景：`5 passed`
- 变更文件 Ruff：通过
- compileall：通过
- `git diff --check`：通过

未触发真实任务重试，未创建新任务，未清空或修改历史报告。
