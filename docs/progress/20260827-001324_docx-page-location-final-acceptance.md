# DOCX 页码探针与正式三文件验收最终记录

## 结论

甲方 DOCX 页码探针成功，页码映射代码未再修改。正式三文件验收因外部文档解析服务返回 `UPSTREAM_502` 而失败，未生成正式报告，因此本阶段仍不能宣称真实闭环完成。

- 日期：2026-08-27
- 分支：`feat/draft-review-multidoc`
- 基线提交：`23246f2`
- 既有成功任务：`tsk_01M0Z48FK9QFS0J83HV14GMNP0`
- 本轮下载配置错误任务：`tsk_01M0ZCV4KSQ9RM7J493BNQ6M38`
- 本轮外部解析失败任务：`tsk_01M0ZDFXTFZAM7A2WDMRY650KY`
- 未执行 commit、push、reset、retry 或清理 `.real-diagnostic-temp/`

## 真实探针

- 同一份脱敏 DOCX 仅调用甲方解析接口一次，客户端重试为 0。
- 外部页号完整：`46/46`，`page_count=46`，`external_detail_page_count=46`。
- 安全映射摘要：本地结构 `2958`，外部结构 `2562`，候选映射 `494`，已映射 `1666`，未映射 `1292`，覆盖率 `0.563218`。
- 结论：甲方服务能够返回完整 DOCX `page_id`；部分未映射结构不再触发整份文档门禁。

## 正式任务尝试

### 第一次任务

- 任务 `tsk_01M0ZCV4KSQ9RM7J493BNQ6M38` 在 `DOWNLOADING / 10%` 失败。
- 错误：`DOWNLOAD_FORBIDDEN_TARGET`。
- 原因：本地 Compose 的 `fixture-server` 未加入下载 allowlist。
- 未 retry，未复用该失败任务。

### 唯一新任务

- 任务 `tsk_01M0ZDFXTFZAM7A2WDMRY650KY` 使用 `fixture-server` allowlist 创建。
- API 和 Worker 实际配置均核实为 `ALLOW_HTTP_DOWNLOADS=true`、`DOWNLOAD_HOST_ALLOWLIST=fixture-server`、`DOCX_PAGE_LOCATION_ENABLED=true`。
- 下载成功并进入 `PARSING / 35%`，说明本地 allowlist 阻塞已排除。
- 首个明确错误：`DOCX_PAGE_LOCATION_INCOMPLETE`，`failure_stage=EXTERNAL_PARSE`，`failure_code=OCR_SERVICE_UNAVAILABLE`，`failure_kind=UPSTREAM_502`，外部解析尝试 3 次后失败。
- 任务未进入结果生成、公开证据补全或控制台正式报告；未调用 retry，未创建第三个任务。

## 配置恢复

- 验收结束后 API/Worker 已强制重建。
- 实际白名单已恢复为 `.env` 中的甲方正式文件域名；`fixture-server` 和通配符均不在交付 allowlist 中。
- `ALLOW_HTTP_DOWNLOADS=true`、`DOCX_PAGE_LOCATION_ENABLED=true` 保持实际生效。
- `/ready` 已确认数据库、OCR 配置和 LLM 配置健康。

## 验收状态

由于正式任务在外部解析阶段失败，以下项目未验证、未宣称通过：

- `SUCCEEDED / COMPLETED / 100` 和控制台可见正式报告；
- 39 项差异、4 项通过；
- 所有展示证据的文件名、真实页码和缺失位置格式；
- 局部差异高亮及建议覆盖率 100%；
- 新旧结果的事实 ID、payload digest、checkpoint 命中键和确定性差异保持。

## 离线验证依据

- Compose PostgreSQL 全量测试：`346 passed, 1 warning`
- 主机后端单元测试：`331 passed, 1 warning`
- 前端格式测试、typecheck、build：通过
- 变更文件 Ruff、compileall、`git diff --check`：通过

## 后续边界

- 页码映射算法不再调整；下一次真实验收需先确认甲方解析服务恢复，或由甲方提供可用的服务侧 502 处理窗口。
- 本轮两个失败任务均不得 retry；后续如重新验收，必须由用户明确授权创建新的唯一任务。
- 当前页码功能仍未提交里程碑，待正式任务成功并完成控制台验收后再提交。
