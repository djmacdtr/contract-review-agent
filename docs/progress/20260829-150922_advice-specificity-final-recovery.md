# Advice 动态具体性接入与最终恢复记录

## 本轮完成

- 将动态业务锚点提取和具体性判定集中到 `app/results/advice.py`。
- DRAFT_REVIEW Advice 逐项复用共享校验；`NOT_SPECIFIC` 仅触发一次单项补偿，补偿仍失败时保留 fallback，不阻断报告。
- Canary 状态允许 `7/8 + NOT_SPECIFIC` 作为可恢复结果；本轮没有重复调用 Canary。
- 保留 FINAL_COMPARE 的默认 Advice 校验语义，不启用动态具体性门禁。

## 离线门禁

- 定向 pytest：`76 passed`。
- Ruff：通过。
- 相关 Python `compileall`：通过。
- `git diff --check`：通过。

## 既有 Canary 依据

- 既有 8 风险生产范围 Canary：8 条返回、7 条满足动态具体性，四类 Advice 质量错误均为 0；按本轮规则可恢复。
- 本轮未重新调用模型或外部 Canary。

## 唯一恢复状态

- 计划来源：`tsk_01M15ZWY9NMWZEQ5K9DWK5W56V`。
- 恢复入口安全阻止创建任务：来源已经存在成功子任务 `tsk_01M161GFY6Q7YSP07R877XQM2B`，不满足来源必须为失败且不存在既有 retry 的唯一性条件。
- 因此本轮没有发送新的 retry 请求，也没有创建第二个任务。
- 已有子任务安全结果：`SUCCEEDED / COMPLETED / 100`、39 个风险、3 个通过、工作流 `0.7.0`、规则 `0.6.0`；已有结果未启用页码补全，Advice 为 13 个模型建议、26 个 fallback。
- 控制台路径：`/console/#/tasks`；已有报告路径：`/console/#/tasks/tsk_01M161GFY6Q7YSP07R877XQM2B/report`。

## 未完成项

- 本轮未形成新的页码启用最终任务；需要一个尚未产生 retry 子任务的失败来源，或由用户明确授权突破“不得创建第二个任务”的唯一性约束后再继续。
- 保留工作区现有修改和 `.real-diagnostic-temp/`，未执行 commit、push、reset、clean 或清理操作。
