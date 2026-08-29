# 任务进度：Text 饱和恢复拆分诊断

## 基本信息

- 时间：2026-08-28 16:31:33 +08:00
- 状态：COMPLETED
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交开发、进度、备份及临时诊断文件

## 用户目标

分析 text 16 单元、8192 token Canary 的 `FACT_BATCH_SATURATED` 结果，并确定下一步最短交付路径。

## 本次完成

- 确认 Canary 的 HTTP 请求成功，失败属于事实数量达到 12 项安全上限后的主动饱和保护，不是上下文、JSON Schema 或网关失败。
- 只读核查 text 恢复逻辑，确认多单元批次发生可恢复错误时，当前实现会直接拆为逐单元请求。
- 确认该行为会使 16 单元饱和批次产生最多 16 个子请求，是调用量膨胀的直接原因之一。

## 修改文件

- `docs/progress/20260828-163133_text-recovery-split-diagnosis.md`：记录只读诊断结论。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 配置：无。
- 兼容性：无业务代码变化。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| `text16-fact-canary-20260828.json` | HTTP 200 | 16 单元，`FACT_BATCH_SATURATED` |
| `recovery_groups()` 只读核查 | 确认问题 | `len(blocks)>1` 时直接为每个 block 生成一个子组 |

## Docker 与运行状态

- 本次未改变 API、Worker、PostgreSQL 或控制台运行状态。

## 重要决策

- 保留 `FACT_BATCH_SATURATED` 安全门，不能将达到输出上限的结果当作完整事实发布。
- text 多单元恢复应改为确定性平衡二分：`16→8+8→4+4`，而不是 `16→16 个单元请求`。
- numeric 保持现有候选感知的 `12→6→3→1` 恢复，不受 text 修改影响。

## 已知问题与风险

- 平衡二分尚未实现和测试。
- 正式恢复任务尚未创建；39 项差异、4 项通过及建议质量仍待验收。

## 下一步建议

1. 仅修改 text 多单元恢复分组为稳定的左右二分，保持顺序、完整覆盖且不重复。
2. 增加定向测试：16→8+8、8→4+4、奇数单元完整覆盖、numeric 和单表格恢复不变。
3. 复用已失败父批次，只调用两个 8 单元子批次作为恢复 Canary；两者成功后创建唯一正式恢复任务。
4. 正式配置保持 `json_schema`、8192 输出、numeric 12、text 16、payload 24000、并发 1。

## 下一会话首先阅读

- `docs/progress/20260828-162957_text16-canary-formal-recovery-report.md`
- `docs/progress/20260828-163133_text-recovery-split-diagnosis.md`
- `app/draft_review/extraction.py`

## 交接摘要

Text 16 Canary 的饱和保护是正确的，但当前恢复逻辑会把 16 单元一次性拆成 16 个请求，导致调用量暴涨。下一步不应删除安全门或继续扩大 token，而应改为平衡二分恢复；预计该类失败从最多 17 次调用降为 3 次，并保持完整事实覆盖。
