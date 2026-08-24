# 任务进度：动态事实与数值能力收口

## 基本信息

- 时间：2026-08-24 09:19:05 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / FIX / TEST / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保留前序 LLM、事实矩阵、Schema 2.1、前端、测试和文档修改，本轮仅增量收口，未清理、回退或提交

## 用户目标

将 DRAFT_REVIEW 收口为目标合同中心的动态多文档事实与数值检查，补齐跨文件语义映射、双模型共识、Decimal/白名单 AST、MISSING/UNCERTAIN 安全语义和完整本地/Docker 验收；外部服务只做无正文/合成最小探测。

## 本次完成

- 将事实矩阵改为目标合同事实实例中心；辅助资料独有事实不再形成目标合同风险。
- 增加逐辅助资料的主模型语义映射和独立模型映射评审协议；映射按证据、独立模型和 `0.85` 共识门决定是否可影响风险或通过。
- 固化逐来源及聚合 `CONSISTENT / CONFLICT / MISSING / UNCERTAIN` 语义；`MISSING` 等同 `NOT_MENTIONED`，默认不产生风险或复核，只有共识缺失计划可要求人工复核。
- 增加金额币种、百分比/利率/基点、期限、数量和日期的程序规范化；币种、单位或口径不可比时降级 `UNCERTAIN`。
- 修复分块合并丢失 `semantic_concepts`、`validation_specs`；合并证据位置并拒绝同 ID 的冲突定义。
- 数值规则改为唯一事实输入，重复不一致输入、缺失、除零、未知 AST 或主/评审公式不一致均进入人工复核；失败风险和复核项携带已有事实证据。
- OpenAI 兼容 Client 增加映射/评审接口，并在每次结构化调用中显式提供 JSON Schema。
- DRAFT workflow/rules 升至 `0.5.1 / 0.4.1`；Schema 保持 `2.1` 且兼容旧结果；前端增加目标值和逐来源状态的非视觉展示。
- Compose 测试和合成冒烟显式关闭真实 LLM/OCR，避免完整验收意外调用外部服务；修正 API 元数据和冒烟版本断言。

## 修改文件

- `app/draft_review/facts.py`、`numeric_rules.py`：目标中心矩阵、证据合并、规范化比较和安全公式执行。
- `app/adapters/llm/`、`app/workflows/draft_review.py`：结构化 Schema、跨资料映射/评审和共识工作流。
- `app/schemas/results.py`、前端报告类型/组件：Schema 2.1 逐来源比较结果和非视觉展示。
- `compose.yaml`、`scripts/verify.ps1`、`scripts/e2e_smoke.py`：隔离外部服务的完整验收及版本断言。
- 相关单元/集成测试与 README：新增回归和行为说明。

## 接口、数据和配置变化

