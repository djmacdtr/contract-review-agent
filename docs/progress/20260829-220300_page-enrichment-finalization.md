# 页码阶段收口最终记录

## 任务范围

- 目标任务：`tsk_01M16QQ6WG4S4D73ME0QF13HSH`
- 操作：从同任务的页码前结果快照重新加载最新持久化 sidecar，仅执行页码补全和结果发布。
- 快照版本：`draft-result-pre-page-v1`
- 快照内容哈希：`630a125466ff06d68700e826039d7fce`
- 来源任务和旧报告均未修改；未 retry、未创建新任务、未调用 OCR/LLM。

## Dry-run

- 状态：`READY_TO_APPLY`
- sidecar：3/3，使用全新进程从数据库缓存加载并重绑定当前任务文件 ID。
- 文件页数：目标 46、模板 24、辅助资料 2。
- 公开证据页码覆盖：120/120，缺失 0。
- 结果校验：39 个风险、39 个差异、4 个通过项；Advice 非空 39/39，模型建议 39，fallback 0。
- OCR 调用：0；LLM 调用：0。

## 单事务收口

- 写入结果：成功。
- 任务最终状态：`SUCCEEDED / COMPLETED / 100%`。
- TaskResult：新写入；TaskFile 页数、SHA 和解析状态同步更新。
- 审计事件：新增一个 `COMPLETED` 事件，记录快照哈希、sidecar 版本、页码覆盖、Advice 覆盖及 OCR/LLM 零调用统计。
- 幂等：任务已有结果后，脚本只返回 `TASK_RESULT_ALREADY_EXISTS`，不会重复写入。
- 控制台任务路径：[/console/#/tasks](#/console/#/tasks)
- 控制台报告路径：[/console/#/tasks/tsk_01M16QQ6WG4S4D73ME0QF13HSH/report](#/console/#/tasks/tsk_01M16QQ6WG4S4D73ME0QF13HSH/report)

## 版本与 Worker 加固

- 页码算法标识已从 `docx-page-location-alignment-v1` 升级为 `docx-page-location-alignment-v2`。
- 页码缓存版本已从 `docx-page-location-v1` 升级为 `docx-page-location-v2`，避免不同算法继续共用同一缓存身份；旧 v1 缓存不会被新流程误用。
- `DocumentParsingRouter` 每次 DOCX 解析前清除同文件的内存 sidecar，再从持久化内容寻址缓存重新加载，避免 Worker 跨任务复用旧内存对象。
- 相关路由、页码、缓存和收口脚本定向测试：35 passed, 1 warning。
- 变更范围 Ruff、相关 Python compileall、`git diff --check`：通过。

## 运行环境与未完成项

- API、PostgreSQL：健康。
- Docker Worker：保持停止，避免领取其他任务。
- 宿主机 Worker 和临时文件服务：已停止/未运行。
- 本阶段目标已完成；控制台视觉和建议语气仍需人工抽查。
- 未执行 commit、push、reset、clean，未清理 `.real-diagnostic-temp/`。
