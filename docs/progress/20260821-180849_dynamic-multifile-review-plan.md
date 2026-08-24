# 任务进度：动态多文件检查后续计划与业务边界

## 基本信息

- 时间：2026-08-21 18:08:49 +08:00
- 状态：PARTIAL
- 任务类型：REVIEW / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保留本轮未提交的黄金标注、LLM Client、事实矩阵、前端组件、测试和文档改动，未清理、未回退

## 用户目标

确认当前接口、业务逻辑和 LLM 接入的完成程度，确定真实五文件与 OCR 的合理验收顺序，固化动态多文件检查的关键业务边界，并永久将浏览器操作和视觉验收移出 Agent 开发范围，由用户手工负责。

## 本次完成

- 复核当前实现和上一份里程碑记录，确认 API 与确定性工作流骨架基本完成，但真实 LLM 业务闭环尚未完成。
- 形成双模型动态语义检查的后续实施顺序和真实五文件分层验收门。
- 在 `AGENTS.md` 和 `README.md` 固化 Agent 不执行浏览器操作、截图或视觉验收的协作边界。
- 在 `README.md` 固化 FINAL_COMPARE、DRAFT_REVIEW、双模型共识、声明式数值执行和请求契约的最新业务决定。
- 未实施双模型、动态语义计划、声明式数值 AST、advice 工作流或公开请求 Schema 变化；这些仍属于下一阶段。

## 修改文件

- `AGENTS.md`：增加永久的前端视觉验收协作边界。
- `README.md`：增加自动化/人工视觉验收边界和后续业务边界。
- `docs/progress/20260821-180849_dynamic-multifile-review-plan.md`：记录本次状态判断、业务确认、计划和交接信息。

## 接口、数据和配置变化

- API：本次未修改。已决定后续删除 `check_asset_schedule`、`check_rent_schedule`，新增默认值为 `true` 的 `check_numeric_consistency`；接受旧请求客户端不兼容，但不能把该计划写成当前已实现。
- 结果契约：本次未修改。建议后续 DRAFT workflow 升至 `0.5.0`、rules 升至 `0.4.0`、结果 Schema 升至 `2.1`，并兼容读取 1.0/2.0。
- 数据库/迁移：无变化。
- 配置：本次无变化。建议后续增加独立评审模型配置和默认 `LLM_CONSENSUS_MIN_CONFIDENCE=0.85`。
- Docker：无文件或运行配置变化。
- 兼容性：本次仅文档变化；未来请求选项变化是有意的破坏性变化。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `git status --short --branch` | 通过 | 确认分支及既有 dirty 工作树，未覆盖或清理 |
| `docker compose ps` | 通过 | 当前未列出运行中的项目服务；本次未启动或停止服务 |
| `git diff --check`、定向文档差异和状态检查 | 通过 | 无空白错误；仅既有 Windows 行尾提示，三个文档内容与状态符合预期 |
| 宿主机单元测试 | 历史复用 | 上一记录为 `132 passed, 1 warning`；本次未重跑 |
| Docker PostgreSQL 全量测试 | 历史复用 | 上一记录为 `147 passed, 1 warning`；本次未重跑 |
| Ruff、编译、前端 typecheck/build、Alembic、API→Worker 冒烟 | 历史复用 | 上一记录均通过；本次纯文档变更未重跑 |
| 浏览器与视觉验收 | 不在 Agent 范围 | 由用户手工执行，不再作为 Agent 验收门 |
| 真实 OCR、真实 LLM、完整五文件 HYBRID | 未执行 | 必须按本记录的分层顺序推进 |

## Docker 与运行状态

- API：`docker compose ps -a` 显示 `Exited (0)`，本次未启动或停止。
- Worker：`docker compose ps -a` 显示 `Exited (0)`，本次未启动或停止。
- PostgreSQL：`docker compose ps -a` 显示 `Exited (0)`；未删除或清空数据库卷。
- 控制台：未启动服务，也未执行浏览器检查。
- 最终是否保持运行：否；本次未改变服务状态。

## 重要决策