- API：DRAFT 请求仍使用 `check_numeric_consistency`；结果 Schema 仍为 `2.1`，事实矩阵新增 `target_fact_id`、`target_candidate` 和 `reference_results`，旧字段继续保留。
- LLM Adapter：新增 `map_facts`、`review_mappings`；结构化请求显式携带目标 JSON Schema。
- 数据库/迁移：无模型或迁移变化；Alembic 当前为 `0002_ungraded_risk_counts (head)`。合成冒烟在开发数据库中新增一条测试任务记录，未清空数据库。
- 配置：Compose 允许显式覆盖 `LLM_ENABLED`，测试容器和合成冒烟固定禁用 LLM/OCR。
- 兼容性：继续读取 2.0 和既有 2.1 结果；`MISSING` 的线上枚举值不变，仅明确为未提及语义。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest -q tests\unit` | 通过 | `149 passed, 1 warning` |
| 变更范围 `ruff check` | 通过 | `All checks passed` |
| `.venv\Scripts\python.exe -m compileall -q app scripts` | 通过 | 无输出 |
| `npm run typecheck` | 通过 | Vue/TypeScript 无错误 |
| `npm run build` | 通过 | 生产构建成功；仅既有 bundle size warning |
| `git diff --check` | 通过 | 无空白错误；仅 Windows CRLF 提示 |
| `scripts\verify.ps1` | 通过 | 无缓存 API/test 镜像构建、迁移、Compose 全量测试和合成冒烟全部通过 |
| Compose 全量测试 | 通过 | `164 passed, 1 warning` |
| Alembic `current` | 通过 | `0002_ungraded_risk_counts (head)` |
| `/health`、`/ready`、OpenAPI、`/console/` | 通过 | database ok、API 0.2.2、DRAFT 路由存在、静态入口 HTTP 200；不代表视觉验收 |
| 无正文 LLM `/v1/models` | 失败并安全映射 | `LLM_UPSTREAM_ERROR`；未发送合同内容、未输出凭据或原始响应 |
| 合成单页 OCR probe | 未执行 | 模型探测失败后按门控停止外部验收，未继续调用 |

## Docker 与运行状态

- Docker Desktop 4.63.0、Engine 29.2.1、Compose 5.0.2 可用。
- PostgreSQL：完整验收期间 healthy；最终 `Exited (0)`。
- API：完整验收期间 healthy；最终 `Exited (0)`。
- Worker：完整验收期间 running/healthy；最终 `Exited (0)`。
- 控制台：静态入口 HTTP 200；视觉和人工交互由用户验收。
- 最终是否保持运行：否；恢复到本轮开始时三项服务均停止的状态。

## 重要决策

- 自动风险和通过必须以目标合同事实、可靠跨文件语义映射及程序精确比较为共同前提。
- 同一来源未提及目标事实不能作为数值冲突；语义、币种、单位、时间范围或公式输入不确定时只进入人工复核。
- 公式 ID 相同但定义不同不能覆盖或任选；主模型与评审模型必须对同一规范化定义达成共识。
- 本地完整验收与真实外部服务解耦，避免上游故障阻塞确定性回归或意外发送内容。

## 已知问题与风险

- LLM 模型列表仍返回 `LLM_UPSTREAM_ERROR`，无法确认抽取、评审和 advice 模型实际可用；真实模型映射质量尚未验收。
- 按外部门控未执行本轮合成 OCR probe，前序 OCR 仍存在响应协议不匹配记录。
- 未执行任何真实或脱敏合同的外部 LLM/OCR 调用；完整五文件 HYBRID 效果仍未验证。
- 前端 bundle 仍有超过 500 KiB 的 Vite 警告；视觉与人工交互未由 Agent 验收。

## 下一步建议

1. 由上游管理员恢复 LLM 网关后，先只重跑一次无正文模型列表并确认三个模型 ID。
2. 模型门通过后再运行一次完全合成的 OCR probe；OCR 协议成功前不运行 PDF 或五文件任务。
3. 两个门均通过且获得外部发送授权后，先做单份脱敏辅助资料的双模型映射证据抽查，再决定是否运行唯一一次五文件 HYBRID。
4. 用户手工验收事实矩阵的目标值、逐来源状态和人工复核展示。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260824_dynamic-document-and-numeric-analysis-principles.md`
- `docs/progress/20260824-091905_dynamic-fact-numeric-closure.md`
- `app/workflows/draft_review.py`
- `app/draft_review/facts.py`
- `app/draft_review/numeric_rules.py`

## 交接摘要

DRAFT_REVIEW 0.5.1 / rules 0.4.1 已改为目标合同中心的逐资料语义映射与程序数值比较。
MISSING 表示未提及，不自动产生风险或复核；币种、单位、语义和公式不确定均安全降级。
本地单元测试 149 项、Docker 全量 164 项、前端 typecheck/build、Alembic、合成冒烟均通过。
LLM 无正文模型探测仍为 `LLM_UPSTREAM_ERROR`，因此未继续 OCR 或真实文件调用。
API、Worker、PostgreSQL 最终均为 Exited(0)；未 commit、push、清库或删除卷。
