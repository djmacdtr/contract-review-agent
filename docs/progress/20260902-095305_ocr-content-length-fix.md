# OCR 流式请求 Content-Length 最小修复

- 时间：2026-09-02 09:53:05 +08:00
- 状态：COMPLETED
- 当前提交：`55d356d`

## 修改

- 生产 OCR 客户端继续以 1MB 分块读取并通过单个 HTTP POST 发送完整文件。
- 请求新增可信 `Content-Length=file.file_size`，避免 HTTPX 使用甲方网关不兼容的 chunked 请求体。
- 起草阶段多文件 OCR/解析并发由 2 收缩为 1，避免甲方网关在并发上传时快速断连。
- 未修改控制台上传、URL 任务接口、下载、缓存、OCR 参数、页码映射、LLM 或结果 Schema。

## 验证

- OCR 客户端与文档路由定向测试：34 passed。
- Ruff、compileall、`git diff --check`：通过。
- 唯一真实 DOCX Canary：61,781 bytes，HTTP/业务成功，8/8 页有效；未写 checkpoint、未创建任务。
- 后续真实多文件任务仍以并发 2 快速失败，结合单调用成功确认并发为剩余差异；串行修改后未追加外部调用。

## 运行状态

- Docker Worker 保持停止。
- 宿主机 Worker 已加载 Content-Length 与串行 OCR 修复并运行，PID `53876`。
- API、PostgreSQL、上传卷及历史任务未修改；旧失败任务未 retry。

## 标准 Worker 配置漂移诊断

- 新控制台任务已通过上传、下载、OCR 和页码阶段，在 `FACT_EXTRACTION / profile` 失败于 `LLM_NETWORK_ERROR`。
- 模型列表探针 317ms 成功，配置的抽取、评审和建议模型均存在，网关并未整体不可用。
- 既有三文件/五文件成功验收脚本显式使用 `LLM_MAX_CONCURRENCY=2`、`LLM_EXTRACTION_TASK_CONCURRENCY=2`、`LLM_HTTP_RETRY_ATTEMPTS=1`、`OCR_HTTP_RETRY_ATTEMPTS=0`，并为 Text/Advice 注入 `json_object` 覆盖。
- 当前标准 `.env`/Worker 使用 LLM 并发 3、HTTP 重试 4，且 `DraftReviewWorkflowExecutor` 默认构造的客户端没有 Text/Advice 覆盖。
- 因此此前成功证据覆盖的是“特殊宿主机验收脚本 + 缓存/受控配置”，不是“标准公开 API + 默认 Worker + 冷启动”的完整部署路径；控制台上传只触发并暴露了这一既有差异。
