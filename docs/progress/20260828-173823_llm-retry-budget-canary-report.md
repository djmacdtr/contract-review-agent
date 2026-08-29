# LLM 重试放大与 Numeric 单结构 Canary 报告

## 实施

- Numeric/profile/mapping/advice 输出上限改为按操作使用 1024–4096、2048、4096、2048；Text 保持 8192。
- `LLM_UPSTREAM_ERROR`、限流、超时和网络错误不再进入内容拆分恢复。
- 独立抽取在传输重试耗尽后打开快速熔断，不再调度后续 LLM 批次；严格证据与 Schema 校验保持不变。
- Numeric 诊断入口固定 `LLM_HTTP_RETRY_ATTEMPTS=0`，并支持按确定性 batch ID 精确重建历史单结构，不做模糊映射。

## 定向验证

- OpenAI Client 输出预算与 Numeric Schema 测试：3 passed。
- Numeric 上游错误不拆分、不调度后续批次：1 passed。
- Numeric 诊断测试：3 passed。
- 相关 Ruff、compileall、`git diff --check`：通过。

## 唯一 Numeric Canary

- 来源任务：`tsk_01M13TETNBVT4SRQGPN6S7T56T`
- 目标 batch：`batch_19682847a9bc4d57f755e490`
- 单结构请求：1 次；HTTP 重试：0
- 结果：`LLM_UPSTREAM_ERROR`
- 未再次调用该 Canary，未创建正式恢复任务。
- 安全结果：`.real-diagnostic-temp/numeric-single-no-retry-canary-20260828.json`

该单结构在无重试和缩小后的 Numeric 输出预算下仍返回上游错误，因此本轮不能将问题归因于双层重试放大或 8192 全局输出预算；按止损规则停止正式验收。

## 服务状态

- Docker Worker 首次恢复时发现旧镜像仍将 Numeric 单元上限校验为 6，导致容器重启；已重建当前工作区镜像。
- 当前 API、PostgreSQL、Worker 均运行，Worker 启动日志确认 `json_schema`、原生结构化输出和 `GLM-5.3-Flash` 已生效。

## 未完成项

- 未创建新的正式任务，未进行三文件报告、控制台、39 项差异、4 项通过或页码验收。
- 甲方 GLM 网关对该 Numeric 单结构仍需独立处理或服务侧确认；本轮不再修改 Prompt、Schema 或重复调用。
- 保留全部未提交修改和 `.real-diagnostic-temp/`；未执行 reset、clean、commit 或 push。
