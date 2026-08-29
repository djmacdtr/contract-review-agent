# 页码跨结构映射与公开证据门禁记录

## 范围

- 日期：2026-08-29
- 目标：修复 DOCX 表格与甲方 OCR 扁平段落之间的页码绑定，并将页码失败定位到实际公开证据位置。
- 约束：未执行 retry、未创建第二个任务、未调用新的 OCR/LLM；保留 `.real-diagnostic-temp/` 和现有工作树。

## 实现

- `TABLE/TABLE_CELL` 支持按外部 OCR 段落的精确紧凑文本和顺序锚点映射。
- 已唯一定位的表格允许其无法独立定位的单元格继承该表的真实页码；不使用段落比例、总页数或固定数量估算。
- sidecar 仍按现有 `sys_page_location_cache_v1` / `docx-page-location-v1` 缓存，值只包含页码、逻辑位置和安全统计。
- 页码公开门禁和 Worker 错误日志补充首个公开文件 ID、逻辑位置及覆盖计数，不记录正文、完整响应、URL 或密钥。
- `page_enrich` 前保存页码无关的内部结果快照，便于后续只重做页码阶段。

## 离线验证

- 定向页码、文档路由和页码前结果快照测试：`34 passed, 1 warning`。
- 变更范围 Ruff：通过。
- 相关 Python `compileall`：通过。
- `git diff --check`：通过；仅有既有换行转换提示和临时测试目录权限提示。

## 零外部调用页码预检

输出：[20260829-212500_page-location-preflight.json](D:/work/contract_review/contract-review-agent/docs/progress/20260829-212500_page-location-preflight.json)

- 状态：`PAGE_LOCATION_PREFLIGHT_OK`
- 来源成功任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`
- sidecar：3/3；外部调用：0；OCR：0；LLM：0。
- 目标：46 页，2878/2958 结构位置已有页码；目标表 2 已绑定真实页码 `(17, 33, 39, 45, 46)`。
- 模板：24 页，422/424 结构位置已有页码。
- 辅助资料：2 页，95/95 结构位置已有页码。
- 已知恢复失败的公开触发位置覆盖：2/2（目标表 2、模板表 0）。剩余未映射项属于未被该公开触发验证的内部结构，不据此放宽公开证据门禁。

## 唯一恢复结果

- 来源任务：`tsk_01M16MWEMN7SAVK42HER1NRNVB`
- 唯一恢复任务：`tsk_01M16QQ6WG4S4D73ME0QF13HSH`
- 来源 checkpoint：55；新任务已保存：6。
- 宿主机 Worker 已领取一次；Docker Worker 保持停止。
- LLM：8 次 HTTP 调用，HTTP 200 为 8 次，`finish_reason=stop` 为 8 次；OCR：0 次。
- 任务在 `GENERATING_ADVICE / 92%` 进入公开页码门禁后失败：`DOCX_PAGE_LOCATION_INCOMPLETE`，首个安全子码为 `PUBLIC_LOCATION_UNMAPPED`。
- 首个公开触发位置：目标文件 `table_index=2`；失败时旧 sidecar 统计为 1668/2958，未映射 1290。
- 控制台路径：[/console/#/tasks](#/console/#/tasks)；报告路径：`/console/#/tasks/tsk_01M16QQ6WG4S4D73ME0QF13HSH/report`。

## 当前结论与未完成项

- 跨结构页码修复已通过定向测试，且基于既有 OCR 缓存完成零外部调用预检；当前缓存已覆盖上述两个已知公开触发位置。
- 唯一恢复任务仍为失败状态，未产生最终持久化报告，因此不能宣称三文件最终控制台闭环、风险/通过项完整性或建议覆盖率已完成。
- 本轮不再调用外部服务、不重试失败任务、不创建新任务；后续若继续正式验收，应由新的明确任务生命周期授权决定。
