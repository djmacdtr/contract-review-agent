# Numeric 思考模式关闭与单批恢复记录

## 本轮范围

- 目标：诊断单候选 Numeric 截断并验证 Numeric 专用模型路由，不修改拆分深度、证据校验、页码算法或公开接口。
- 来源任务：`tsk_01M16HJ4ZB4ZXBEG2FKRZF7XSB`
- 精确批次：`batch_ffa43f22a1bf7903d86d05dd`
- Docker Worker：保持停止；诊断和恢复均由宿主机 Worker/进程执行。

## 离线实现与检查

- Numeric 请求新增安全响应诊断：`finish_reason`、正文字符数、推理字符数、聚合 usage、实际 `max_tokens`。
- Numeric 首次请求关闭思考模式；仅单候选遇 HTTP 400 时允许以 8192 tokens、默认思考模式执行兼容回退。
- Numeric wire Schema 仅保留候选索引、语义、类型、决策、原因码和置信度；应用层候选全集、证据和身份校验不变。
- Numeric 模型支持内部覆盖，Text、映射和 Advice 的模型路由不受影响。
- 定向客户端测试：10 passed。
- Numeric 恢复/空批次定向测试：8 passed。
- Ruff、compileall、`git diff --check`：通过。
- 一个既有旧测试仍携带已废弃的 Numeric `has_more` 字段，未纳入本轮修改，也未运行全量测试。

## 唯一外部 Canary

- GLM：`GLM-5.3-Flash`，单次精确批次调用成功。
- HTTP 状态：200；`finish_reason=stop`；`max_tokens=2048`；正文字符数185；推理字符数0；usage 为 prompt 646、completion 47、total 693。
- 结果：`SUCCEEDED`，`llm_calls=1`，`request_attempts=1`。
- 因 GLM Canary 已成功，未调用 Qwen Canary。

## 唯一恢复结果

- 通过既有 retry 接口创建并执行唯一任务：`tsk_01M16MWEMN7SAVK42HER1NRNVB`。
- 来源成功 checkpoint 统计：52；恢复任务成功 checkpoint 统计：55。
- 宿主机 Worker 执行耗时约467.625秒；LLM HTTP 调用23次，全部 HTTP 200；Numeric 未出现截断失败。
- 任务最终失败于页码公开证据门禁：
  - 任务错误：`DOCX_PAGE_LOCATION_INCOMPLETE`
  - 首个安全子码：`PUBLIC_LOCATION_UNMAPPED`
  - 任务阶段：`PUBLIC_EVIDENCE_MAPPING`
- 因页码门禁失败，没有发布正式结果；未执行第二次 retry，也未创建第二个任务。
- 结果安全报告：[20260829-193000_numeric-thinking-recovery.json](D:/work/contract_review/contract-review-agent/docs/progress/20260829-193000_numeric-thinking-recovery.json)
- 控制台任务列表：`/console/#/tasks`
- 任务报告路径：`/console/#/tasks/tsk_01M16MWEMN7SAVK42HER1NRNVB/report`

## 未完成项

- 页码公开证据映射仍有 `PUBLIC_LOCATION_UNMAPPED`，本轮按止损规则不修复、不重跑。
- Docker Worker 继续保持停止，等待后续明确授权的页码门禁处理。
