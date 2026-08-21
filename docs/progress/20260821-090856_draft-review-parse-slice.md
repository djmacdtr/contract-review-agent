# 任务进度：DRAFT_REVIEW 真实多文档解析切片

## 基本信息

- 时间：2026-08-21 09:08:56 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`901cbb8`
- 工作树状态：dirty；本次 DRAFT_REVIEW 阶段 A 的代码、测试、前端、配置、README 和本文档均未提交

## 用户目标

按照 `docs/plans/20260820_draft-review-multidoc-llm.md` 开始下一阶段开发，优先完成阶段 A：取消调用方辅助资料类型依赖、使用可配置数量上限，并将 DRAFT_REVIEW 从 Mock 切换为目标/模板/1–N 辅助资料的真实受控下载和逐文件解析闭环。

## 本次完成

- 从现有规划提交创建 `feat/draft-review-multidoc` 分支，没有提交或推送。
- `reference_type` 保持短期兼容输入，但标记 deprecated 并在业务、数据库新任务和 OpenAPI 示例中忽略。
- `MAX_REFERENCE_FILES` 代码默认改为 20，Compose 显式透传，运行时超限返回 `INVALID_REQUEST` 和安全计数详情。
- 新增 DRAFT_REVIEW 真实 LangGraph：受控下载、逐份解析、文件级汇总、结果保存。
- DOCX 使用 `python-docx`；任意角色的 PDF 使用外部解析器 `auto`，未配置时以 `OCR_NOT_CONFIGURED` 安全失败，不回退 `pdfplumber` 生成正式结果。
- 结果使用 `mock=false / PARSER_ONLY / REVIEW_REQUIRED`，逐文件返回页数、解析器、warning、parser metadata、内容块/表格数和无正文的位置样例。
- 文件自动画像暂时明确返回 `UNKNOWN / NOT_RUN`，没有调用或冒充 LLM 分类。
- Worker 原子保存每份文件的 SHA-256、大小、MIME、页数、解析器和警告；临时目录由既有 TaskWorkspace 清理。
- 控制台删除辅助资料类型下拉，支持动态增加 URL，并展示真实解析模式、待识别文档类型、块/表格数和位置样例。
- 冒烟脚本改为在 API 容器临时生成并服务三份完全合成 DOCX，验证真实解析闭环；验证脚本会在 finally 恢复原下载配置。

## 修改文件

- `app/workflows/draft_review.py`：新增 DRAFT_REVIEW 真实下载/解析 LangGraph 和解析阶段结果。
- `app/workflows/router.py`：DRAFT_REVIEW 从 Mock 路由到真实执行器。
- `app/documents/router.py`：新增逐文件 DRAFT DOCX/PDF 解析计划。
- `app/schemas/files.py`、`app/schemas/requests.py`：弃用旧类型输入、更新示例和数量边界描述。
- `app/services/task_service.py`：运行时数量校验并忽略旧类型。
- `app/core/config.py`、`.env.example`、`compose.yaml`：默认和透传 `MAX_REFERENCE_FILES=20`。
- `frontend/src/views/DraftReviewView.vue`：删除类型下拉与旧 payload 组装。
- `frontend/src/views/TaskDetailView.vue`、`frontend/src/api/types.ts`、`frontend/src/utils/labels.ts`：展示解析阶段结果。
- `scripts/e2e_smoke.py`、`scripts/verify.ps1`：将 DRAFT 冒烟改为合成文件真实解析并安全恢复配置。
- `tests/unit/test_draft_review_workflow.py`、相关 unit/integration 测试：覆盖路由、真实 Graph、API 兼容、上限和 Worker 持久化。
- `README.md`：说明 DRAFT_REVIEW 0.2.0 能力、配置和边界。

## 接口、数据和配置变化

