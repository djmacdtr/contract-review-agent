# 任务进度：Text 平衡二分恢复与子批次 Canary

## 基本信息

- 时间：2026-08-28 16:38:02 +08:00
- 状态：BLOCKED
- 任务类型：FIX / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留既有未提交修改、历史进度和 `.real-diagnostic-temp/`

## 用户目标

将 text 多单元恢复从逐单元拆分改为连续平衡二分，验证 8 单元子批次后再执行唯一正式恢复任务；numeric、公开接口和既有业务规则保持不变。

## 本次完成

- 新增 `text_recovery_blocks()`，按连续结构单元执行平衡二分：16→8+8，后续 8→4+4，奇数单元保持完整顺序和覆盖。
- text 恢复路径改用该分组；numeric 专用 `12→6→3→1` 恢复未修改。
- Canary 脚本改为仅重建两个 8 单元 text 子批次，不再发送已失败的 16 单元父批次。

## 修改文件

- `app/draft_review/extraction.py`：增加 text 平衡二分恢复并接入 text 多单元失败路径。
- `scripts/expanded_fact_canary.py`：重建并执行两个 8 单元 text 子批次。
- `tests/unit/test_structured_extraction_v2.py`：增加 16、8 和奇数单元的顺序/覆盖测试，调整恢复夹具验证 16→8+8。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化。
- 配置：未修改；Canary 使用既定 `json_schema`、8192 tokens、payload 24000、numeric 12、text 16、并发 1。
- 兼容性：不改变 numeric、表格恢复、checkpoint 身份或历史 checkpoint 兼容范围。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `pytest ... -k "text_recovery_blocks or multi_unit_truncation or numeric_truncation or table_truncation"` | 通过 | 6 passed，42 deselected |
| `ruff check app/draft_review/extraction.py scripts/expanded_fact_canary.py tests/unit/test_structured_extraction_v2.py` | 通过 | All checks passed |
| `python -m compileall`（变更文件） | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅有既有换行格式提示 |
| 两个 8 单元 text 子批次 Canary | 未通过 | 2 次 HTTP 200；均为 `FACT_BATCH_SATURATED` / `LLM_EXTRACTION_EVIDENCE_INVALID` |

## Docker 与运行状态

- API：running / healthy。
- Worker：running。
- PostgreSQL：running / healthy。
- 控制台：未进行视觉验收。
- 最终是否保持运行：保持现状，未停止或重启服务。

## 重要决策

- 保留 `FACT_BATCH_SATURATED` 安全门，不把饱和响应当作完整事实。
- 8 单元子批次均饱和后，本轮不继续拆分、不追加 LLM 请求、不创建正式恢复任务。

## 已知问题与风险

- 16 单元父批次的调用膨胀问题已修正，但当前 GLM 在两个 8 单元子批次上仍返回饱和结果。
- 正式恢复任务尚未创建；39 项差异、4 项通过、页码和建议覆盖率未验收。

## 下一步建议

1. 后续独立决定是否按既定止损规则继续处理 text 12 单元门禁或认定当前 text 批次/模型配置不可用。
2. 在 text Canary 成功前，不创建来源任务 `tsk_01M13PH5H5EAWJXJRCFKH00PH0` 的正式恢复任务。

## 下一会话首先阅读

- `app/draft_review/extraction.py`
- `scripts/expanded_fact_canary.py`
- `docs/progress/20260828-163133_text-recovery-split-diagnosis.md`
- `docs/progress/20260828-163802_text-balanced-recovery-canary-report.md`

## 交接摘要

Text 多单元恢复已改为连续平衡二分，定向测试 6 passed。
两个 8 单元子批次各调用一次，均 HTTP 200 但返回 `FACT_BATCH_SATURATED`。
未重试父批次，未创建正式恢复任务，也未追加外部调用。
numeric、表格恢复、配置、API 和 checkpoint 逻辑保持不变。
API、Worker、PostgreSQL 保持原运行状态。
