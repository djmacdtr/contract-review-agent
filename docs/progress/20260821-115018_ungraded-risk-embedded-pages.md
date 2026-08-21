# 任务进度：无等级风险 Schema 2.0 与双嵌入页面

## 基本信息

- 时间：2026-08-21 11:50:18 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`fd1bdb4`
- 工作树状态：dirty；包含并行会话遗留的阶段 B、LLM DTO、计划/进度文件以及本次 Schema 2.0、迁移和前端页面改动，均未自动提交或覆盖

## 用户目标

按照无等级风险与双页面计划，把公共结果契约升级为 Schema 2.0，将风险语义统一为“风险 / 人工复核 / 通过”，增加兼容数据库迁移，并实现起草检查和放款比对两个独立、只读的嵌入式报告页面。

## 本次完成

- 将结果契约升级为 Schema 2.0，移除 `DiffItem`、`RuleCheck` 和比较引擎中的 `severity`。
- 新增明确的 `RiskItem`、`ReviewItem`、`PassedCheck` 和无等级统计结构。
- 实现旧 Schema 1.0 结果的动态兼容读取：旧等级统计映射到 risk/review，并标记 `legacy_statistics=true`。
- 新增 `0002_ungraded_risk_counts` 迁移；新增 `risk_count`、`review_count`，保留旧等级列作为 deprecated，且新任务只写新列。
- FINAL_COMPARE 升级为 workflow/rules `0.4.2`；DRAFT_REVIEW 升级为 `0.3.1`；两个真实工作流均输出 Schema 2.0。
- 新增统一风险映射层，将确认差异/规则失败映射为风险，将 OCR 低置信和解析 warning 映射为人工复核。
- 更新任务列表、调试详情、OpenAPI、TypeScript 类型和中文标签，移除等级展示。
- 新增 `/console/#/reports/draft/:taskId` 和 `/console/#/reports/final/:taskId` 两个独立嵌入页。
- 嵌入页共用报告组件，但独立编排；不显示控制台导航、任务创建、重试、原始 JSON、文件 URL、阶段 Tab 或印章模块。
- 更新 README、`.env.example` 和离线验收脚本；本次没有调用真实 OCR 或 LLM。

## 修改文件

- `alembic/versions/0002_ungraded_risk_counts.py`：无等级任务统计迁移与旧数据回填。
- `app/schemas/results.py`、`app/schemas/tasks.py`：Schema 2.0、旧结果适配和任务列表统计。
- `app/results/risk_model.py`：风险、人工复核、通过项生成与统计。
- `app/comparison/*`：移除等级计算，保留差异证据和复核原因。
- `app/workflows/draft_review.py`、`app/workflows/final_compare.py`、`app/workflows/mock_graphs.py`：输出无等级结果。
- `app/db/models/entities.py`、`app/db/repositories/task_repository.py`、`app/services/task_service.py`、`app/worker/runner.py`：新字段写入、旧结果识别和结果版本持久化。
- `frontend/src/components/report/*`：嵌入式报告共享组件。
- `frontend/src/views/reports/*`、`frontend/src/composables/useTaskReport.ts`：两个报告页、轮询和任务类型校验。
- `frontend/src/router.ts`、`frontend/src/App.vue`、`frontend/src/api/types.ts`：嵌入路由、壳层隔离和 Schema 2.0 类型。
- `tests/unit/test_result_schema_v2.py`、`tests/unit/test_risk_model.py` 及既有工作流/API/Worker 测试：新旧契约、风险映射与回归覆盖。
- `README.md`、`.env.example`、`scripts/e2e_smoke.py`、`scripts/e2e_ocr_acceptance.py`：版本、运行说明和验收断言。

## 接口、数据和配置变化

- API：路由和请求字段不变；结果 `schema_version` 升至 `2.0`，新增 `review_items`、`passed_checks`，统计改为 risk/review/pass；任务列表新增 `risk_count`、`review_count`、`legacy_statistics`。
- 数据库/迁移：当前 head 为 `0002_ungraded_risk_counts`；新增两列，旧四个等级列保留，不做不可逆删除。
- 配置：`RESULT_SCHEMA_VERSION` 默认值与 `.env.example` 改为 `2.0`；工作流使用代码常量，避免本机旧 `.env` 把新结果降回 1.0。
- 兼容性：旧任务原 JSON 保持 `schema_version=1.0`，读取时由响应模型补出新统计和缺省集合；新任务只产生 2.0。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| Schema 2.0/旧结果定向测试 | 通过 | 新旧结果均可校验 |
| 比较、工作流、模板定向测试 | 通过 | `53 passed, 1 warning` |
| 宿主机全量单元测试 | 通过 | `109 passed, 1 warning` |
| Docker PostgreSQL 全量测试 | 通过 | `127 passed, 1 warning in 7.28s` |
| 变更范围 Ruff | 通过 | 所有新增/修改 Python 文件通过；全仓仍有 43 个历史格式问题，未扩散修改范围 |
| Vue TypeScript 类型检查 | 通过 | 无类型错误 |
| Vue 生产构建 | 通过 | 1494 modules；仅既有大 chunk warning |
| `docker compose config --quiet` | 通过 | Compose 配置有效 |
| Docker runtime/test 镜像构建 | 通过 | `contract-review-agent:dev`、`:test` |
| 空库迁移与旧统计回填 | 通过 | 空库 0001 → 0002；旧值 2/3/4/5 回填为 risk=5、review=9 |
| 常驻数据库 `alembic upgrade head` | 通过 | 当前 revision `0002_ungraded_risk_counts` |
| `alembic check` | 通过 | `No new upgrade operations detected` |
| OpenAPI 检查 | 通过 | DiffItem 无 severity；新统计六字段齐全 |
| 历史任务兼容读取 | 通过 | `tsk_01M0H07WR7ZT27589G0Q3WDNJV` 可读取，统计标记 legacy |
| 合成 DRAFT API → Worker 闭环 | 通过 | `tsk_01M0H6ZJGSWT1H2W0H20R5RXGT`，SUCCEEDED/COMPLETED/100，Schema 2.0，risk=0、review=0、passed=2 |
| API 重建后持久化 | 通过 | 上述新任务在恢复常驻配置并重建 API/Worker 后仍可查询 |
| `/health`、`/ready`、`/docs`、`/console/` | 通过 | 均返回正常，docs/console 为 HTTP 200 |
| 嵌入页浏览器视觉验收 | 待人工 | 浏览器控制环境没有可用浏览器；未用 HTTP 200 冒充视觉通过 |
| `git diff --check` | 通过 | 无空白错误；仅 Windows LF/CRLF 提示 |

