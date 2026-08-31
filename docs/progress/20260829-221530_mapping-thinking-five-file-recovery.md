# Mapping 思考模式关闭与五文件恢复记录

## 状态

已完成：唯一 PDF Mapping Canary、唯一公开 retry、零 OCR/零 LLM 页码收口，以及 Docker Worker 恢复。

## 变更与离线门禁

- `map_facts` 和 `review_mappings` 显式关闭模型思考模式，保持 GLM-5.3-Flash、`json_schema` 和 Mapping `12288` 输出上限不变。
- Mapping 失败边界补充安全聚合字段：`finish_reason`、`content_chars`、`reasoning_content_chars`、`usage`、`max_tokens`、HTTP 状态及既有批次上下文；不保存正文、请求、响应、URL 或凭据。
- 定向客户端测试 `7 passed`，Mapping/工作流测试 `17 passed`；变更文件 Ruff、compileall 和 `git diff --check` 通过。未运行全量回归。

## PDF Mapping Canary

- 来源任务：`tsk_01M16W32545DN9NC65XXEPJG1D`
- 读取：五份 `document-extraction-v1` 快照，`5/5` 命中；不写 checkpoint，不修改任务。
- 目标合格事实：`39`
- PDF 参考合格事实：`24`
- HTTP 请求：`1`，HTTP `200`
- 模型：配置/实际均为 `GLM-5.3-Flash`
- 响应格式：`json_schema`
- `finish_reason=stop`，输出字符数 `3171`，推理字符数 `0`，completion tokens `1034`，实际上限 `12288`
- Mapping 结果：`12` 条映射，`0` 条缺失要求，结构重试 `0`

## 唯一恢复任务

- 来源失败任务：`tsk_01M16W32545DN9NC65XXEPJG1D`
- 唯一 retry 任务：`tsk_01M16XN8BFR11RPP7Y4RZR36KE`
- 通过公开 `/api/v1/tasks/{id}/retry` 创建；未创建第二个 retry。
- 事实快照：五份文档级快照均复用，事实抽取模型调用 `0`。
- OCR：`0` 次。
- 结果阶段：Mapping `3` 个成功请求；Advice `4` 个成功请求；每个逻辑请求 `request_attempts=1`，结构重试 `0`。
- 任务最终状态：`SUCCEEDED / COMPLETED / 100%`
- 结果：`32` 项差异、`32` 项风险、`13` 项通过、事实矩阵 `41` 项。
- Advice：`32/32` 非空，模型建议 `32`，fallback `0`。
- 页码收口：从 `draft-result-pre-page-v1` 快照完成；四份 DOCX sidecar + 一份单页 PDF 解析缓存，公开证据 `98/98` 覆盖，缺失 `0`；OCR/LLM 均为 `0`。

## 控制台路径

- 任务列表：`/console/#/tasks`
- 报告：`/console/#/tasks/tsk_01M16XN8BFR11RPP7Y4RZR36KE/report`

## 未完成项

- 首次宿主机 retry 运行器在结束后收集 finish reason 时发生了自身字段兼容错误，因此 retry 请求级 finish reason 未被该运行器摘要持久化；任务状态、模型运行次数、结果与页码收口均已持久化并通过校验。后续应修正运行器统计器后再用于审计，不重跑本次任务。
- 页面视觉与文案人工抽查由控制台使用者完成。
