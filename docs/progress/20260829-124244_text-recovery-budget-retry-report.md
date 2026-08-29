# Text 恢复预算与深度边界修复记录

## 实施范围

- Text 独立 Map–Reduce 的单文档最低恢复预算调整为 3。
- Text 实际恢复深度限制为 2，允许 `16 → 8 → 4`；Numeric 使用原有配置。
- Text 恢复预算耗尽时保留外层 `TEXT_RECOVERY_BUDGET_EXHAUSTED` 和底层错误码。
- Worker 结构化失败日志及宿主机 retry 安全摘要补充 `underlying_failure_code`。
- 未修改 OCR、页码、模型参数、Prompt、证据校验、checkpoint 逻辑、公开 API 或 `.real-diagnostic-temp/`。

## 离线验证

- 定向测试：`6 passed`。
- Ruff：通过。
- `compileall`：通过。
- `git diff --check`：通过。
- Worker retry 集成测试未能进入测试体，测试环境解析 `postgres` 失败（`socket.gaierror`）；未修改业务代码规避该环境问题。

## 唯一正式恢复

- 来源任务：`tsk_01M15W20DRDW8ZHTC9N72XNMTZ`
- 新任务：`tsk_01M15X5XTYWVJ6MMBY7B47VKNS`
- 控制台任务列表：`/console/#/tasks`
- 控制台报告：`/console/#/tasks/tsk_01M15X5XTYWVJ6MMBY7B47VKNS/report`
- 执行方式：Docker Worker 保持停止，宿主机 Worker 执行；仅调用一次 retry，随后未再次 retry 或创建任务。
- 结果：`FAILED`，进度 `75%`，阶段 `FACT_EXTRACTION`。
- 首个安全错误：`chain=text`，`batch_depth=2`，`unit_count=4`，`batch_id=batch_161ca9fd8a106e60d1fcc815`，`failure_code=LLM_INVALID_JSON`，`failure_stage=FACT_EXTRACTION`。
- 网络摘要：7 次 LLM HTTP 请求，全部返回 `200`。
- checkpoint 摘要：来源 51 个；新任务物化 53 个。

## 未完成项

- 4 单元 Text 叶子批次仍返回非法 JSON，因已达到 Text 深度边界，本轮按止损规则停止。
- 任务未发布正式报告，因此尚未执行 39 项/4 项结果、页码、高亮、建议覆盖率及控制台报告验收。
- 未执行 commit、push、reset、clean，也未清理 `.real-diagnostic-temp/`。
