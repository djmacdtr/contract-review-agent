# 任务进度：DRAFT_REVIEW 多文档与 LLM 方向确认

## 基本信息

- 时间：2026-08-20 17:34:46 +08:00
- 状态：COMPLETED
- 任务类型：REVIEW / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-alignment`
- 当前提交：`137423f`
- 工作树状态：开始时 clean 且本地领先远端 1 个协作规范提交；本次新增计划和本记录

## 用户目标

提交已完成修改，分析 FINAL_COMPARE 0.4.1 之后的方向，确认是否进入起草检查多文档智能比对和 LLM 接入，并取消调用方选择固定辅助资料类型。

## 本次完成

- 确认并行会话已提交并推送 FINAL_COMPARE 0.4.1 与 OCR 安全诊断，真实 46 页任务通过：3 LOW、0 HIGH/MEDIUM/numeric。
- 提交快速开发与分层测试协作规范，提交为 `137423f`。
- 核对当前 DRAFT_REVIEW 请求、控制台、Mock Graph、LLM Protocol 和集团网关设计。
- 确认下一主线为 DRAFT_REVIEW，但采用“模板确定性比对 + 单文档事实抽取 + 跨文件事实矩阵”，不做全文件两两 diff。
- 确认 `reference_type` 不再由调用方选择，系统根据内容自动生成开放式 `document_kind`。
- 新增完整的多文档和 LLM 分阶段实施计划。

## 修改文件

- `docs/plans/20260820_draft-review-multidoc-llm.md`：新增下一主里程碑计划。
- `docs/progress/20260820-173446_draft-review-direction.md`：新增本次交接记录。
- 本次未修改业务代码、API 实现、数据库、配置或 Docker 服务。

## 接口、数据和配置变化

- API：本次未实现；计划确定下一版请求不再要求 `reference_type`。
- 数据库/迁移：无变化；既有 nullable 字段可暂时作为历史兼容保留。
- 配置：本次无变化；计划建议将辅助文件上限改为可配置。
- 兼容性：可以短期接受旧 `reference_type` 但忽略，正式接口冻结前移除。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 当前 Git/进度核对 | 通过 | 业务分支最新为 `7d75ccc`，并行会话已 clean/push |
| FINAL_COMPARE 验收证据 | 通过（读取既有记录） | 103 passed；46 页真实任务 3 LOW、0 HIGH/MEDIUM/numeric |
| DRAFT_REVIEW 现状核对 | 通过 | 当前仍为 Mock，UI/API 仍存在 `reference_type` |
| 文档 diff 检查 | 通过 | `git diff --check` 无错误，本次仅新增 Markdown |
| 业务测试 | 未执行 | 本次未修改业务代码，按快速开发策略无需运行 |

## Docker 与运行状态

- 本次未重建、重启或停止 Docker 服务。
- 沿用上一进度：默认 Compose API healthy、Worker running、PostgreSQL healthy。

## 重要决策

- 任意辅助资料不能依赖调用方枚举类型，模型分类结果也使用开放字符串并允许 UNKNOWN。
- 多文档检查不做 N 份文件的 O(N²) 全文 pairwise diff，使用事实候选和事实矩阵聚合。
- LLM 用于文档分类、事实抽取、语义映射和建议；确定性比较和数值规则继续由代码负责。
- 首个开发切片先做接口清理、真实下载/解析和模板比对，再接入真实 LLM。

## 已知问题与风险

- 真实 LLM API Key、零信任权限和 JSON Schema 能力尚未确认。
- 辅助文件数量不能无限开放，需要配置上限和任务资源控制。
- 当前 DRAFT_REVIEW 的 Mock 结果和接口示例仍使用固定辅助类型，后续实现必须同步修改测试和控制台。
- PR #2 仍有控制台人工视觉清单待确认，与下一里程碑开发可以分开处理。

## 下一步建议

1. 人工完成 PR #2 控制台清单并将 PR 标记 Ready，不自动合并。
2. 新分支执行 DRAFT_REVIEW 阶段 A：移除类型下拉、可配置数量、真实下载/解析和文件自动画像结果结构。
3. 接着实现模板确定性检查，再实现 LLM Adapter 和单文档事实抽取。
4. 获取甲方 LLM Key 后只做最小脱敏联调，再进入多文档事实矩阵。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260820_draft-review-multidoc-llm.md`
- `docs/progress/20260820-172227_ocr-acceptance-unblock.md`
- `docs/progress/20260820-173446_draft-review-direction.md`
- `app/schemas/requests.py`
- `app/adapters/llm/base.py`
- `app/workflows/mock_graphs.py`

## 交接摘要

FINAL_COMPARE 0.4.1 后端真实验收已完成，下一主线转向 DRAFT_REVIEW。
起草检查采用模板确定性比对、逐文档事实抽取和跨文件事实矩阵，不做所有文件两两 diff。
辅助资料不再由调用方选择固定类型，系统根据正文自动识别开放式 document_kind。
任意资料理解需要 LLM，但金额、日期、占位符和明确文字差异继续由程序判断。
下一切片先完成接口清理和多文件真实解析，再接入 LLM Adapter。
