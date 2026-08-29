# 任务进度：Text Canary 与生产规划范围诊断

## 基本信息

- 时间：2026-08-28 17:01:31 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改、历史进度和 `.real-diagnostic-temp/`

## 用户目标

分析引入 `has_more` 后两个 8 单元 Text Canary 仍分别饱和和截断的原因，判断是否是业务校验规则整体过严。

## 本次完成

- 核对正式 DRAFT_REVIEW text 规划路径和 `scripts/expanded_fact_canary.py` 的 Canary 规划路径。
- 确认正式流程对 TARGET 使用模板差异候选 `plan_text_candidate_batches()`；Canary 脚本却对 TARGET 使用全文 `plan_text_document_batches()`。
- 本地重建正式目标候选：模板原始差异 52 项、保留差异 39 项、文字候选 49 项、正式 TARGET text 批次 4 个。
- 核对 Canary 选中的 16 个 TARGET 表格单元与正式 49 个目标候选的 block 身份，重合数为 0。
- 确认最近 16→8+8 的真实 Canary 测试了一组正式流程不会发送的全文表格批次，因此不能作为正式任务门禁。
- 重建当前三文件正式 text 规划，共 6 批：TARGET 4 批、REFERENCE 2 批。

## 修改文件

- `docs/progress/20260828-170131_text-canary-production-scope-diagnosis.md`：记录只读诊断结论。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 正式模板分析与候选重建 | 完成 | raw diff 52、retained diff 39、TARGET text candidate 49 |
| 正式 text 批次重建 | 完成 | TARGET 4 批、REFERENCE 2 批，共 6 批 |
| Canary 父批次与正式 TARGET 候选交集 | 0 | Canary 的 16 个表格 block 均不在正式候选范围 |
| 输入规模安全统计 | 完成 | 正式批次 payload 约 2,076–7,229 字符，单批 1–16 单元 |

## Docker 与运行状态

- 本次未改变 API、Worker、PostgreSQL 或控制台状态。

## 重要决策

- 当前最近几轮 Text Canary 失败不能用于证明生产 text 链不可用，因为 Canary 规划范围与正式流程不一致。
- 不继续放宽 unit、quote、证据、身份或 Schema 校验。
- Canary 必须复用正式工作流的模板差异候选和参考资料规划逻辑，不能另造全文 TARGET “最坏批次”。
- `has_more`、平衡二分、numeric 扩容和 HTTP 有限重试可以保留，但必须在真实生产批次上验证。

## 已知问题与风险

- 修正后的正式范围 Canary 尚未执行。
- REFERENCE 仍按实际资料全文动态规划，这是生产设计；其 2 个批次需要纳入真实门禁。
- 正式恢复任务尚未创建，控制台报告仍待验收。

## 下一步建议

1. 修正 `scripts/expanded_fact_canary.py`，使 TARGET 调用 `analyze_template()`、`build_template_text_candidates()` 和 `plan_text_candidate_batches()`；REFERENCE 保持 `plan_text_document_batches()`。
2. 只选择正式 6 个 text 批次中 payload 最大的 TARGET 批次和 REFERENCE 批次各调用一次。
3. 两次通过后直接创建唯一正式恢复任务；若某批出现 `has_more=true`，允许生产平衡二分恢复，不再把可恢复首批饱和本身当作全流程失败。
4. 若正式范围批次仍出现 8192 token 截断，再记录 usage/content/reasoning 字符计数，针对最短 quote 和事实粒度修复，不再盲目拆分。

## 下一会话首先阅读

- `docs/progress/20260828-165149_text-has-more-canary-report.md`
- `docs/progress/20260828-170131_text-canary-production-scope-diagnosis.md`
- `scripts/expanded_fact_canary.py`
- `app/workflows/draft_review.py`
- `app/draft_review/extraction.py`
- `app/draft_review/facts.py`

## 交接摘要

最近的 Text Canary 门禁选错了数据范围：它对 TARGET 做全文规划，而生产只处理模板差异候选。失败父批次的 16 个表格单元与正式 49 个 TARGET 候选重合为 0。当前不应继续放宽业务校验，而应先让 Canary 完全复用生产规划；正式三文件 text 实际只有 6 个首批计划。
