# 任务进度：动态多文件 LLM 共识与数值检查

## 基本信息

- 时间：2026-08-22 16:29:45 +08:00
- 状态：PARTIAL
- 任务类型：BUILD
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保留前序会话及本轮未提交修改，未清理、未回退

## 用户目标

继续实施动态多文件检查计划，完成双模型独立评审、动态语义/数值计划、声明式 Decimal AST、受证据约束 advice、请求契约替换和 Schema 版本升级；浏览器视觉验收由用户手工负责。

## 本次完成

- 增加 `SemanticConcept`、`ValidationSpec`、`FactReview`、`AdviceResponse` 等 LLM 结构。
- 增加独立评审调用协议与 OpenAI 兼容 Client 校验；评审证据必须匹配同一文件候选事实和位置。
- 工作流增加事实评审、双模型独立模型/置信度 `0.85`/证据完整共识门；未通过者进入人工复核，不影响自动风险或通过。
- 增加白名单声明式数值 AST，使用 `Decimal` 执行 add/subtract/multiply/divide/sum 和比较，不执行模型代码。
- 接入动态数值规则、`REVIEW_REQUIRED` rule status、动态语义/规则元数据和 advice 节点；advice 引用只保留可回查证据，失败降级为 warning/review。
- 起草请求移除 `check_asset_schedule`、`check_rent_schedule`，新增 `check_numeric_consistency=true`。
- 结果 Schema 升至 `2.1`，保留 `2.0` 及历史旧结果读取；DRAFT workflow/rules 升至 `0.5.0`/`0.4.0`。
- `AGENTS.md` 和 README 已明确 Agent 不执行浏览器、截图或视觉验收，页面由用户手工确认。

## 修改文件

- `app/adapters/llm/schemas.py`、`base.py`、`openai_client.py`：双模型评审、动态计划和 advice 契约。
- `app/draft_review/numeric_rules.py`、`facts.py`：安全数值 AST 和共识事实矩阵过滤。
- `app/workflows/draft_review.py`：评审、共识、数值、advice 节点及版本。
- `app/schemas/requests.py`、`results.py`、`app/services/task_service.py`：请求替换、Schema 2.1、旧版本兼容。
- `frontend/src/api/types.ts`：复核状态和动态 metadata 类型。
- `tests/unit/test_numeric_rules.py`、`test_result_schema_v21.py`、既有 DRAFT/FINAL 测试：新增定向覆盖并更新版本断言。
- `README.md`：更新 Schema 和请求契约说明。

## 接口、数据和配置变化

- API：DRAFT options 现在只接受 `check_numeric_consistency`；旧两个固定开关按 `extra=forbid` 拒绝。
- 结果：新结果 `schema_version=2.1`；`RuleCheck.status` 增加 `REVIEW_REQUIRED`；metadata 可包含 reviewed files、semantic concepts、validation specs。
- 数据库/迁移：无变化。
- 配置：复用前序新增的独立评审模型、共识置信度和独立模型配置；本轮未新增环境变量。
- Docker：无 Dockerfile/Compose 变化。
- 兼容性：结果读取兼容 2.0；请求旧字段有意不兼容。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest -q tests/unit` | 通过 | `136 passed, 1 warning` |
| `.venv\Scripts\python.exe -m pytest -q` | 部分通过 | `136 passed, 15 errors`；15 个 integration fixture 因 PostgreSQL 主机 `postgres` 未运行/不可解析而在 setup 阶段失败，非业务断言失败 |
| 定向数值/Schema/事实/LLM/DRAFT 测试 | 通过 | `20 passed, 1 warning`；另数值/LLM `10 passed`、事实 `8 passed` |
| `ruff check` 变更模块 | 通过 | LLM、draft_review、workflow、Schema、task_service 无错误 |
| `python -m compileall -q app` | 通过 | 变更 Python 可编译 |
| `npm run typecheck` | 通过 | 前端类型检查通过 |
| `npm run build` | 通过 | Vite 生产构建通过；仅有 bundle size warning |
| `git diff --check` | 通过 | 无空白错误；仅 Windows 行尾提示 |
| 浏览器/截图/视觉验收 | 未执行 | 永久由用户手工负责，不作为 Agent 阻塞条件 |
| 真实 `/v1/models`、真实 LLM、OCR、五文件 HYBRID | 未执行 | 留待下一验收门 |

## Docker 与运行状态

- API：`Exited (0)`，本轮未启动或停止。
- Worker：`Exited (0)`，本轮未启动或停止。
- PostgreSQL：`Exited (0)`，未删除或清空卷。
- 控制台：未启动，未做浏览器检查。
- 最终是否保持运行：否；本轮未改变服务状态。

## 重要决策

- FINAL_COMPARE 仍只做打印前 DOCX 与盖章后扫描 PDF 的同文档防篡改比对；通用数值核验仅属于 DRAFT_REVIEW。
- 模板、字段、对应关系和公式均动态，29 个候选仅作为回归集，不形成生产特例。
- 双模型不一致、单模型、低置信度、证据不完整和外部失败均进入人工复核；不自动选择冲突来源的正确值。
- 浏览器操作、截图和视觉验收移出 Agent 范围，用户手工确认页面。

## 已知问题与风险

- 本轮实现使用 Mock/fixture 验证，尚无真实模型成功响应；前序 `/v1/models` 曾返回 `LLM_UPSTREAM_ERROR`。
- advice 的生产质量、动态语义映射和复杂表格语义仍需真实样本抽查；当前不把解析/OCR 不确定性当业务风险。
- 旧测试 fixture 缺少 advice 时会安全降级为 review；生产 OpenAI client 应提供评审和 advice 方法。

## 下一步建议

1. 无合同内容调用 `/v1/models`，确认主模型和评审模型可用。
2. 使用合成单页文件做最小 OCR probe。
3. 关闭 LLM，运行真实五文件解析基线。
4. 用仓库外 `项目方案确认函.docx` 做双模型单文档验收。
5. 证据人工抽查通过后，仅运行一次完整五文件 HYBRID；代码只修复可泛化问题。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/README.md`
- `docs/progress/20260822-162945_dynamic-multifile-llm-consensus.md`
- `app/workflows/draft_review.py`
- `app/draft_review/numeric_rules.py`
- `app/adapters/llm/openai_client.py`

## 交接摘要

双模型评审、证据共识门、Decimal AST、动态规则和 advice 已接入 DRAFT workflow。
请求改为 `check_numeric_consistency`，新结果为 Schema 2.1，旧 2.0 可读。
本次宿主机单元测试 135 passed，Ruff、compile、前端 typecheck/build 均通过。
真实 LLM/OCR/五文件 HYBRID 未执行；浏览器视觉由用户负责。
工作树 dirty，未 commit、未 push；Compose 服务均保持 Exited(0)。
