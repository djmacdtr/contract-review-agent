# 任务进度：合同真实缺页与 OCR 漏页分层改造

## 基本信息

- 时间：2026-08-24 16:59:10 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / FIX / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；本轮比较、工作流、报告、测试、配置和本文档修改均未提交，开始时工作区为 clean

## 用户目标

区分“原始文件真实缺页”和“OCR 服务漏解析”：OCR 不完整继续令任务失败；已完整解析文件中的可靠连续缺失应聚合为页面或内容块风险，并在两个正式报告中使用业务位置和针对性建议展示。

## 本次完成

- 保留 TextIn Mapper 的总页数、有效页数、逐页状态和逐页内容完整性失败门；仅补充可靠物理页码元数据，没有放宽 `OCR_PARTIAL_FAILURE`。
- 共享比较层新增 `PAGE_MISSING`、`CONTENT_BLOCK_MISSING`、`certainty` 和 `missing_detail`，每个连续缺口聚合为一个差异。
- PDF↔PDF 完整连续页面缺失生成 `PAGE_MISSING + CONFIRMED`；单侧无物理页码时使用分页侧每页有效字符中位数估算，满足门槛后生成 `PAGE_MISSING + INFERRED`。
- 未达到页级证明但包含多个可靠连续结构单元时生成 `CONTENT_BLOCK_MISSING + CONFIRMED`；单个普通短删除仍为 `DELETED`，单个达到页当量的超长正文块可豁免结构单元数量。
- 文档开头和末尾作为结构边界参与判断；中部页级推断要求分页侧前后锚点落在相邻物理页，避免把同页长条款删除误判为缺页。
- 对连续删除做全文移动回查；能在当前文件其他位置找到的内容保留普通删除/新增差异，不标记缺失。
- 可靠缺失从未解释的基准侧覆盖缺口中扣除并计算有效覆盖率；缺失规模不再单独使任务失败，OCR/解析、文件相关性和剩余内容对齐仍使用安全失败门。
- 起草检查和放款比对均接入三个配置阈值；工作流/规则版本分别更新为 `DRAFT_REVIEW 0.7.0 / rules 0.6.0`、`FINAL_COMPARE 0.6.0 / rules 0.6.0`。
- 两种新增差异统一映射为 `DELETION_OR_MISSING`，不会生成复核项；确定性和批量模型 Advice 输入均包含缺失分类、确定性、文件、位置及摘要。
- 正式报告按文件名显示物理页范围、DOCX 段落范围、PDF 相邻页缺口及文档首尾位置，并只展示缺失内容摘要，避免几十个段落删除卡片。

## 修改文件

- `app/comparison/`：公开差异模型、缺失聚合、页当量估算、移动回查和有效覆盖率。
- `app/core/config.py`、`.env.example`：三个缺页识别配置项。
- `app/documents/parsers.py`、`app/adapters/document_parser/textin_mapper.py`：明确 PDF/OCR 物理页码可靠性元数据。
- `app/draft_review/template_checks.py`、`app/workflows/`：两个工作流接线和版本更新。
- `app/results/`：删除/缺失风险映射、确定性缺页建议及 Advice 载荷。
- `frontend/src/api/types.ts`、`frontend/src/components/report/DiffEvidence.vue`、`frontend/src/utils/`：接口类型、缺失摘要和业务位置展示。
- `tests/`、`scripts/`、`README.md`：定向回归、Schema 断言、版本和配置说明。

## 接口、数据和配置变化

- API：`DiffItem.diff_type` 新增 `PAGE_MISSING`、`CONTENT_BLOCK_MISSING`；新增可选 `certainty`、`missing_detail`。`ComparisonDiagnostics` 增加有效基准覆盖率和已解释缺失单元数。
- Schema：保持 `2.1`；新增字段均为可选，Schema 2.0/2.1 历史结果继续读取。
- 数据库/迁移：无数据库模型或 Alembic 变化。
- 配置：新增 `PAGE_MISSING_MIN_EQUIVALENT=0.8`、`PAGE_MISSING_MIN_ANCHOR_SIMILARITY=0.85`、`PAGE_MISSING_MIN_STRUCTURE_UNITS=2`；无最大缺失规模失败配置。
- 请求、文件角色、URL、OCR Adapter 协议和 LLM Schema：不变。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 12 个直接相关 unit pytest 文件 | 通过 | `111 passed, 1 warning`；warning 为既有 LangGraph pending deprecation |
| 变更范围 `ruff check` | 通过 | `All checks passed!` |
| `python -m compileall -q app scripts tests/unit ...` | 通过 | 无错误输出 |
| `npm run typecheck` | 通过 | Vue/TypeScript 无错误 |
| `npm run build` | 通过 | Vite 生产构建成功；仅既有大 chunk warning |
| FastAPI 本地 `openapi()` Schema 断言 | 通过 | 两个 diff type 及 `certainty`、`missing_detail` 均存在 |
| `git diff --check` | 通过 | 无空白错误；仅 Windows CRLF 转换提示 |
| PostgreSQL API 集成用例尝试 | 未执行到测试体 | fixture 连接 `postgres` 主机名失败；按快速模式未启动 Compose，随后使用本地 OpenAPI 生成完成 Schema 校验 |

## Docker 与运行状态

- API / Worker / PostgreSQL / 控制台：本轮未检查、未启动、未停止或重启。
- 未执行 Docker build、Compose、Alembic 或数据库操作。
- 未调用真实 OCR 或 LLM；全部使用 fixture、Mock 和程序化文档。
- 前端视觉与交互验收由用户负责，本轮未执行浏览器或截图验收。

## 重要决策

- 缺失规模只区分页面缺失和内容块缺失，不作为任务失败门。
- `certainty` 描述判定依据，不作为风险等级；所有可靠缺失仍是同一无等级风险类别。
- 只有双方可靠物理分页且完整页面缺失时使用 `CONFIRMED PAGE_MISSING`；DOCX↔PDF 页级判断使用 `INFERRED`。
- 大范围可靠缺失通过有效覆盖率保留报告能力；未解释的混乱差异仍不能借缺页逻辑绕过对齐失败门。

## 已知问题与风险

- 未执行全仓 pytest、数据库集成、Docker/Compose、Alembic、真实 OCR/LLM 或真实大合同缺页验收。
- 页当量是基于有效正文字符中位数的通用估算，仍需用不同版式、表格密度和扫描质量的真实合同评估误报率。
- 前端 bundle 继续存在超过 500 KiB 的既有 Vite warning。

## 下一步建议

1. 使用一组脱敏 PDF↔PDF 和 DOCX↔扫描 PDF 文件人工验收中部、首页、末页和多处缺页文案。
2. 交付收口时运行 PostgreSQL 集成、全仓 pytest、Docker build/Compose 冒烟和 Alembic 检查。
3. 根据真实样本评估三个配置阈值，优先关注同页长条款、超长表格和重复页眉正文。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260824-165910_missing-page-classification.md`
- `app/comparison/reliable.py`
- `app/comparison/models.py`
- `frontend/src/components/report/DiffEvidence.vue`

## 交接摘要

OCR 服务漏页仍由 Mapper 严格失败，不会冒充合同缺页。
两个工作流已共享真实缺页和连续内容块聚合，风险统一进入删除/缺失分类。
可靠大范围缺失可继续生成正式报告，无法解释的对齐不足仍失败。
相关后端测试 111 项、Ruff、compileall、前端 typecheck/build 和本地 OpenAPI 校验通过。
数据库集成、全仓/Docker、真实 OCR/LLM 和视觉验收尚未执行。
