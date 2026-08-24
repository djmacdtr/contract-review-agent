# 任务进度：正式报告与二元检查结果改造

## 基本信息

- 时间：2026-08-24 16:18:23 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / FIX / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`c4fd6aa`（开始本轮前按用户授权提交既有已验证工作区，提交信息为 `feat: complete dynamic multi-document review baseline`）
- 工作树状态：dirty；本轮后端、前端、测试、README 和本进度记录均未提交，未覆盖、回退或清理基线内容

## 用户目标

将两个正式报告改为共享四分类 Tab 和纯业务展示；新任务采用成功后二元结果，检查不完整直接失败；动态生成可信通过项；统一业务位置和字符级差异；为每项风险批量生成针对性 AI 建议，并保留历史 review/warning Schema 兼容。

## 本次完成

- FINAL_COMPARE 升至 `0.5.0 / 0.5.0`，DRAFT_REVIEW 升至 `0.6.0 / 0.5.0`。新任务成功结果固定 `review_items=[]`、`review_count=0`，结论只为 `RISK_FOUND` 或 `PASS`。
- 解析/OCR 逐页覆盖不足、文档配对或对齐不可靠、起草扩展表格无法可靠检查，以及已启用的事实抽取、独立评审、跨文件映射或数值规则未完成时抛出 `WorkflowError`，不生成正式结果。
- OCR 单字符、占位符、阅读顺序和低置信差异在比较成功后不再转为 review item，而是进入正式风险；历史 review 构造器、字段和枚举继续保留。
- 新增动态通过项汇总：只对实际存在且可靠完成的全文、日期、期限、比例/利率、金额和兼容表格检查生成通过记录；关闭能力、内容不存在、存在对应风险或对齐不可靠时不生成。
- 统一业务文本投影和 segments：过滤 `<br>` 变体、普通换行差异、零宽字符、软连字符及解析器展示标记；字符级片段与两侧展示文本保持对应，模板表格差异也使用同一生成器。
- `RiskItem` 新增可选 `analysis_advice`，Schema 保持 2.1。两个工作流在 Advice 阶段各使用一次批量调用；严格校验 JSON、当前任务 risk ID、重复 ID、重复建议和技术标识，缺失或失败时使用包含实际差异、文件名和业务位置的确定性建议，不改变结论。
- 两个独立正式报告路由改用纯报告外壳，共享四个带数量的 Tab：检出风险、删除/缺失、新增/变更、校验通过；移除正式页图例、统计卡、review/warning、调试入口、任务/业务 ID 和其他技术信息。
- DiffEvidence 使用 Vue 文本插值逐 span 渲染，不使用 `v-html`；只高亮 DELETE/INSERT，EQUAL 保持普通显示，长文本保留换行并自动换行。
- DiffEvidence、SourceEvidence 通过 `result.files` 映射文件名；页码保持解析器 1 基值，段落、表格、行、列由内部 0 基转为业务 1 基，多位置去重并用业务列表展示。
- 更新 Mock/fixture、README、集成断言和验收脚本默认工作流版本；未加入固定文件名、样本坐标、29 项候选或固定业务字段生产特例。

## 修改文件

- `app/comparison/reliable.py`、`app/draft_review/template_checks.py`：统一比较/展示投影和字符级 segments。
- `app/results/advice.py`、`app/results/passed_checks.py`、`app/results/risk_model.py`：风险建议、动态通过项和二元风险分类。
- `app/workflows/draft_review.py`、`app/workflows/final_compare.py`、`app/workflows/mock_graphs.py`：失败门、批量 Advice、二元结果和 Mock 兼容。
- `app/adapters/document_parser/textin_mapper.py`、`app/adapters/llm/`、`app/schemas/results.py`：逐页 OCR 完整性、严格 Advice Schema 和新增结果字段。
- `frontend/src/views/reports/`、`frontend/src/components/report/`、`frontend/src/utils/reportEvidence.ts`、`frontend/src/App.vue`、`frontend/src/router.ts`：纯报告外壳、共享四 Tab、业务位置、字符级高亮和业务文案。
- `tests/unit/`、`tests/integration/test_worker.py`、`scripts/e2e_smoke.py`、`scripts/e2e_ocr_acceptance.py`、`README.md`：定向回归、版本断言和当前行为说明。

## 接口、数据和配置变化