- 整体 API 与确定性工作流骨架基本完成；LLM 业务链路尚未完成。当前已有 OpenAI 兼容单模型 Client、逐文件抽取、证据回查、失败降级和初版事实矩阵，但尚无真实模型成功结果。
- `FINAL_COMPARE` 仅比较打印前 DOCX 与盖章后扫描 PDF 的同文档内容，检查盖章前后是否被篡改；不做跨资料数值计算，不做印章识别或真伪判断。
- 通用数值核验只属于 `DRAFT_REVIEW`。模板、字段、跨资料映射和计算关系均可能变化，不按租金、设备数量、文件名、文件哈希、正文或段落写死规则。
- 主模型动态识别模板固定区/填写区、字段语义、跨资料对应关系和计算关系；独立评审模型核验证据、语义和公式。
- 只有证据完整、主模型与独立评审模型一致且置信度达到默认 `0.85`，结论才可影响自动风险或通过。分歧、单模型、低置信度或外部失败进入人工复核。
- 不同模型优于同模型重复评审；只有同一模型可用时，不把 LLM-only 判断自动定为风险或通过。
- 程序只使用 `Decimal` 和白名单声明式 AST 做精确比较/计算，禁止执行模型生成代码。
- 后续请求契约直接删除 `check_asset_schedule`、`check_rent_schedule`，新增 `check_numeric_consistency=true`；接受破坏旧请求客户端。
- 当前 29 个黄金候选只作为模型和 Prompt 回归集，不成为生产硬编码规则，也不能由开发者猜测业务分类。
- Agent 永久不执行浏览器操作、截图和视觉验收；用户负责页面视觉与人工交互确认。前端自动验证保留 typecheck、生产 build 和非视觉接口/静态资源检查。

## 已知问题与风险

- 真实 `/v1/models` 探测此前返回 `LLM_UPSTREAM_ERROR`，尚无真实模型成功结果；LLM 与 OCR 当前本机配置均为 disabled/not configured。
- 双模型评审、动态 `SemanticConcept`/`ValidationSpec`、声明式数值验证器和受证据约束的 advice 节点尚未实现。
- 当前公开请求仍包含两个固定规则开关，且尚未加入 `check_numeric_consistency`。
- 29 个黄金候选均未完成业务分类；现有差异结果尚不能作为规则质量闭环结论。
- 尚未跑真实五文件完整 OCR + LLM 流程，不能宣称整体业务已经开发完成。

## 下一步建议

1. 实现双模型独立评审、动态语义计划、白名单声明式数值 AST 和受证据约束的 advice；同时完成请求选项与 Schema 2.1 兼容读取。
2. 先做不含合同内容的 `/v1/models` 探测，再用完全合成单页文件做 OCR probe。
3. LLM 关闭，只运行固定真实五文件解析基线，检查结构、位置、页数、表格和 warning；解析问题不得由 LLM 掩盖。
4. 使用仓库外 `项目方案确认函.docx` 的有限分块完成双模型单文档验收。
5. 单文档证据和双模型共识通过后，仅运行一次完整五文件 HYBRID 任务。
6. 用户在仓库外人工抽查真实结果；只修复可泛化缺陷，不增加文件名、哈希、正文或段落特例。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/README.md`
- `docs/progress/20260821-173144_draft-golden-llm-matrix.md`
- `docs/progress/20260821-180849_dynamic-multifile-review-plan.md`
- `app/workflows/draft_review.py`
- `app/adapters/llm/openai_client.py`
- `app/draft_review/facts.py`

## 交接摘要

API 与确定性流程骨架可用，但 LLM 业务闭环未完成，暂不应直接跑完整五文件 HYBRID。
单模型抽取、证据校验、失败降级和初版事实矩阵已有实现；真实模型、双模型、动态语义 AST 和 advice 尚缺。
FINAL_COMPARE 仅做盖章前后同文档防篡改；通用数值核验只属于 DRAFT_REVIEW。
双模型一致、证据完整且置信度不低于 0.85 才可影响自动风险/通过，否则人工复核。
程序只执行 Decimal 与白名单声明式 AST，不执行模型代码，不写死真实合同特例。
浏览器与视觉验收永久由用户手工负责，Agent 只做前端 typecheck/build 和非视觉检查。
下一顺序为通用能力实现、无正文模型探测、合成 OCR probe、五文件解析、单文档 LLM、唯一完整 HYBRID。
工作树仍 dirty，未 commit、未 push；当前 Compose 未列出运行服务，本次未改变服务状态。