唯一测试 warning 为 LangGraph 上游未来弃用提示，不影响当前运行。

## Docker 与运行状态

- API：`contract-review-agent:dev`，healthy，`127.0.0.1:8000`。
- Worker：`contract-review-agent:dev`，运行中。
- PostgreSQL：`postgres:16.10-alpine`，healthy。
- 命名卷：`contract-review-postgres-data` 存在，未删除。
- 控制台：`http://localhost:8000/console/` 可访问。
- 最终是否保持运行：是，API、Worker、PostgreSQL 均保持运行。
- 合成闭环临时使用 `api` 下载白名单，任务结束后已重建服务并恢复原有本机配置。

## 重要决策

- Schema 2.0 是公共结果的破坏性版本升级，但不改变 API 路由和请求结构。
- 技术差异不自动等于业务风险；确认差异才进入 risk，解析/OCR 不确定性进入 review。
- 旧数据库等级列至少保留一个发布周期，新任务不再写入。
- 两个嵌入页共享组件但不使用单个巨型条件页面；任务类型不匹配时明确报错。
- 正式嵌入页保持只读和最小暴露，不展示开发诊断信息或文件地址。

## 已知问题与风险

- 当前工作树包含多个阶段和并行会话的未提交修改，收口提交时必须分组审查，不能整体盲目提交。
- 浏览器控制环境无可用浏览器，桌面和窄屏的视觉验收仍需人工完成。
- 甲方 iframe 父域名、目标尺寸、CSP `frame-ancestors` 和是否需要 `postMessage` 自适应高度尚未确认。
- 旧结果 metadata 内部可能保留历史等级诊断内容；响应的正式 risk/diff/rule 和嵌入页均不再展示等级，但旧原始 JSON 不做数据重写。
- 全仓 Ruff 仍有 43 个既有格式问题，均位于本阶段未修改的历史文件；变更范围已经通过。

## 人工视觉验收清单

1. 打开 `http://localhost:8000/console/#/reports/draft/tsk_01M0H6ZJGSWT1H2W0H20R5RXGT`，确认没有控制台导航、Tab、创建/重试入口或原始 JSON。
2. 打开 `http://localhost:8000/console/#/reports/draft/tsk_01M0H07WR7ZT27589G0Q3WDNJV`，确认旧 Schema 1.0 任务可展示且标识历史统计。
3. 打开 `http://localhost:8000/console/#/reports/final/tsk_01M0F7EP40AEJNRG7CJNET0BS5`，确认放款页独立展示差异证据和复核项。
4. 把窗口缩到 375px 宽，确认统计卡、文件卡、差异左右栏和长文本不横向溢出。
5. 用 DRAFT 路由打开 FINAL task、用 FINAL 路由打开 DRAFT task，确认显示明确任务类型不匹配错误。
6. 确认两个正式页面均不出现 HIGH/MEDIUM/LOW/INFO 等级、印章模块或完整文件 URL。

## 下一步建议

1. 先对阶段 B 与本次 Schema 2.0 改动做分组审查并建立干净提交检查点。
2. 使用真实黄金样本把现有 26 个差异和 3 个扩展表格复核点标注为“风险 / 允许填写 / 对齐误报 / 人工复核”，收敛 DRAFT 规则。
3. 让所有 review item 拥有非空、稳定原因码，并增加黄金集回归断言。
4. 人工完成两个嵌入页桌面、375px 和甲方 iframe 目标尺寸视觉验收。
5. 页面契约稳定后，按既有计划进入 LLM 单文档纵向切片；先离线 Client/DTO 测试，再做受限真实调用。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/README.md`
- `docs/plans/20260821_ungraded-risk-and-embedded-pages.md`
- `docs/progress/20260821-115018_ungraded-risk-embedded-pages.md`
- `docs/plans/20260821_draft-review-real-files-and-llm.md`
- `app/schemas/results.py`
- `app/results/risk_model.py`
- `frontend/src/views/reports/DraftReportView.vue`
- `frontend/src/views/reports/FinalReportView.vue`

## 交接摘要

Schema 2.0、无等级风险语义和 0002 兼容迁移已实现并通过全量测试。
新旧结果均可读取；新任务只写 risk/review 统计，旧等级列保留。
起草与放款已有两个独立只读嵌入路由，共享组件但独立编排。
Docker 全量测试 127 passed；常驻服务已升级并保持运行。
合成 DRAFT 新闭环成功，API 重建后任务仍存在。
本轮未调用真实 OCR/LLM，也未修改或删除数据库卷。
浏览器视觉验收因无可用浏览器保持待人工，清单和 URL 已记录。
下一步应先收敛黄金样本规则，再进入 LLM 单文档切片。
