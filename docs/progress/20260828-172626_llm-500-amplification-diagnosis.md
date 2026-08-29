# 任务进度：LLM 500 请求放大诊断

## 基本信息

- 时间：2026-08-28 17:26:26 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改、历史进度和 `.real-diagnostic-temp/`

## 用户目标

解释唯一正式恢复任务在并发 1 下仍产生大量 HTTP 500 的原因，判断是参数、调用频率还是甲方服务问题。

## 本次完成

- 核对正式任务安全统计：436.375 秒、98 次 HTTP、43 次 200、55 次 500，首个终止点为 numeric 单结构 `LLM_UPSTREAM_ERROR`。
- 确认宿主机恢复并发为 1，不存在应用侧并行请求洪峰。
- 确认 HTTP 层对 429/500/502/503/504 最多尝试 4 次。
- 确认抽取层又把 `LLM_UPSTREAM_ERROR` 列为内容可恢复错误；多单元上游失败会继续拆分批次，子批次再次各自执行 HTTP 重试。
- 确认 numeric 非截断上游错误会走通用多单元拆分，导致一次服务错误被放大为父批次和多个子批次请求。
- 确认当前全局 `LLM_MAX_OUTPUT_TOKENS=8192` 会应用到 numeric 单候选等所有 LLM 操作，尽管此类响应通常只需数百 token。

## 修改文件

- `docs/progress/20260828-172626_llm-500-amplification-diagnosis.md`：记录只读诊断结论。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| 正式任务请求统计 | 确认 | 98 次 HTTP，500 占 55 次，约 56.1% |
| 并发配置 | 确认 | LLM 和抽取任务并发均为 1 |
| HTTP 重试 | 确认 | 5xx/429 最多 4 次总尝试 |
| 抽取恢复分类 | 确认问题 | `LLM_UPSTREAM_ERROR` 重试耗尽后仍触发内容拆分 |
| 输出预算 | 确认问题 | 全操作共用 8192 tokens，numeric 单项未使用紧凑预算 |

## Docker 与运行状态

- 本次未改变 API、Worker、PostgreSQL 或控制台状态。

## 重要决策

- `LLM_UPSTREAM_ERROR`、`LLM_RATE_LIMITED` 和网络型超时不得触发内容拆分；它们只允许 HTTP 层有限重试，耗尽后立即保存 checkpoint 并停止任务。
- 只有内容相关错误才允许拆分：输出截断、批次饱和、非法 JSON、Schema/证据错误等。
- LLM 输出预算应按操作设置，不能让 numeric 单候选请求统一预留 8192 token。
- 并发 1 可以保留；当前不需要继续降低并发。

## 已知问题与风险

- 甲方网关为何返回 500 仍需网关日志才能最终确认；但本项目的双层恢复明确放大了 500 次数。
- operation-specific token budget 和 upstream circuit breaker 尚未实现。

## 下一步建议

1. 从抽取内容拆分集合中移除 `LLM_UPSTREAM_ERROR`、限流和网络型超时；HTTP 重试耗尽后立即终止本次任务。
2. 增加简单熔断：连续上游失败达到阈值后停止调度剩余批次，不再拆分。
3. 增加按操作输出预算：numeric 按候选数动态使用约 512–4096 token，text 保持 8192，profile/mapping/advice 使用独立上限。
4. 运行定向测试，确认上游错误最多产生 4 次 HTTP 且不生成子批次；内容截断仍可二分。
5. 用首个失败 numeric 单结构执行 1 次无重试 Canary；通过后从最新失败任务创建唯一正式恢复任务。

## 下一会话首先阅读

- `docs/progress/20260828-172322_text-recovery-formal-retry-report.md`
- `docs/progress/20260828-172626_llm-500-amplification-diagnosis.md`
- `app/adapters/llm/openai_client.py`
- `app/draft_review/extraction.py`
- `app/core/config.py`

## 交接摘要

并发已是 1，问题不是并行洪峰。一次 HTTP 500 先被重试 4 次，随后又被抽取层误当作内容问题拆分，所有子批次再次重试，造成 98 次请求和 55 次 500。全局 8192 输出预算还应用到 numeric 单项请求，增加资源压力。下一步应先消除双层放大并设置按操作 token 预算，再做一次精确 numeric Canary。
