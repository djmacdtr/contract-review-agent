# 任务进度：Text 饱和协议修复与 Canary

## 基本信息

- 时间：2026-08-28 16:51:49 +08:00
- 状态：BLOCKED
- 任务类型：FIX / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改和 `.real-diagnostic-temp/`

## 用户目标

修正 text 批次将“恰好达到 12 项”误判为饱和的问题，引入必填 `has_more` 内部协议，保持严格证据和身份校验，并重新验证两个既有 8 单元子批次。

## 本次完成

- `TextFactExtraction` 增加必填 `has_more: bool`。
- text 饱和判断改为仅在 `has_more=true` 时触发；`has_more=false` 时即使返回 12 项也接受。
- Prompt 明确要求在本批完整时返回 `has_more=false`，仍有后续事实时返回 `true`。
- Text wire Schema 移除 Prompt 未要求的可选字段，保留 unit、语义、类型、quote 和 confidence 等必要字段。
- 同步更新本地 mock、结构化 probe 和定向测试响应。

## 修改文件

- `app/adapters/llm/schemas.py`：增加 text 响应必填字段并精简 item Schema。
- `app/draft_review/facts.py`：按 `has_more` 控制饱和判断。
- `app/adapters/llm/openai_client.py`：更新 text Prompt 和动态 Schema。
- `app/adapters/llm/base.py`：更新 mock text 响应。
- `scripts/llm_structured_output_probe.py`：更新合成 text 响应。
- `tests/unit/test_structured_extraction_v2.py`：覆盖 `has_more` 饱和语义及响应字段。
- `tests/unit/test_text_grounding_filter.py`：补齐 text 响应协议。
- `tests/unit/test_openai_llm_client.py`：验证必填字段和精简 wire Schema。
- `tests/unit/test_llm_structured_output_probe.py`：补齐合成响应。

## 接口、数据和配置变化

- API：无公开接口变化。
- 数据库/迁移：无变化。
- 配置：无变化；未修改 `.env` 或密钥。
- 兼容性：只改变内部 text LLM 响应协议；numeric、平衡拆分、表格恢复、checkpoint 身份和结果 Schema 未改变。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| text 证据/饱和/恢复定向用例 | 通过 | 6 passed |
| text grounding filter 定向用例 | 通过 | 4 passed |
| OpenAI text/schema 定向用例 | 通过 | 15 passed |
| structured output probe | 通过 | 2 passed |
| Ruff（变更文件） | 通过 | All checks passed |
| compileall（变更文件） | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅既有换行格式提示 |
| 两个 8 单元 text Canary | 未通过 | 各 1 次、HTTP 200；第一批 `FACT_BATCH_SATURATED`，第二批 `LLM_OUTPUT_TRUNCATED` |

此前较宽的 text/checkpoint 测试选择中有 10 个既有失败上下文夹具，本轮未扩大范围处理；本次协议相关的聚焦用例均已通过。

## Docker 与运行状态

- API：running / healthy。
- Worker：running。
- PostgreSQL：running / healthy。
- 控制台：未进行视觉验收。
- 最终是否保持运行：保持原状态，未停止或重启服务。

## 重要决策

- `FACT_BATCH_SATURATED` 不删除，只由模型明确返回 `has_more=true` 触发。
- 未将 `has_more_required` 写入业务 payload，避免改变既有 payload digest 和 checkpoint 命中身份。
- 两个子 Canary 未同时成功，因此不创建来源任务 `tsk_01M13PH5H5EAWJXJRCFKH00PH0` 的正式恢复任务。

## 已知问题与风险

- 第一子批次已证明 `has_more` 饱和信号能被程序正确识别，但模型判断仍提示有后续事实。
- 第二子批次仍发生输出截断；当前 text=16 配置尚未通过真实 Canary 门禁。
- 正式任务、39 项差异、4 项通过、页码和建议覆盖率均未验收。

## 下一步建议

1. 按既定止损规则停止本轮外部调用；后续再决定是否将 text 上限降至 12 或针对截断子批次做独立处理。
2. 在两个子批次成功前，不创建正式恢复任务，不重复调用失败父批次。

## 下一会话首先阅读

- `app/adapters/llm/schemas.py`
- `app/draft_review/facts.py`
- `app/adapters/llm/openai_client.py`
- `docs/progress/20260828-164054_text-saturation-contract-diagnosis.md`
- `docs/progress/20260828-165149_text-has-more-canary-report.md`

## 交接摘要

Text 饱和判定已从数量阈值改为必填 `has_more` 协议，严格证据校验保留。
相关定向测试、Ruff、compileall 和 diff 检查通过。
两个 8 单元 Canary 各调用一次且均 HTTP 200，但分别出现饱和和截断。
未创建正式任务，未 retry，未追加外部调用。
服务保持 API/Worker/PostgreSQL 原状态。
