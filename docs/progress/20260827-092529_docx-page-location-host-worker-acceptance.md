# 宿主机 Worker DOCX 页码正式验收记录

## 结论

本轮按 Docker 网络诊断改用宿主机 Python Worker，成功绕过 Docker Worker 到甲方 OCR 的网络阻断。三份 DOCX 已完成下载和外部页码解析，随后在事实抽取阶段以 `DYNAMIC_CHECK_INCOMPLETE` 失败，未生成正式报告。本轮不 retry、不创建后续任务。

- 日期：2026-08-27
- 分支：`feat/draft-review-multidoc`
- 基线提交：`23246f2`
- 本轮唯一新任务：`tsk_01M10C9J6HDXNW429E051KAZ12`
- 任务状态：`FAILED / FACT_EXTRACTION / 75%`
- 未执行 commit、push、reset 或清理 `.real-diagnostic-temp/`

## 执行环境

- Docker Worker 已停止；宿主机 Miniconda Python Worker 运行 `python -m app.worker`。
- 宿主机 Worker 连接 Docker PostgreSQL 映射端口 `127.0.0.1:15432`。
- 脱敏文件由宿主机只读 HTTP 服务 `127.0.0.1:18081` 提供。
- 宿主机临时 Worker 设置 `ALLOW_HTTP_DOWNLOADS=true`、下载白名单为 `127.0.0.1`、`DOCX_PAGE_LOCATION_ENABLED=true`、OCR 重试为 0、任务最大尝试为 1。
- 临时 HTTP 服务和宿主机 Worker 已在任务失败后关闭；Docker Worker 已恢复运行。

## 页码解析结果

- 目标合同、模板和项目方案确认函均完成下载并进入解析流程。
- 目标合同外部页码结果沿用受控探针：`46/46` 页、1666 个可靠映射、覆盖率 `0.563218`。
- 任务在页码补全之后才进入 `FACT_EXTRACTION`，本轮没有页码映射错误，也没有引入估算页码。

## 正式任务结果

- 下载阶段已通过，证明宿主机文件服务、宿主机 Worker 和 Docker PostgreSQL 链路正常。
- 任务进入 `FACT_EXTRACTION / 75%` 后失败。
- 首个明确错误：代码 `DYNAMIC_CHECK_INCOMPLETE`，安全消息为“最小事实分片仍未可靠完成”。
- 任务未进入正式结果持久化、公开证据补全或控制台报告展示；未调用 retry，也未创建第四个页码验收任务。
- 39 项差异、4 项通过、页码展示、局部高亮和建议覆盖率均未验证，未宣称真实闭环完成。

## 配置恢复

- API/Worker 已恢复为 Docker 常驻配置。
- API/Worker 实际下载白名单均恢复为 `.env` 中的甲方正式文件域名，未保留 `fixture-server` 或 `127.0.0.1`。
- `ALLOW_HTTP_DOWNLOADS=true`、`DOCX_PAGE_LOCATION_ENABLED=true` 保持生效。
- `/ready` 正常，数据库、OCR 和 LLM 配置均可用。

## 离线验证依据

本轮未修改代码、未重跑测试；沿用已确认的 Compose PostgreSQL 全量测试 `346 passed`，前后端构建、变更文件 Ruff、compileall 和 `git diff --check` 均通过。

## 后续边界

- 页码和映射算法不再调整；本轮阻塞已从 OCR 网络层推进到事实抽取可靠性门。
- 当前失败任务不得 retry；如需继续，需要先定位 `DYNAMIC_CHECK_INCOMPLETE` 的首个安全子错误，并由用户明确授权创建新的唯一任务。
- 在正式任务成功并完成控制台验收前，不提交页码功能里程碑。
