# DOCX 页码真实探针与正式三文件验收记录

## 结论

代码和离线验证通过；唯一真实 DOCX 探针成功。随后创建的唯一正式三文件任务在下载阶段因 Compose fixture 主机不在下载 allowlist 中失败，未进入文档解析、页码映射或正式报告生成。本轮按规则停止，未重试任务。

- 日期：2026-08-27
- 分支：`feat/draft-review-multidoc`
- 基线提交：`23246f2`
- 唯一正式任务：`tsk_01M0ZCV4KSQ9RM7J493BNQ6M38`
- 任务状态：`FAILED / DOWNLOADING / 10%`
- 业务关联 ID：`formal-docx-page-location-20260827-000054`
- 既有三文件任务：`tsk_01M0Z48FK9QFS0J83HV14GMNP0`
- 保护项：未执行 commit、push、reset、retry 或清理 `.real-diagnostic-temp/`

## 唯一真实探针

- 输入：同一份脱敏 DOCX，使用 `python-docx` 和现有甲方文档解析适配器。
- 请求次数：1；`OCR_HTTP_RETRY_ATTEMPTS=0`；未发生重试。
- 结果：成功返回完整物理页号，`page_count=46`、`external_detail_page_count=46`。
- 安全映射摘要：本地结构 `2958`，外部结构 `2562`，候选映射 `494`，已映射位置 `1666`，未映射位置 `1292`，覆盖率 `0.563218`。
- 映射覆盖不足仅涉及未公开或无法唯一定位结构；探针没有因整份 DOCX 全结构覆盖不足失败。
- 页码来源为外部文档解析服务的 `page_id`，没有使用段落比例、DOCX 总页数属性或固定数量估算。

## 正式任务

- API、worker 已通过 Compose 重建并启用 `DOCX_PAGE_LOCATION_ENABLED=true`；DOCX 解析重试设为 `0`。
- 使用目标合同、模板和项目方案确认函创建了唯一一次正式 `DRAFT_REVIEW` 任务。
- 首个错误：`DOWNLOAD_FORBIDDEN_TARGET`，安全消息为“文件地址主机不在允许列表”。失败发生在下载阶段，未调用甲方 DOCX 解析接口。
- 根因范围：当前运行配置的 `DOWNLOAD_HOST_ALLOWLIST` 只包含甲方文件域名；正式请求使用 Compose 内部 `fixture-server:8080` 地址，未被该 allowlist 接受。
- 已读取错误并停止；没有修改 allowlist、没有调用 retry、没有原样创建第二个任务。

## 真实验收状态

以下项目因正式任务未进入解析而未通过、未宣称完成：

- `SUCCEEDED / COMPLETED / 100` 和控制台正式报告：未达成。
- 39 项差异、4 项校验：未验证。
- 正式展示证据的真实文件名和页码：未验证。
- 缺失位置页前/页后/页间格式：未验证。
- 局部差异高亮、39 项建议覆盖率 100%：未验证。
- 新旧任务的事实 ID、payload digest、checkpoint 命中键和确定性差异保持：未验证。
- 浏览器视觉验收：未执行，因为没有成功正式报告页。

## 离线验证

- 主机后端单元测试：`331 passed, 1 warning`
- 页码、路由、TextIn mapper、配置定向测试：`34 passed`
- Compose PostgreSQL 全量测试：`346 passed, 1 warning`
- 变更文件 Ruff：通过
- `python -m compileall -q app scripts tests`：通过
- 前端格式测试、typecheck、build：通过；build 仅有既有 chunk size warning
- `git diff --check`：通过

## 未完成项

- 下一轮若继续正式验收，必须先为 Compose fixture 使用受控 allowlist 配置或改用已允许的文件地址，然后再由用户明确授权创建新的唯一任务；本记录中的失败任务不得 retry。
- 修复配置后仍需重新完成一次正式三文件任务和控制台验收；不得把本轮任务标记为成功。
- 本轮页码代码和探针实现仍保留在未提交工作区，尚未提交里程碑。
