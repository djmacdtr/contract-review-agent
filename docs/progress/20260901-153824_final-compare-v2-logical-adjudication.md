# FINAL_COMPARE V2 受控逻辑候选裁决记录

- 时间：2026-09-01 15:38（Asia/Shanghai）
- 范围：仅更新 `FINAL_LOGICAL_V2` 的候选分组/裁决、风险等级和通过项互斥；`DRAFT_REVIEW`、LEGACY、OCR、页码、Advice 生成和公开 API 保持兼容。
- 外部状态：本轮未调用 OCR/LLM，未创建正式任务。

## 已实现

- 新增 `group_id + candidate_ids` 四态内部裁决协议：`SAME_LOGICAL_CHANGE`、`EQUIVALENT_NO_CHANGE`、`DISTINCT_CHANGES`、`UNCERTAIN`。
- 候选分组限制在同文件、同表格/局部条款和相邻证据区域；保留原始证据位置，真实值变化不得进入等价删除。
- 逻辑裁决调用限制为每批最多 4 组、缺项最多 2 组恢复、总逻辑调用不超过 8 次；错误或不确定时保留候选并标记待复核。
- 合并使用程序类型优先级，重建差异编号并汇总物理位置；`EQUIVALENT_NO_CHANGE` 只有在规范化文本、数值和安全语义门禁均通过时才删除。
- `RiskItem` 增加可选 `risk_level`，由程序依据证据分类为 `HIGH/MEDIUM/LOW`；控制台增加等级标签和筛选。
- 通过项同时考虑已确认差异和待复核差异，并补充中文数字日期识别，避免不确定变化被误报为通过。

## 离线验证

- 相关后端测试：`93 passed, 1 warning`。
- 前端格式测试、typecheck、build：通过。
- 变更范围 Ruff：通过。
- 相关 Python compileall：通过。
- `git diff --check`：通过（仅显示现有 LF/CRLF 提示）。
- 三组真实 cache-only dry-run：
  - 第 1 组：`93` 个候选、`1` 个待复核、页码 `247/247`；
  - 第 2 组：`21` 个候选、`0` 个待复核、页码 `64/64`；
  - 第 3 组：`22` 个候选、`0` 个待复核、页码 `66/66`。
  - 三组均 `SKIPPED_NO_CANDIDATES`，OCR/LLM/数据库写入均为 `0`。
- 已有三组正式报告保持不变；本轮没有重跑或覆盖历史任务。

## 未完成项

- 当前真实缓存没有疑似重复簇，因此没有执行外部 Canary；不制造人工重复候选。
- 57 项人工金标、四类真实疑难组和 V2 正式 LLM 裁决尚未执行。
- 控制台视觉效果和建议语气由用户人工抽查。
- 保留工作区其他既有未提交修改及 `.real-diagnostic-temp/`；未执行 reset、clean、commit 或 push。