- API：结果中的 `RiskItem.analysis_advice?: string` 是唯一新增公开字段；请求、文件角色和 URL 路由不变。
- 数据库/迁移：无数据库模型或 Alembic 变化。
- 配置：无配置项变化。
- 兼容性：`schema_version=2.1` 不变；`review_items`、`review_count`、`warnings`、`REVIEW_REQUIRED` 和旧任务读取继续保留。旧结果缺少 `analysis_advice` 时前端省略建议区域。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 18 个直接相关 unit pytest 文件 | 通过 | `148 passed, 1 warning`；warning 为既有 LangGraph pending deprecation |
| 变更范围 `ruff check` | 通过 | `All checks passed!` |
| `.venv\Scripts\python.exe -m compileall -q app scripts tests/unit tests/integration/test_worker.py` | 通过 | 无错误输出 |
| `npm run typecheck` | 通过 | Vue/TypeScript 无错误 |
| `npm run build` | 通过 | Vite 生产构建成功；仅既有大 chunk warning |
| `git diff --check` | 通过 | 无空白错误；仅 Windows CRLF 转换提示 |
| 正式报告静态文案与 `v-html` 扫描 | 通过 | 未发现人工复核、任务/业务 ID、调试入口、技术位置箭头/计数或 `v-html` 展示 |
| 生产特例关键词扫描 | 通过 | 未发现真实文件名、fixture file ID 或 29 项候选生产分支；`29` 命中仅为 HTTP 429 状态码 |
| 一次扩大到全 `app/scripts/tests` 的 Ruff 尝试 | 未通过 | 发现 18 个本轮未修改文件中的既有 E501；未扩散修改，随后变更范围 Ruff 通过 |

## Docker 与运行状态

- API / Worker / PostgreSQL / 控制台运行状态：本轮未检查、未启动、未停止或重启。
- 未执行 Docker build、Compose、Alembic 或数据库操作。
- 未调用真实 OCR 或 LLM；Advice 使用 Mock/fixture，符合日常开发门控。
- 前端视觉和交互验收由用户人工负责，本轮未执行浏览器或截图验收。

## 重要决策

- 检查是否完成与检查结果是否有风险分开：不完整是任务失败，不再输出面向业务的第三类复核结果。
- Advice 是补充能力，任何模型、结构或内容校验失败都只记录后台 warning 并保留确定性建议。
- 动态通过项按本次实际内容和启用能力生成，各检查范围独立；不以总风险数为零伪造通过项。
- 技术字段继续存在于结果和独立调试页，但正式嵌入页不展示。

## 已知问题与风险

- 未执行全仓 pytest、PostgreSQL 集成测试、Docker/Compose、Alembic、真实 OCR/LLM 或完整五文件验收；需在最终交付收口执行。
- 前端 bundle 仍有超过 500 KiB 的既有 Vite warning。
- 浏览器视觉、响应式布局和实际甲方 iframe 交互由用户人工验收。

## 下一步建议

1. 用户人工验收两个独立报告页的四 Tab、风险卡、字符级高亮、业务位置和历史任务兼容展示。
2. 交付收口时运行全仓 pytest、PostgreSQL 集成、Docker build/Compose 冒烟和 Alembic 检查。
3. 获得授权后只运行一次代表性真实 OCR/LLM 任务，重点抽查逐页覆盖、动态通过项和批量建议质量。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260821_ungraded-risk-and-embedded-pages.md`
- `docs/plans/20260824_dynamic-document-and-numeric-analysis-principles.md`
- `docs/progress/20260824-161823_formal-report-binary-results.md`
- `app/workflows/draft_review.py`
- `app/workflows/final_compare.py`
- `frontend/src/components/report/ReportResultTabs.vue`

## 交接摘要

新任务已改为成功后二元分类，检查不完整直接失败，历史 review/warning 契约保留。
两个正式报告页保持独立并共享四分类 Tab，不再展示人工复核或技术诊断。
比较层过滤换行/`<br>`/零宽噪声并生成可对应两侧文本的字符级 segments。
风险级建议使用单次批量 Advice、严格 risk ID 校验和确定性降级。
相关后端测试 148 项、变更范围 Ruff/compileall、前端 typecheck/build 均通过。
全仓/Docker/数据库/真实外部/视觉验收尚未执行，工作树保持 dirty，未 commit、push。
