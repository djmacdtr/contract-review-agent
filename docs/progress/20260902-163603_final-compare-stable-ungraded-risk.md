# FINAL_COMPARE 表格稳定版与无等级风险收敛记录

## 状态

PARTIAL：本轮完成离线实现和首组 cache-only 预检；按计划未调用外部 OCR/LLM、未创建正式任务。

## 实现摘要

- FINAL_COMPARE 使用三个独立内部开关：
  - `FINAL_COMPARE_LOGICAL_V2_ENABLED=true`
  - `FINAL_COMPARE_EQUIVALENT_FILTER_ENABLED=true`
  - `FINAL_COMPARE_LLM_ADJUDICATION_ENABLED=false`
- V2 运行链仅执行确定性等价/边界过滤；逻辑变化簇不自动合并，LLM 差异裁决调用数为 0。
- 公开 `RiskItem`、前端风险卡片和报告 Tab 已移除风险等级展示；历史结果中的未知字段仍可读取并被忽略。
- 未改 DRAFT_REVIEW、OCR、页码、印章、Advice、下载和 Worker 配置逻辑。

## Cache-only dry-run

来源任务：`tsk_01M1BBHY5424N69QRDFA8N96VZ`（只读诊断输入）。

安全统计：

- 原始候选：93
- 疑似簇：22 个，覆盖 52 个候选
- 确定性等价过滤：11 个候选，5 个簇
- 边界噪声过滤：1 个候选
- 逻辑变化自动合并：0
- 最终规则候选：81
- 待人工复核：0
- 公开页码证据：222/222，缺失 0
- OCR：0；LLM：0；数据库写入：0

过滤仅使用程序既有的字段/值、公式和证据安全门；未使用历史金标、Canary 或模型裁决。

## 离线检查

- 定向 pytest：64 passed（1 个第三方弃用警告）
- 变更范围 Ruff：通过
- 相关 Python compileall：通过
- 前端格式测试：通过
- 前端 typecheck：通过
- 前端 build：通过
- `git diff --check`：通过（仅有工作树换行策略提示）

## 保护与未完成项

- 已在 `backups/20260902-final-compare-stabilization.patch` 保存开工前工作树补丁。
- 未执行 reset、checkout、clean、commit、push；未清理 `.real-diagnostic-temp/`。
- 未进行真实 FINAL_COMPARE 验收；页码统计以本次缓存预检的实际覆盖集合为准，尚未创建新任务。
- 复杂金标、Canary 和 LLM 差异裁决代码仍作为未启用研发材料保留。
