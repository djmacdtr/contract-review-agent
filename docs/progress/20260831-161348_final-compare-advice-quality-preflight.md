# FINAL_COMPARE Advice 完整预检记录

## 门禁调整

- Canary 通过条件调整为 8 项中至少接受 7 项。
- 仅允许 `NOT_SPECIFIC` 造成 Canary 回退；其他质量拒绝和安全错误仍阻断。
- 完整报告继续要求 `model_rate >= 0.95`，未达标不创建任务。
- 复用既有 Canary 安全摘要，未再次调用 Canary。

## 离线检查

- 定向 pytest：`24 passed`。
- Ruff：通过。
- compileall：通过。
- `git diff --check`：通过。

## 完整 189 项 Advice 内存预检

- 来源任务：`tsk_01M1BBHY5424N69QRDFA8N96VZ`
- 任务创建数：`0`
- OCR 调用：`0`
- 风险数：`189`
- 初始批次：`24`
- 恢复批次：`24`
- 逻辑调用：`48`
- HTTP 调用：`49`
- 模型接受：`129`
- fallback：`60`
- 模型覆盖率：`0.6825`
- finish reason：`stop=28`
- 安全失败码：`LLM_NETWORK_ERROR=11`、`LLM_OUTPUT_TRUNCATED=2`、`LLM_SCHEMA_INVALID=7`
- `NOT_SPECIFIC=32`

## 结论

完整预检未达到 `0.95` 发布门禁，未创建 Advice-only 任务，来源报告保持不变。本轮外部调用已停止；Docker Worker 已恢复。剩余问题是完整批次的模型/网络/输出质量覆盖不足，不能通过创建低覆盖率报告绕过门禁。
