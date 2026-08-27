# 放款阶段 FINAL_COMPARE 与印章影像 Tab 实施记录

## 结论

本次功能代码已完成并通过相关静态检查及单元测试；唯一一次正式真实 FINAL_COMPARE 任务未达到成功门槛，因此未宣称业务验收通过，也未重试或新建任务。

## 实施范围

- 普通 OCR 请求继续使用 `get_image=none`；仅放款比对的 TARGET PDF 使用 `get_image=objects` 与 `image_output_type=base64str`。
- 从 OCR `detail` 和页内容中读取 `type=image/sub_type=stamp`，按页码和归一化位置去重；不读取或输出印章文字、上游图片 URL、路径、鉴权头或密钥。
- 仅物化 PNG/JPEG，并增加印章数量、单图大小和总数据量上限；图片数据不可安全承载或超限时安全失败。
- 结果协议增加可选 `stamp_images`；仅 FINAL_COMPARE 报告开启“印章影像”Tab，DRAFT_REVIEW 不开启。页面使用固定免责声明并再次拒绝非 data URI 图片源。
- 未新增业务路由，未修改 SSRF 校验，未修改正式 `.env` 或正式下载白名单。

## 验证摘要

- 后端相关测试：46 passed。
- 前端 `test:format`、`typecheck`、`build`：通过。
- 变更范围 Ruff：通过。
- `compileall`：通过。
- `git diff --check`：通过；Git 仅报告既有工作树换行提示。
- OCR 图片直达资源探针未在安全等待时间内完成；未向浏览器暴露上游资源，采用受限 Base64 承载策略。
- 目标文件下载地址仅执行了状态和大小检查，未记录地址或完整上游响应。

## 正式任务

- 唯一任务 ID：`tsk_01M10GRV9XB5D4NVQ3BWTBW13M`。
- 输入角色：DOCX 为 `BASELINE`，盖章 PDF 为 `TARGET`。
- 任务仅执行 1 次，`attempt_count=1`；未重试、未新建任务。
- OCR 和差异计算阶段已推进；任务最终状态为 `FAILED`，失败时状态进度为 97%。
- 首个可定位失败阶段：`PUBLIC_EVIDENCE_MAPPING`。
- 安全错误摘要：`DOCX_PAGE_LOCATION_INCOMPLETE`，原因 `PUBLIC_LOCATION_UNMAPPED`；25 页中 527 个本地结构有 523 个候选映射，仍有 4 个位置未映射。未通过门槛，未将任务标记为成功。

## 控制台验收

因正式任务失败，按要求立即停止，没有打开失败任务报告页，也没有将未完成结果用于控制台业务验收。差异高亮、文件名/页码、AI 建议、通过项及“印章影像”Tab 的真实页面验收留待页码映射问题修复后的新授权任务；本记录不降低该门槛。

## 环境恢复

- 临时宿主机 Worker 和只读文件服务已停止，并确认临时文件服务监听端口已释放。
- Docker Worker 已恢复运行；PostgreSQL 继续运行于项目既有 Docker 映射，API 健康、数据库和 OCR/LLM 配置就绪检查均正常。
- 正式下载白名单和交付配置未写入临时地址，未被修改。
- `.real-diagnostic-temp/` 未清理；本次专用运行日志保留在 `tmp/formal-final-compare/` 供诊断核查。

