# 任务进度：控制台报告主流程与原型化展示

## 基本信息

- 时间：2026-08-21 16:25:41 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 开发起点提交：`b0a1f55`
- 工作树状态：提交前 dirty；仅包含本次前端控制台、报告组件、README 和本记录修改

## 用户目标

将起草检查和放款比对报告纳入控制台并作为默认业务结果入口，按甲方原型的信息结构和视觉语言重构两个数据驱动报告页，同时优化统一任务中心的筛选、分页及报告/调试双入口；本阶段只考虑桌面 Web，不处理 iframe、CSP 或移动端。

## 本次完成

- 移除报告路由的嵌入壳隔离，保留原有两个 URL 并统一显示控制台导航。
- 创建起草或放款任务后直接进入对应业务报告；调试详情重试后也进入新任务报告。
- 报告页支持任务进度轮询、成功后原位展示、失败重试、请求重新加载和任务类型不匹配错误。
- 任务中心接入既有服务端分页及任务类型、状态、业务关联 ID、创建时间筛选。
- 任务列表使用中文状态、结论、风险/复核统计，并提供“查看报告”主入口和“调试详情”次入口。
- 按甲方原型复用渐变标题、状态条、统计卡、文件卡、模块卡、风险/复核/通过配色和前后差异展示。
- 起草与放款页面分别编排，但按 `module_code` 动态分组；未知模块使用稳定通用标题。
- 风险和复核项按 `related_diff_ids` 展示差异证据；无关联风险的差异保留在“其他差异证据”。
- 无关联 diff 的规则/来源证据通过安全结构化组件展示，不显示完整文件 URL。
- 修正前端 `TaskDetail` 错误继承 `TaskSummary` 的类型定义；任务列表 API Client 支持查询参数编码。
- 更新 README，将“嵌入页”调整为控制台业务报告页并说明默认流程。

## 修改文件

- `frontend/src/views/TaskListView.vue`：统一任务中心、筛选、分页和双入口。
- `frontend/src/views/reports/*`、`frontend/src/components/report/*`：两个报告页、证据关联和原型化共享组件。
- `frontend/src/composables/useTaskReport.ts`、`frontend/src/utils/routes.ts`：轮询、失败重试、重新加载和报告路径选择。
- `frontend/src/api/*`、`frontend/src/router.ts`、`frontend/src/App.vue`：查询参数、类型修正、控制台路由与导航。
- `frontend/src/style.css`：控制台及报告共享桌面设计变量和布局。
- `README.md`：页面入口和当前能力说明。

## 接口、数据和配置变化

- API：后端公开路由和响应 Schema 无变化；前端 `api.list` 新增既有查询参数的调用能力。
- 数据库/迁移：无变化，未执行迁移。
- 配置：无变化；没有修改 Docker、Compose、依赖或环境变量。
- 兼容性：保留 `/reports/draft/:taskId` 和 `/reports/final/:taskId`；Schema 1.0 继续通过现有后端兼容层显示历史统计。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `npm run typecheck` | 通过 | `vue-tsc --noEmit` 无类型错误 |
| 首次 `npm run build` | 失败并修复 | `DiffEvidence.vue` 缺少 `</style>`，生产编译捕获 |
| 第二次 `npm run build` | 失败并修复 | `CheckModule.vue` 缺少 `</style>`，生产编译捕获 |
| 最终 `npm run typecheck` | 通过 | 修复后再次通过 |
| 最终 `npm run build` | 通过 | 1499 modules；JS 约 1,019.44 kB，仅既有大 chunk warning |
| Vite 控制台入口 | 通过 | `http://127.0.0.1:5173/console/` 返回 HTTP 200 |
| 任务筛选/分页代理联调 | 通过 | `FINAL_COMPARE + SUCCEEDED + page_size=2` 返回 2 项，总数 13 |
| Schema 2.0 起草真实结果 | 通过 | 任务 `tsk_01M0H6ZJGSWT1H2W0H20R5RXGT` 可读取，risk=0、review=0 |
| Schema 1.0 放款历史结果 | 通过 | 任务 `tsk_01M0F7EP40AEJNRG7CJNET0BS5` 可兼容读取，历史统计 review=3，diff=3 |
| 报告敏感/越界文案扫描 | 通过 | 报告组件未出现 `safe_url`、原始 JSON、印章、风险等级或 embedded 标记 |
| `git diff --check` | 通过 | 无空白错误；仅 Windows LF/CRLF 提示 |
| 桌面浏览器视觉验收 | 待人工 | 浏览器运行环境返回无可用浏览器；未用 HTTP 200 冒充视觉通过 |
| 后端 pytest / Docker build / OCR / LLM | 未执行 | 本次未改后端、容器、依赖或外部协议，按分层测试规范不重复 |

