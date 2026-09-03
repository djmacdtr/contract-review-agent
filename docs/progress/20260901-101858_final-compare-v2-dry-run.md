# 任务进度：FINAL_COMPARE V2 真实重复差异收敛

## 基本信息

- 时间：2026-09-01 10:18:58 +08:00
- 状态：PARTIAL
- 任务类型：FIX / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`55d356d`
- 工作树状态：dirty；保留既有 DRAFT_REVIEW、页码、OCR 缓存及 FINAL_COMPARE V2 未提交修改，本次未回退或清理。

## 用户目标

在不影响 DRAFT_REVIEW 和 FINAL_COMPARE LEGACY 的前提下，让 FINAL_LOGICAL_V2 使用真实本地 DOCX 与持久化 PDF OCR 缓存进行只读 dry-run，并以逻辑区域、合并单元格跨度和类型优先级收敛可证明的重复差异。

## 本次完成

- V2 规则去重支持显式逻辑单元区域；同一逻辑区域的物理位置会合并保留，跨类型按 `NUMERIC_CHANGED` 等既定优先级仲裁。
- 增加安全合并审计组，输出结构坐标、页码、计数和文字摘要哈希，不写入合同正文。
- V2 Candidate 评审删除候选现在要求双侧规范化文字一致、逻辑区域一致和 `confidence >= 0.95`；文字不同会保留为变化。
- dry-run 改为重新解析本地基准 DOCX，并从数据库内容寻址 OCR 缓存加载目标盖章 PDF；PDF 缓存未命中时安全停止，不回源 OCR。
- dry-run 不调用 OCR/LLM、不写入任务、结果或 checkpoint。

## 修改文件

- `app/comparison/models.py`：增加 V2 内部逻辑区域键和去重审计字段。
- `app/comparison/logical_v2.py`：增加逻辑区域解析、物理位置合并、类型仲裁和安全审计。
- `app/comparison/candidate_validation.py`：使用逻辑区域和高置信度校验 `DUPLICATE_OF`。
- `scripts/final_compare_logical_dry_run.py`：改为真实本地 DOCX/PDF 缓存只读回放。
- `tests/unit/test_final_compare_logical_v2.py`：增加逻辑区域、跨类型、跨表和候选置信度回归。
- 其余既有工作树修改保持不变，未修改 DRAFT_REVIEW 业务算法、OCR、公开 API 或数据库 Schema。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化；dry-run 仅读取既有缓存和结果。
- 配置：无变化。
- 兼容性：默认 `LEGACY` 和 DRAFT_REVIEW 路径保持原行为；V2 内部字段均排除在公开结果之外。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| V2/LLM/comparison 定向 pytest | 通过 | `117 passed`，后续 V2 增补测试单独 `12 passed` |
| 扩展相关后端 pytest | 通过 | `159 passed`，1 个 LangGraph 弃用警告 |
| 变更范围 Ruff | 通过 | V2、候选校验、模型、dry-run 和测试文件全部通过 |
| 变更范围 compileall | 通过 | `app` 及相关脚本成功编译 |
| 前端 format/typecheck/build | 通过 | build 成功，仅保留既有 chunk size warning |
| `git diff --check` | 通过 | 无空白错误 |
| `docker compose --profile tools run --rm test` | 部分通过 | `383 passed, 1 failed`；既有 `test_llm_configuration_defaults` 仍期望 `GLM-5.2`，当前 Settings 为 `GLM-5.3-Flash`，未为本任务修改无关测试 |
| 真实 cache-only V2 dry-run | 通过 | 旧报告 `189`；V2 原始候选 `186`；规则后 `186`；OCR `0`、LLM `0`、数据库写入 `0`；目标 PDF OCR cache `HIT`、DOCX sidecar `HIT` |

## Docker 与运行状态

- API：运行中（本次只读确认）。
- Worker：运行中（本次未停止或重启）。
- PostgreSQL：运行中（本次 dry-run 只读）。
- 控制台：未进行视觉验收；由用户负责。
- 最终是否保持运行：保持现状，未改变服务状态。

## 重要决策

- 现有 PDF OCR 缓存缺少部分逻辑 ID/跨度字段时，V2 保守降级为物理证据键，不激进推断合并；不因文本相似跨表或跨条款删除差异。
- 合并审计不保存双方原文，仅保存字符数、截断哈希和结构/页码信息，符合诊断安全边界。

## 已知问题与风险

- 本次真实样本 dry-run 已从 `189` 收敛到 `186`，但规则去重计数为 `0`；当前 PDF 缓存的目标表格单元缺少逻辑 ID/跨度，因此无法安全证明更多逻辑区域合并。若要进一步收敛，需先由新的 OCR 缓存版本提供可验证跨度，不能在旧缓存上猜测。
- Compose 全量测试尚有 1 个既有默认模型断言失败；未修改其业务配置或测试期望。
- 本轮未执行外部 Candidate Canary、正式 FINAL_COMPARE 任务或控制台人工验收。

## 下一步建议

1. 在离线数据具备逻辑 ID/跨度的情况下复核三组预期重复的 merge audit 组；旧缓存继续保持安全降级。
2. 由用户确认是否将当前 V2 作为候选 Canary 前的实现基线，并单独处理 Compose 的旧模型默认断言。

## 下一会话首先阅读

- `AGENTS.md`
- `app/comparison/logical_v2.py`
- `scripts/final_compare_logical_dry_run.py`
- `tests/unit/test_final_compare_logical_v2.py`

## 交接摘要

V2 已具备真实文件 cache-only 回放、逻辑区域键、物理位置保留、类型仲裁和安全审计。真实 dry-run 成功且无 OCR/LLM/数据库写入，结果从 189 项变为 186 项。现有 PDF cache 缺少逻辑 ID/跨度，规则层按安全策略未继续合并。定向后端 159 项和前端构建通过；Compose 为 383 passed/1 个旧模型断言失败。尚未执行外部 Canary 或正式任务。
