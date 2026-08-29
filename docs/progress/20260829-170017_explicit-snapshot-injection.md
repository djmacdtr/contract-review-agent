# 显式文档快照注入诊断记录

## 范围

- 目标任务：`tsk_01M167E69YV0MGNHENB9HW10DG`
- 来源任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`
- 本次未创建新任务，未调用公开 retry 接口。
- Docker Worker 保持停止；API 和 PostgreSQL 保持运行。

## 实现

- 再生成工作流支持显式注入 `document-extraction-v1` 文档快照。
- checkpoint 来源优先使用 `_report_regeneration_source_task_id`，兼容普通 `source_task_id`。
- 快照加载器按来源任务、文件 SHA-256 和抽取版本读取，并在正式解析上下文中执行证据重绑定。
- 通过后可将三份快照物化到当前任务，并在抽取阶段返回零模型调用结果。
- 运维脚本区分 ORM checkpoint 行模型和抽取 checkpoint 数据类，避免物化阶段对象装配错误。

## 离线检查

- `tests/unit/test_report_regeneration.py` 与 `tests/unit/test_draft_review_workflow.py`：`58 passed`。
- 变更范围 Ruff：通过。
- `compileall`：通过。
- `git diff --check`：通过；仅保留工作区原有换行提示。

## 零调用预检

- 执行入口：`--snapshot-only --task-id tsk_01M167E69YV0MGNHENB9HW10DG`。
- 预检启用正式 DOCX 页码绑定，并限定为本地文件、OCR 缓存和数据库读取；未发起 OCR、LLM 或其他外部请求。
- 进程持续进行完整页码结构映射，超过安全观察窗口后由操作端中止；未生成预检结果文件，未写入当前任务 checkpoint，未改变任务状态。
- 因正式页码上下文未完成，本轮未执行快照物化确认、`max_attempts` 提升或第三次领取。

## 当前状态

- 目标任务：`FAILED`，`attempt_count=2`，`max_attempts=2`，成功 checkpoint 数为 `0`，审计事件数为 `14`。
- 来源任务结果保持只读未变更。
- 未完成项：正式页码预检尚未达到来源快照 `3/3`、正式解析证据 `3/3`、当前任务物化 `3/3` 和预计抽取调用 `0` 的联合门禁；因此未执行宿主机 Worker。
