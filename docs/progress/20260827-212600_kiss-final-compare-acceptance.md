# KISS 交付路径 FINAL_COMPARE 验收记录

## 结论

在 DRAFT_REVIEW 三文件真实任务成功后，按计划只创建了一次 DOCX/PDF `FINAL_COMPARE` 任务。任务已在版本对齐阶段安全失败，未 retry、未创建第二个 FINAL_COMPARE 任务，也未修改 FINAL_COMPARE 代码掩盖对齐问题。

- 日期：2026-08-27
- 任务：`tsk_01M11NGRKE35H16HZNGVQN81T4`
- `client_reference_id`：`kiss-final-real-20260827-1311`
- 状态：`FAILED / VERSION_COMPARE / 68%`
- 首个公开安全错误：`COMPARISON_UNRELIABLE`
- 安全消息：两份合同的内容对齐覆盖率不足，未生成正式报告
- 控制台路径：`http://127.0.0.1:8000/console/#/tasks`
- 未调用 retry、未创建第二个任务、未执行 commit、push、reset 或 clean

## 执行环境

- API、PostgreSQL 使用 Compose；Docker Worker 在任务期间停止。
- 宿主机 Worker 连接 Docker PostgreSQL `127.0.0.1:15432`，通过宿主机网络访问甲方 OCR/LLM。
- 本地只读文件服务提供 `融资租赁合同（回租）.docx` 和 `融资租赁合同（回租）.pdf`；两份文件均下载成功。
- 宿主机 Worker 使用 `DOCX_PAGE_LOCATION_ENABLED=true`、`OCR_HTTP_RETRY_ATTEMPTS=0`、`LLM_RESPONSE_FORMAT=json_schema` 和 `LLM_NATIVE_STRUCTURED_OUTPUT=true`。
- 任务失败后宿主机 Worker 与文件服务已停止，Docker Worker 已恢复启动。

PDF 离线检查为 46 页，每页包含文本和图像；失败发生在 `VERSION_COMPARE` 对齐门，不是下载、OCR 配置或持久化错误。任务详情未提供更细的公开错误详情，本记录不补写合同正文、OCR 响应或模型响应。

## 已完成验证

- DRAFT_REVIEW KISS 三文件任务已成功完成，任务为 `tsk_01M11MBF3C5T9Y7SAB6DDFFPAT`。
- FINAL_COMPARE 任务详情 GET 已确认进入 `VERSION_COMPARE / 68%` 后失败。
- API 与 Docker 服务已恢复；未保留本地文件服务地址或临时下载白名单。

## 未完成项

- FINAL_COMPARE 未生成正式结果，因此本轮未验证最终文字差异、真实页码、局部高亮、AI 建议和“印章影像”Tab。
- 未运行完整 Compose 回归、Docker 冒烟或部署重启验收；按照失败边界停止，不立即重跑真实任务。
- 后续应基于 `COMPARISON_UNRELIABLE` 的首个对齐子问题做离线诊断，获得明确代码级修复后再由用户授权新的唯一 FINAL_COMPARE 验收；不得通过降低可靠性阈值发布结果。
