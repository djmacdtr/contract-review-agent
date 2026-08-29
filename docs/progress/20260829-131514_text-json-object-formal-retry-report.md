# Text `json_object` A/B 与正式恢复记录

## 实施

- 增加 `OpenAIContractLlmClient` 的内部 Text 专用响应格式和模型覆盖。
- Text 可单独使用 `json_object`；Numeric、映射、Advice 继续使用全局 `json_schema`。
- 保留 Text 的 Pydantic、unit、quote、证据和事实身份校验。
- 增加安全响应元数据：`finish_reason`、`content_chars`、`code_fence`、`json_error_position`。
- Text 诊断入口改为复用正式 `16 → 8 → 4` 批次树，单次调用、关闭 HTTP/结构重试、不写 checkpoint。
- 未修改公开 API、数据库 Schema、checkpoint 身份、OCR、页码或差异算法。

## 离线验证

- 客户端与诊断定向测试：`11 passed`。
- Text/Numeric 恢复回归：`6 passed`。
- Ruff、compileall、`git diff --check`：通过。

## Text Canary

- 来源任务：`tsk_01M15X5XTYWVJ6MMBY7B47VKNS`
- 批次：`batch_161ca9fd8a106e60d1fcc815`
- 文件：`项目方案确认函.docx`
- 结果：`SUCCEEDED`
- 调用：1 次；HTTP 200；`finish_reason=stop`。
- 模型：`GLM-5.3-Flash`
- 响应格式：`json_object`
- 结构校验：4 个单元、3 条响应事实、3 条通过证据校验、0 条丢弃。
- `checkpoint_written=false`，未改变来源任务状态。

## 唯一正式恢复

- 来源任务：`tsk_01M15X5XTYWVJ6MMBY7B47VKNS`
- 新任务：`tsk_01M15Z1CTEZ7FGQKAEM60NQNV1`
- 控制台任务列表：`/console/#/tasks`
- 控制台报告：`/console/#/tasks/tsk_01M15Z1CTEZ7FGQKAEM60NQNV1/report`
- Text 配置：`json_object`；未使用 Qwen fallback。
- 来源 checkpoint：53 个；新任务物化：55 个。
- 结果：`FAILED`，进度 `80%`，任务阶段 `CROSS_VALIDATE`。
- 首个安全错误：`failure_stage=FACT_MAPPING`、`chain=mapping`、`batch_depth=0`、`unit_count=23`、`failure_code=LLM_OUTPUT_TRUNCATED`。
- 映射请求：2 次 HTTP，全部返回 `200`；未继续调用。

## 未完成项

- 正式任务在跨资料映射批次截断，未发布报告；尚未进行差异、通过项、页码、局部高亮和建议覆盖率验收。
- 本轮不启动 Qwen Canary，不调整映射批次或模型参数，不再次 retry。
- Docker Worker 保持停止；API/PostgreSQL 健康。
- 未执行 commit、push、reset、clean，也未清理 `.real-diagnostic-temp/`。
