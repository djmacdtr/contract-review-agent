# DOCX 页码真实探针重跑与正式验收记录

## 结论

本轮同一份 DOCX 的真实页码探针再次成功，甲方接口能力稳定返回完整页号。随后使用本地 `fixture-server` 白名单创建了一次新的正式三文件任务，但任务在外部解析阶段再次收到 `OCR_SERVICE_UNAVAILABLE`，未生成正式报告。本轮已停止，不 retry、不创建后续任务。

- 日期：2026-08-27
- 分支：`feat/draft-review-multidoc`
- 基线提交：`23246f2`
- 本轮唯一新正式任务：`tsk_01M10BEX47C3MA1FFWB12WENK0`
- 任务状态：`FAILED / PARSING / 35%`
- 旧失败任务未 retry：`tsk_01M0ZCV4KSQ9RM7J493BNQ6M38`、`tsk_01M0ZDFXTFZAM7A2WDMRY650KY`
- 未执行 commit、push、reset 或清理 `.real-diagnostic-temp/`

## 真实探针

- 同一份脱敏 DOCX 仅调用甲方解析接口一次，探针客户端重试为 0。
- 外部页号完整：`46/46`，`page_count=46`、`external_detail_page_count=46`。
- 安全映射摘要：本地结构 `2958`，外部结构 `2562`，候选映射 `494`，已映射 `1666`，未映射 `1292`，覆盖率 `0.563218`。
- 页码来源确认是甲方返回的 `page_id`；未使用任何页码估算方案。

## 正式任务

- 本地验收期间 API/Worker 实际配置为 `ALLOW_HTTP_DOWNLOADS=true`、`DOWNLOAD_HOST_ALLOWLIST=fixture-server`、`DOCX_PAGE_LOCATION_ENABLED=true`，并已通过容器环境核实。
- 新任务下载成功，进入 `PARSING / 35%`，说明 allowlist 配置问题已排除。
- 首个明确错误：`DOCX_PAGE_LOCATION_INCOMPLETE`，`failure_stage=EXTERNAL_PARSE`，`failure_code=OCR_SERVICE_UNAVAILABLE`；安全诊断显示外部详情和映射均为 0，本地结构为 2958。
- 任务未进入公开证据补全、结果生成或控制台正式报告；没有调用 retry，也没有创建第四个页码验收任务。

## 配置恢复

- 验收结束后已强制重建 API/Worker。
- 两者实际 `DOWNLOAD_HOST_ALLOWLIST` 均恢复为 `.env` 中的甲方正式文件域名，`fixture-server` 和通配符均未保留。
- `ALLOW_HTTP_DOWNLOADS=true`、`DOCX_PAGE_LOCATION_ENABLED=true` 保持生效。
- `/ready` 返回数据库、OCR 配置和 LLM 配置均正常。

## 验收结果

由于甲方外部解析再次返回 502，以下项目仍未验证、未宣称通过：

- `SUCCEEDED / COMPLETED / 100` 和控制台正式报告；
- 39 项差异、4 项校验；
- 所有展示证据页码、缺失位置格式和文件名；
- 局部差异高亮及建议覆盖率 100%；
- 新旧任务事实 ID、payload digest、checkpoint 命中键和确定性差异保持。

浏览器视觉验收未执行，因为本轮没有成功的正式报告页。

## 离线验证依据

本轮未重跑代码测试，沿用已确认结果：Compose PostgreSQL 全量测试 `346 passed`，前后端构建、变更文件 Ruff、compileall 和 `git diff --check` 均通过。

## 后续边界

- 页码和映射代码不再调整；当前阻塞为甲方文档解析服务的 `UPSTREAM_502`。
- 本轮任务不得 retry；如需再次正式验收，应先由甲方确认 OCR 服务恢复并由用户明确授权创建新的唯一任务。
- 正式任务成功后再提交页码功能里程碑；当前保持未提交状态。
