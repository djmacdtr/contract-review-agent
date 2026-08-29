# KISS DRAFT_REVIEW 三文件真实验收记录

## 结论

本轮按 KISS 交付图完成唯一一次全新三文件 `DRAFT_REVIEW` 任务。任务成功持久化并可从任务列表查询；旧的全文事实抽取、事实评审、映射评审和语义规划节点未进入默认图。

- 日期：2026-08-27
- 任务：`tsk_01M11MBF3C5T9Y7SAB6DDFFPAT`
- `client_reference_id`：`kiss-draft-real-20260827-1249`
- 状态：`SUCCEEDED / COMPLETED / 100`
- 控制台：`http://127.0.0.1:8000/console/#/tasks`
- 本轮未 retry、未创建第二个正式任务、未执行 commit、push、reset 或 clean

## 执行环境

- API、PostgreSQL 使用 Compose；Docker Worker 在真实任务期间保持停止。
- 宿主机 Miniconda Python Worker 连接 Docker PostgreSQL `127.0.0.1:15432`。
- 三份脱敏文件由宿主机只读 HTTP 服务 `127.0.0.1:18081` 提供；下载请求均成功。
- 宿主机 Worker 进程级配置：`ALLOW_HTTP_DOWNLOADS=true`、白名单为 `127.0.0.1`、`DOCX_PAGE_LOCATION_ENABLED=true`、`LLM_RESPONSE_FORMAT=json_schema`、`LLM_NATIVE_STRUCTURED_OUTPUT=true`。
- Worker 启动日志仅记录结构化输出模式、开关和模型名，未记录密钥。

任务成功后已停止宿主机 Worker 与临时文件服务，并重新启动 Docker Worker。API/Worker 当前实际配置核对为：

- `ALLOW_HTTP_DOWNLOADS=true`
- `DOWNLOAD_HOST_ALLOWLIST=tk1r7ibsv.hd-bkt.clouddn.com`
- `LLM_RESPONSE_FORMAT=json_schema`
- `LLM_NATIVE_STRUCTURED_OUTPUT=true`
- 未保留 `127.0.0.1`、`fixture-server` 或通配符白名单

## 结果统计

| 项目 | 结果 |
| --- | ---: |
| `diff_items` | 39 |
| `risk_items` | 39 |
| `passed_checks` | 1 |
| `review_items` | 0 |
| 非空 `analysis_advice` | 39/39 |
| 建议列表覆盖 | 39/39 |
| 缺失证据项 | 2/2 有页前/页后锚点 |
| warning | 16 |

文件页数来自本次真实 DOCX 解析结果：目标合同 46 页、模板 24 页、项目方案确认函 2 页。56/60 个差异展示侧具有有效页码；4 个表格结构展开侧无可靠页码，仅保留文件名并产生 `DOCX_PAGE_LOCATION_PARTIAL` warning，没有回退显示段落、表格、行或列编号。39 个风险均有来源证据；38 个风险证据具有有效页码，剩余位置按同一缺页边界安全降级。

模板解析诊断为可靠，结果元数据为 `HYBRID / workflow 0.8.0 / rules 0.7.0`。本次跨资料候选共 60 组，超过上限的 67 组被业务 warning 标记；交叉判断和 Advice 均按失败软降级，确定性模板结果仍成功发布，全部 39 项风险使用了非空确定性建议。

## 验证记录

- 任务详情 GET 轮询阶段：`PARSING → CROSS_VALIDATE → GENERATING_ADVICE → COMPLETED`。
- 结果 GET 核对：39 个差异、39 个风险、1 个通过项、0 个 review item、39 条建议。
- 任务列表 GET 按 `client_reference_id` 查询返回唯一任务，证明控制台列表可见。
- 前端报告格式代码使用文件名和页码/页范围；缺页只显示文件名或缺失边界锚点，不回退技术位置。
- 当前会话无可用浏览器实例，未能执行截图级视觉检查；已完成 API 结果和前端格式实现的只读核对。

## 未完成项

- 未执行 FINAL_COMPARE 的本轮真实 DOCX/PDF 验收；该流程按计划在 DRAFT_REVIEW 闭环后另行执行。
- 未运行完整 Compose 回归、Docker 冒烟或部署重启验收；本轮只运行了简化链路相关定向测试及代码门禁。
- 当前开发机 Docker Worker 的 DOCX 页码开关仍按 `.env` 正式默认值关闭；真实任务使用宿主机 Worker 显式开启页码解析。正式部署时需由部署环境明确开启该开关并从 Worker 侧先做 OCR 探针。