- API：路由不变；标准 DRAFT 请求不再包含 `reference_type`。旧字段暂时可传但 deprecated/ignored。辅助资料仍至少 1 份，Schema 安全硬上限 100，运行时由配置收紧。
- 数据库/迁移：无迁移；既有 nullable `task_file.reference_type` 保留，新建 DRAFT 文件不再写入调用方类型。
- 配置：`MAX_REFERENCE_FILES` 代码和示例默认 20，允许 1–100；Compose 支持 `.env`/进程覆盖。未覆盖本机真实 `.env`。
- 兼容性：创建、查询、重试路由和结果 schema 版本保持不变；结果文件只做向后兼容扩展。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 首轮定向 pytest | FAILED（预期） | 新真实 Workflow 模块尚不存在，形成 TDD 红灯 |
| DRAFT/Router/Core 定向 pytest | PASSED | 12 passed，1 个既有 LangGraph warning |
| 变更范围 `ruff check` | PASSED | All checks passed |
| Python `compileall` | PASSED | app/scripts/tests 无语法错误 |
| Vue `npm run typecheck` | PASSED | TypeScript 无错误 |
| Vue 首轮 `npm run build` | FAILED（已修复） | 捕获 v-for 局部变量不可直接作为 v-model |
| Vue 修复后 `npm run build` | PASSED | 1459 modules；仅既有大 chunk 提示 |
| `docker compose config --quiet` | PASSED | Compose 有效 |
| `docker compose --profile tools build test` | PASSED | 当前源码测试镜像构建成功 |
| Docker PostgreSQL 全量测试首轮 | FAILED（已修复） | 109 passed / 1 failed；测试误把 `.env` 覆盖值当作代码默认值 |
| Docker PostgreSQL 全量测试复跑 | PASSED | 110 passed，1 个 LangGraph 上游未来弃用 warning |
| `docker compose build api worker` | PASSED | 当前前后端运行镜像构建成功 |
| `docker compose run --rm api alembic check` | PASSED | No new upgrade operations detected |
| 合成 DOCX API→Worker→Result 冒烟 | PASSED | 目标、模板、辅助资料共 3 份；任务 `tsk_01M0GXRGK6YWZVWVXBC8M8PPRS` 成功 |
| OpenAPI 契约检查 | PASSED | 示例无 `reference_type`；兼容属性 `deprecated=true` |
| 恢复后端点检查 | PASSED | `/health`、`/ready`、`/docs`、`/console/` 均 HTTP 200 |
| 真实 OCR/LLM | 未执行 | 本轮未变更 Adapter 协议，按分层策略使用合成解析器覆盖 PDF 路由 |

## Docker 与运行状态

- API：`contract-review-api-1`，healthy，`127.0.0.1:8000`。
- Worker：`contract-review-worker-1`，running。
- PostgreSQL：`contract-review-postgres-1`，healthy，继续使用命名卷 `contract-review-postgres-data`。
- 控制台：`http://localhost:8000/console/`，HTTP 200，已构建最新页面。
- 最终是否保持运行：是；验证用临时 HTTP allowlist 已恢复为默认 Compose/.env 配置。

## 重要决策

- 阶段 A 只输出真实解析状态，不提前实现模板差异、事实矩阵或 LLM；用 `PARSER_ONLY` 明确能力边界。
- PDF 在 DRAFT 中统一使用外部解析器 `auto`，保证文本/扫描 PDF 输出模型一致；不使用本地 PDF 解析作为正式静默降级。
- `reference_type` 采用兼容接收但业务忽略，避免旧联调客户端立即 400，同时确保新任务不再依赖枚举。
- 文件画像在 LLM 阶段前只返回 UNKNOWN，不根据文件名猜测资料类型。

## 已知问题与风险

- 本机被忽略的 `.env` 仍可显式覆盖 `MAX_REFERENCE_FILES`；当前运行值不应与代码默认 20 混淆。
- 混合 DOCX/PDF 路由已由合成 external parser 测试覆盖，但本轮未消耗真实 OCR 调用；应在后续阶段验收集中验证一次。
- DRAFT_REVIEW 尚无模板确定性差异、空白/占位符检查、文档自动分类、事实抽取、跨文件矩阵或建议。
- `reference_type` 数据库列和枚举仍保留，正式接口冻结后可单独评估迁移清理。
- 前端仍有既有约 992 KiB 大 chunk 构建提示，不影响功能。

## 下一步建议

1. 阶段 B：复用 FINAL_COMPARE 的可靠 N:M 对齐，实现目标合同与模板的固定条款差异，以及占位符、疑似空白和空表检查。
2. 为模板正样本建立金额、日期、固定文字、占位符和表格必填项 100% 召回测试。
3. 阶段 C 再定义/实现 OpenAI 兼容 LLM Client 和 `DocumentProfile`、`FactCandidate` 严格 Schema；没有 Key 也先完成 Mock 和错误映射。
4. 在下一验收门使用一组脱敏混合 DOCX/PDF 任务验证外部解析，不在日常规则迭代中重复真实 OCR。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260820_draft-review-multidoc-llm.md`
- `docs/progress/20260821-090856_draft-review-parse-slice.md`
- `app/workflows/draft_review.py`
- `app/comparison/engine.py`

## 交接摘要

DRAFT_REVIEW 已从 Mock 切到真实受控下载和逐文件解析。
新请求不需要辅助资料类型，旧字段暂时兼容但完全忽略。
辅助资料默认上限 20，可配置且超限明确失败。
DOCX 本地解析，PDF external auto；未配置外部解析时安全失败。
结果明确为 PARSER_ONLY，不生成模拟风险或 LLM 事实。
Docker 全量 110 项测试和合成三文件真实闭环通过。
默认 Compose API、Worker、PostgreSQL 保持健康运行。
下一步进入模板确定性比对与占位符/空白检查。
