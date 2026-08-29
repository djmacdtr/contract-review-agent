# 任务进度：GLM 上下文容量探针

## 基本信息

- 时间：2026-08-28 16:00:43 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交开发、进度、备份及临时诊断文件

## 用户目标

验证甲方网关中 `GLM-5.3-Flash` 的上下文长度，并判断是否可以通过扩大单批输入减少 DRAFT_REVIEW 的 LLM 调用次数。

## 本次完成

- 从甲方 `/v1/models` 实时读取模型元数据，确认 `GLM-5.3-Flash` 声明 `max_model_len=262144`。
- 执行一次不含合同内容、单并发、短输出的合成大上下文探针。
- 核对项目当前有效限制：单批 payload 约 12,000 字符，模型输出上限 4,096 token，数值批次最多 6 个结构单元。

## 修改文件

- `docs/progress/20260828-160043_glm-context-capacity-probe.md`：记录本次只读诊断结果。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `GET /v1/models` 安全元数据读取 | 通过 | `GLM-5.3-Flash`，`max_model_len=262144`，`owned_by=vllm` |
| 合成大上下文 `POST /v1/chat/completions` | HTTP 200 | 输入 72,000 字符；网关计数 48,029 prompt tokens；耗时 4.481 秒；实际模型为 `GLM-5.3-Flash` |

探针将输出限制为 8 token，因此 `finish_reason=length` 只表示达到人为设置的短输出限制，不表示输入上下文溢出。

## Docker 与运行状态

- 本次未变更 Docker、API、Worker、PostgreSQL 或控制台运行状态。

## 重要决策

- 该模型至少已验证可接受约 48K 输入 token，当前 12,000 字符输入上限明显偏保守。
- 不能仅凭 256K 上下文把整份合同或全部辅助文件一次发送：当前主要瓶颈是结构化响应的 4,096 token 输出预算、逐候选完整返回及上游 HTTP 500 稳定性，而不是输入窗口。
- 后续应通过最坏批次 Canary 逐步扩大批次，并同步控制输出体积；不直接跳到模型最大上下文。

## 已知问题与风险

- 尚未验证甲方网关允许的最大输出 token、最大请求体及 64K 以上真实结构化请求的稳定性。
- 最近正式恢复任务 102 次 HTTP 中有 22 次返回 500；扩大单批可以减少暴露次数，但单批失败的重算成本也会增加。

## 下一步建议

1. 保持业务逻辑不变，先用最坏 numeric/text 历史批次做 2 次受控 Canary。
2. 第一档建议将 payload 提升至 24,000 字符、numeric 单元 6→12、text 单元 16→24，并将输出预算提升至 8,192 token；通过后再决定是否继续扩大。
3. 同时为 HTTP 500/502/503/429 增加有限指数退避，正式验收先使用并发 1，复用最新任务的 78 条 checkpoint。
4. 不采用整份合同单请求，也不改变动态检查项、证据回查和精确数值校验边界。

## 下一会话首先阅读

- `docs/progress/20260828-155354_numeric-index-schema-recovery-report.md`
- `docs/progress/20260828-160043_glm-context-capacity-probe.md`
- `app/core/config.py`
- `app/draft_review/extraction.py`
- `app/adapters/llm/openai_client.py`

## 交接摘要

甲方网关实时声明 GLM 上下文为 262,144 token；合成探针已验证 48,029 输入 token 可在 4.481 秒内成功处理。当前项目调用膨胀并非上下文不足，而是 12,000 字符分片过小、4,096 token 输出上限、完整结构化返回和失败拆分共同造成。建议先把批次扩大约 2 倍并将输出预算提升至 8,192，配合上游 5xx 有限重试和串行验收；不要一次发送整份合同。
