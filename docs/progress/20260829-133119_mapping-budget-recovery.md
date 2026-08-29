# 任务进度：映射输出预算提升与唯一恢复验收

## 基本信息

- 时间：2026-08-29 13:31:19 +08:00
- 状态：PARTIAL
- 任务类型：FIX
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：5af48ac
- 工作树状态：dirty；保留既有页码、抽取、恢复、映射及进度记录修改

## 用户目标

将 `map_facts` 和 `review_mappings` 的输出上限从 4096 调整为 12288，在不改变其他抽取链路的前提下，通过宿主机 Worker 从指定失败任务执行唯一一次 checkpoint 恢复。

## 本次完成

- 将映射及映射评审共用的内部输出上限调整为 12288。
- 增加请求级定向测试，确认映射模型、`json_schema` 和输出预算正确，且 Profile、Text、Advice 预算未变化。
- 使用宿主机 Worker 从指定来源任务创建并执行唯一一次恢复任务。
- 恢复任务失败后停止，未 retry、未创建第二个任务、未再次调用 Text Canary 或事实抽取开发流程。

## 修改文件

- `app/adapters/llm/openai_client.py`：将 `_MAPPING_MAX_OUTPUT_TOKENS` 从 4096 调整为 12288。
- `tests/unit/test_openai_llm_client.py`：增加映射/映射评审请求预算和其他操作预算不变的定向测试。
- `docs/progress/20260829-133119_mapping-budget-recovery.md`：记录本次修改与唯一恢复结果。

## 接口、数据和配置变化

- API：未修改。
- 数据库/迁移：未修改。
- 配置：未修改 `.env`；仅修改映射客户端内部常量。
- 兼容性：Numeric、Text、Profile、Advice、Prompt、Schema、校验、checkpoint 和公开接口未因本任务改变。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest tests/unit/test_openai_llm_client.py -k "mapping_operations_use_expanded_budget_only_for_mapping or mapping_schema_validation or numeric_output_tokens" -q` | 通过 | 3 passed，40 deselected |
| `ruff check app/adapters/llm/openai_client.py tests/unit/test_openai_llm_client.py` | 通过 | All checks passed |
| `python -m compileall -q app/adapters/llm/openai_client.py` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅有 Git 行尾转换提示 |

## Docker 与运行状态

- API：运行且健康，`127.0.0.1:8000`。
- Worker：Docker Worker 保持停止；本次由宿主机 Worker 执行并已退出。
- PostgreSQL：运行且健康，`127.0.0.1:15432`。
- 控制台：任务地址已生成，但未发布报告。
- 最终是否保持运行：API/PostgreSQL 保持运行，宿主机临时 Worker 和文件服务已停止。

## 唯一恢复任务

- 来源任务：`tsk_01M15Z1CTEZ7FGQKAEM60NQNV1`
- 来源成功 checkpoint：55
- 新任务：`tsk_01M15ZWY9NMWZEQ5K9DWK5W56V`
- 新任务实际成功 checkpoint：3
- 映射请求：1 次；HTTP 状态：200
- `finish_reason`：失败任务未持久化模型运行明细，安全报告和 `task_event` 均未提供该字段，因此记为“不可得”，不作推断。
- 结果阶段：`RULE_CHECKING`，进度 85
- 任务状态：`FAILED`
- 首个安全错误：`failure_code=MAPPING_CONFIDENCE_INVALID`
- 失败外层码：`DYNAMIC_CHECK_INCOMPLETE`
- 控制台任务列表：`/console/#/tasks`
- 控制台报告：`/console/#/tasks/tsk_01M15ZWY9NMWZEQ5K9DWK5W56V/report`

## 重要决策

- 按用户止损要求，恢复失败后没有再次调用 retry、LLM 或外部探针。
- 没有将 `MAPPING_CONFIDENCE_INVALID` 改写为映射预算问题；本次任务未生成可供差异、通过项、页码和建议验收的正式结果。

## 已知问题与风险

- 映射响应在 HTTP 200 后触发 `MAPPING_CONFIDENCE_INVALID`，具体模型 `finish_reason` 未被失败任务安全持久化。
- 未生成正式报告，因此 39 项差异、通过项、页码、证据和建议覆盖率本轮均未完成验收。
- 来源 55 个 checkpoint 与新任务实际物化数量不一致，未在本轮扩大范围处理。

## 下一步建议

1. 由后续任务针对 `MAPPING_CONFIDENCE_INVALID` 做最小安全诊断；不要重复本次 retry。
2. 在不暴露正文的前提下补齐映射失败响应元数据持久化，再决定是否需要新的恢复策略。

## 下一会话首先阅读

- `app/adapters/llm/openai_client.py`
- `app/workflows/draft_review.py`
- `scripts/retry_failed_draft_report_host.py`
- `docs/progress/20260829-133119_mapping-budget-recovery.md`

## 交接摘要

映射和映射评审输出上限已改为 12288，定向测试及静态检查通过。唯一宿主机恢复任务 `tsk_01M15ZWY9NMWZEQ5K9DWK5W56V` 在 `RULE_CHECKING` 以 `MAPPING_CONFIDENCE_INVALID` 失败，映射仅请求 1 次且 HTTP 200。未再次 retry，API/PostgreSQL 健康，Docker Worker 停止，正式报告尚未生成。
