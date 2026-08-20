# 任务进度：FINAL_COMPARE 0.4.1 下一里程碑计划

## 基本信息

- 时间：2026-08-20 16:20:50 +08:00
- 状态：COMPLETED
- 任务类型：DOCS / REVIEW
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-alignment`
- 当前提交：`f5a475e`
- 工作树状态：dirty；开始时已有未提交的 16 项差异复核记录，本次新增计划和本进度记录

## 用户目标

基于 46 页任务从 2,099 项降至 16 项、最新规则预计剩余 3 LOW 的现状，给出下一步可执行计划。

## 本次完成

- 读取相关方案、最新进度、当前 Git 状态和 16 项复核结论。
- 新增 FINAL_COMPARE 0.4.1 精度收口计划，明确范围、实施步骤、真实调用次数和验收门槛。
- 将表格续行定点修复、OCR 复核分区、正负黄金集、Docker 验证、一次真实复验和 PR 收口排定顺序。
- 明确暂缓 DRAFT_REVIEW、LLM、200 页和异步 OCR。

## 修改文件

- `docs/plans/20260820_final-compare-0.4.1-precision-closure.md`：新增下一里程碑执行计划。
- `docs/progress/20260820-162050_next-milestone-plan.md`：新增本次交接记录。
- 本次未修改业务代码、接口、数据库、配置或 Docker 服务。

## 接口、数据和配置变化

- API：无变化；计划建议未来仅增加向后兼容的可选复核原因字段。
- 数据库/迁移：无变化，计划中也不引入迁移。
- 配置：无变化。
- 兼容性：计划保持 API 版本与 `schema_version=1.0`，workflow/rules 目标为 `0.4.1`。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| 相关方案和进度读取 | 完成 | 以实际 16 项分类和当前代码状态为基线 |
| `git diff --check` | 待文档生成后执行 | 本次只检查文档格式 |
| 业务测试 | 未执行 | 本次为计划任务，没有修改业务代码 |

## Docker 与运行状态

- 本次未启动、停止、重建或重启 Docker 服务。
- 服务保持已有运行状态。

## 重要决策

- 下一步先收口 FINAL_COMPARE 0.4.1，不并行扩展 DRAFT_REVIEW 或 LLM。
- 合理目标是 2–3 个 LOW，而不是通过宽松规则追求 0 差异。
- 只有全部本地测试通过后才重跑一次 46 页真实 external 任务。
- 0.4.1 通过后，下一主黄金集必须切换到真实 DOCX→盖章扫描 PDF。

## 已知问题与风险

- 当前历史任务仍显示 16 项，最新规则只有离线回放证据。
- 表格续行合并如果条件过宽可能吞掉真实设备名称、数量或金额变化，因此必须先写反例测试。
- 当前工作树已有未提交的复核记录，后续会话必须保留并纳入合适提交。

## 下一步建议

1. 新会话完整阅读精度收口计划和最近两份进度记录。
2. 从失败测试开始，实现严格表格续行合并。
3. 补正负黄金集和 OCR 复核原因展示。
4. 完成本地/Docker 全量验证后只运行一次真实 46 页任务。
5. 验收通过后将 PR 标记 Ready，未经授权不合并。

## 下一会话首先阅读

- `docs/plans/20260820_final-compare-0.4.1-precision-closure.md`
- `docs/progress/20260820-155700_final-compare-alignment.md`
- `docs/progress/20260820-161727_sixteen-diff-review.md`
- `docs/progress/20260820-162050_next-milestone-plan.md`
- `app/comparison/reliable.py`
- `app/comparison/engine.py`
- `tests/unit/test_comparison.py`

## 交接摘要

下一里程碑是 FINAL_COMPARE 0.4.1 精度收口。
先定点消除表格续行误报，再将两项单字符 OCR 差异保留为 LOW 复核。
必须补金额、日期、主体、条款和表格正样本，防止降噪导致漏检。
全部本地/Docker 测试通过后，只重跑一次 46 页真实任务。
验收目标为 0 HIGH、0 MEDIUM、0 数值变化、最多 3 LOW，目标 2。
通过后 PR Ready，再切换到真实 DOCX→盖章扫描 PDF 主黄金集。
