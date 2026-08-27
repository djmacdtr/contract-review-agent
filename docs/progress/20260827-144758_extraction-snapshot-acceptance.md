# 文档级抽取快照与单结构截断恢复验收记录

## 结论

本轮完成了文档级抽取 checkpoint、文本分片截断恢复、动态文本 Schema 及离线回归；唯一正式三文件任务已按计划创建并仅通过 GET 轮询，但在事实抽取阶段失败，未生成正式报告。本轮不 retry、不创建第二个任务，不提交代码。

- 日期：2026-08-27
- 分支：`feat/draft-review-multidoc`
- 唯一正式任务：`tsk_01M10YQ3Z99FB3AP5PAKN5PNE5`
- 控制台入口：`/console/#/tasks`
- 任务状态：`FAILED / FACT_EXTRACTION / 75%`
- `.real-diagnostic-temp/`：未修改、未清理

## 本轮实现

- 增加 `document-extraction-v1` 文档级 checkpoint；身份使用文件 SHA-256、解析器版本、抽取版本、结构单元顺序/文本和候选范围，排除任务文件 ID、分片 ID、物理页码及展示字段。
- 文档 Reduce 完成并通过证据及事实身份校验后保存完整快照；加载时校验来源文件、证据和身份，再映射为当前任务文件 ID。
- 快照命中时跳过 profile、numeric、text 抽取调用；保留既有分片 checkpoint，不扩展历史恢复树兼容。
- 文本 payload 支持动态 `max_items`；Schema 与提示同步限制，恢复顺序为表格单元格、句子/条款边界、`12 → 6 → 3` 数量上限，3 项仍截断或饱和时安全失败。

## 离线验证

- 定向结构化抽取与 LLM 客户端测试：`68 passed`。
- Workflow、结果格式相关测试：`54 passed`。
- Compose 全量测试：`384 passed`。
- 变更文件 Ruff：通过。
- `python -m compileall -q app`：通过。
- `git diff --check`：通过。

## 唯一真实任务

### 执行环境

- Docker Worker 已暂停；宿主机 Miniconda Worker 连接 Docker PostgreSQL `127.0.0.1:15432`。
- 脱敏三文件通过宿主机只读 HTTP 服务提供。
- 临时生效配置为 `ALLOW_HTTP_DOWNLOADS=true`、下载白名单 `127.0.0.1`、`DOCX_PAGE_LOCATION_ENABLED=true`、`OCR_HTTP_RETRY_ATTEMPTS=0`、`LLM_RESPONSE_FORMAT=json_schema`、`LLM_NATIVE_STRUCTURED_OUTPUT=true`、`TASK_MAX_ATTEMPTS=1`。
- 任务结束后宿主机 Worker、文件服务已停止；Docker API/Worker 已恢复运行，API 健康检查通过。
- 恢复后的 Docker 配置使用 `.env` 正式文件域名白名单，不保留 `127.0.0.1`、`fixture-server` 或通配符；未修改 API Key。

### 结果与首个安全错误

- 三份文件下载完成，任务进入 `FACT_EXTRACTION / 75%`。
- 首个安全错误：`DYNAMIC_CHECK_INCOMPLETE`。
- `failure_stage=FACT_EXTRACTION`。
- `chain=numeric`。
- `file_id=fil_01M10YQ3Z99FB3AP5PAKN5PNE6`。
- `batch_depth=1`。
- `unit_count=10`。
- `failure_code=LLM_OUTPUT_TRUNCATED`。
- Worker 结构化日志同步记录了上述安全字段；未记录合同正文、完整响应、URL 或密钥。

本次错误发生在 numeric 链，而不是已覆盖的映射阶段；由于正式任务未完成全部文档 Reduce，无法据此证明三份文档快照全部保存，也未验证快照命中后的抽取调用数为 0。

## 未完成项

- 未获得 `SUCCEEDED / COMPLETED / 100`。
- 未生成正式结果，因而未验证 39 项差异、4 项通过、页码、局部高亮和 `analysis_advice` 覆盖率。
- 未完成 numeric 链 `LLM_OUTPUT_TRUNCATED` 的后续定向恢复；按唯一任务失败规则，本轮不修复后重跑。
- 未进行控制台报告页视觉验收；失败任务没有可展示报告，保留 `/console/#/tasks` 供后续验收。

## 保护项

本轮未执行 commit、push、reset、clean 或 retry；保留工作区既有未提交修改。
