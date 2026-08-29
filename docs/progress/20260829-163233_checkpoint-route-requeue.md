# 任务进度：checkpoint 来源路由修复与唯一重排队执行

## 基本信息

- 时间：2026-08-29 16:32:33 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / CONTROLLED ACCEPTANCE
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 工作树状态：dirty；保留本会话及此前所有未提交修改

## 本轮目标

修复报告再生成任务使用 `_report_regeneration_source_task_id` 作为抽取 checkpoint 来源的路由问题；在不调用 OCR/LLM 的情况下确认三份文档快照命中并完成证据重绑定，然后复用既有任务 `tsk_01M167E69YV0MGNHENB9HW10DG` 进行唯一一次重新执行。

## 已完成

- Docker Worker 保持停止，API 和 PostgreSQL 健康。
- `DraftReviewWorkflowExecutor` 的抽取来源优先读取 `_report_regeneration_source_task_id`，并兼容普通任务的 `source_task_id`。
- 新增脚本 `--snapshot-only`：使用正式本地 `ParserRegistry`、文档 checkpoint identity、payload digest 和 `_validated_document_checkpoint` 执行零外部调用诊断。
- 快照诊断结果为 `3/3` 命中、`3/3` 证据重绑定通过；角色、当前文件 ID、batch ID 和 payload digest 仅作为安全摘要输出，未记录正文。
- 新增内部重排队方法：保留 `attempt_count=1`，将 `max_attempts` 从 1 调整为 2，恢复 `PENDING/QUEUED`，清除上次失败字段，并写入 `REPORT_REGENERATION_REQUEUE / CHECKPOINT_SOURCE_ROUTE_FIXED` 审计事件。
- 同一任务第二次领取已执行一次，`attempt_count=2`、`max_attempts=2`；来源任务和来源结果未修改。

## 唯一执行结果

- 任务：`tsk_01M167E69YV0MGNHENB9HW10DG`
- 结果：`FAILED`
- 首个安全错误：`failure_stage=SNAPSHOT_PREFLIGHT`，`failure_code=DOCUMENT_EXTRACTION_CHECKPOINT_MISSING`，外层任务码为 `REPORT_REGENERATION_SNAPSHOT_INCOMPLETE`。
- 事实抽取调用：0；映射、Advice 和结果页码阶段未执行；执行报告中的 LLM HTTP 调用数为 0。
- 当前任务已达到 `attempt_count=2/max_attempts=2`，不再 retry、不创建新任务、不修改来源报告。

## 离线检查

- `tests/unit/test_report_regeneration.py`：3 passed。
- 变更文件 Ruff：通过。
- 变更文件 compileall：通过。
- `git diff --check`：通过；仅有工作树原有换行格式提示，无 diff 错误。

## 未完成项

- 虽然零外部精确诊断在无页码 sidecar 的本地解析上下文中达到 3/3，但正式 Worker 解析上下文仍未加载到文档快照；需要后续单独定位正式解析身份（尤其页码绑定后结构/候选身份）与诊断身份的差异。
- 本轮未进入跨文件映射、Advice、严格页码公开证据校验和控制台报告验收。
- Docker Worker 仍保持停止，等待后续明确运维决定。

## 保护事项

- 未创建第二个再生成任务，未调用公开 retry，未重试甲方 OCR/LLM。
- 未执行 commit、push、reset、checkout、clean，未清理 `.real-diagnostic-temp/`。
