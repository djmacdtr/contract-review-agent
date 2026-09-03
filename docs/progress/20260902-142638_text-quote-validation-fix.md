# Text Quote 证据校验分层修复

## 目标

修复 Text 事实响应中单条 quote 无法回查时导致整批恢复预算耗尽的问题，保留动态事实抽取、跨资料映射、Advice 和正式证据可靠性边界。

## 实现

- LLM Adapter 不再调用全量证据展开函数验证每条 quote。
- Adapter 继续校验 JSON、Pydantic Schema、Text 类型、批次 items 上限和 `has_more` 饱和协议。
- 抽取层继续使用 `filter_text_fact_evidence`，逐条执行 unit、quote、位置、身份和来源文件校验。
- 无法回查的候选只记录 `discarded_fact_codes` 并丢弃；可靠候选继续进入后续映射。
- 增加 payload 文件 ID 与当前解析文档 ID 的硬校验，防止跨文档误绑定。
- Numeric、Mapping、Advice、页码、checkpoint、公开 API 和 KISS 兼容代码未改动；KISS 仍不是默认生产抽取路径。

## 验证

- `tests/unit/test_openai_llm_client.py tests/unit/test_text_grounding_filter.py`：`60 passed`
- Text/恢复相关 `tests/unit/test_structured_extraction_v2.py`：`6 passed`
- `ruff check`：通过
- `compileall`：通过
- `git diff --check`：通过
- 宿主机唯一 Worker 已重启并加载修复，API `/health` 和 `/ready` 均 HTTP 200。

## 真实任务状态

- 未创建新业务任务，未 retry 失败任务。
- 最新任务为五文件输入，不符合本轮原计划的三文件验收范围，因此未擅自复制或重跑。
- 未修改历史报告、数据库数据或 Docker Worker 状态。