## Docker 与运行状态

- API：`contract-review-agent:dev`，healthy，`127.0.0.1:8000`。
- Worker：运行中。
- PostgreSQL：`postgres:16.10-alpine`，healthy。
- 控制台：临时 Vite 服务用于联调后已停止；常驻 API 镜像未重建，因此新前端在下次常规镜像构建后进入 `localhost:8000/console/`。
- 最终是否保持运行：API、Worker、PostgreSQL 保持原状态；未重启或停止。

## 重要决策

- 业务报告作为创建任务和任务列表的默认结果入口，调试详情继续承担原始 JSON 与技术诊断。
- 任务中心复用既有列表接口，不新增报表中心、全局统计接口或数据库字段。
- 原型只作为视觉与信息结构依据；页面不模拟印章、事实矩阵或其他当前不存在的检查结果。
- 报告模块按真实结果动态分组，为后续事实矩阵复用当前页面，而不是新增第三套结果页。
- 本阶段只验收桌面 Web；移动端和 iframe 相关能力明确不在范围内。

## 已知问题与风险

- 1280px、1440px 的真实浏览器视觉验收尚未完成；需要在可用浏览器中人工检查排版、长文本和固定列。
- 前端生产包仍有既有约 1 MiB 大 chunk 提示，本次未引入路由懒加载重构。
- 常驻 Docker API 仍提供提交前镜像中的前端，需要在后续需要实际访问时执行常规镜像重建。
- 旧 Schema 1.0 结果只具备兼容统计和历史 diff，没有 Schema 2.0 的明确 `review_items`，因此页面通过“其他差异证据”保留展示。

## 人工视觉验收清单

1. 1440px 打开任务中心，检查筛选区、表格固定操作列、分页和双入口。
2. 打开起草报告 `#/reports/draft/tsk_01M0H07WR7ZT27589G0Q3WDNJV`，检查 26 项风险的模块和证据布局。
3. 打开通过报告 `#/reports/draft/tsk_01M0H6ZJGSWT1H2W0H20R5RXGT`，检查零风险状态和通过项。
4. 打开放款报告 `#/reports/final/tsk_01M0F7EP40AEJNRG7CJNET0BS5`，检查历史统计和 3 项 OCR 差异证据。
5. 打开失败任务 `#/reports/draft/tsk_01M0GYKA7YTMBS1FQZDM3ECDSV`，检查错误与重试按钮。
6. 使用错误类型路由打开任一任务，检查明确的任务类型不匹配提示。
7. 在 1280px 再检查任务中心和长合同文本，不要求 375px 或 iframe 验收。

## 下一步建议

1. 在可用浏览器环境完成上述桌面视觉清单并定点修正。
2. 使用真实黄金样本标注 26 个差异和 3 个扩展表格点，收敛 DRAFT_REVIEW 规则。
3. 规则收敛后实现 OpenAI 兼容 LLM Client 的离线测试和单文档事实抽取。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260821-162541_console-report-workflow.md`
- `frontend/src/views/TaskListView.vue`
- `frontend/src/views/reports/DraftReportView.vue`
- `frontend/src/views/reports/FinalReportView.vue`

## 交接摘要

控制台已改为业务报告优先，创建、列表和重试均进入对应报告。
任务中心已支持服务端筛选、分页、中文状态及报告/调试双入口。
两个报告页已按原型重构，并按真实 module_code 动态分组风险、复核和通过项。
风险、复核、diff 和无 diff 来源证据均可追溯展示，不显示 URL 或原始 JSON。
最终 typecheck 和生产构建通过，真实新旧结果接口联调通过。
浏览器不可用，桌面视觉验收保持待人工；清单和任务 URL 已记录。
后端、数据库、Docker 和外部 OCR/LLM 未改动或重复验收。
