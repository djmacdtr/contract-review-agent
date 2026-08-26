# 任务进度：DRAFT_REVIEW 交付路径与真实三文件验收

## 基本信息

- 时间：2026-08-26 16:11:13 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / TEST / DIAGNOSE
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：ec14e96
- 工作树状态：dirty；保留既有抽取、事实、LLM 与测试未提交改动，并新增本阶段交付路径改动。

## 用户目标

将不稳定的 `SEMANTIC_PLAN` 降级为默认关闭的可选增强，直接以已评审的动态事实映射发布 DRAFT_REVIEW 正式结果，并在一次真实三文件任务中验收该路径。

## 本次完成

- 新增内部配置 `LLM_SEMANTIC_PLAN_ENABLED=false`；Compose 对 API 与 Worker 明确传入该默认值，未新增任务请求字段或修改公开 Schema。
- DRAFT_REVIEW 图在交付模式从跨资料映射（其中包含映射复核）直接进入 `build_result`，保留开关开启时原语义规划路径。
- 交付模式不执行模型生成的数值 AST，且公开兼容字段 `semantic_concepts`、`validation_specs` 返回空列表。
- 正式结果复用事实矩阵、双侧事实差异和确定性 `Decimal` 规范化比较；已接受的缺失要求可发布为风险。未接受映射不会生成风险或通过结论。
- 建议输入现覆盖全部正式风险；建议调用失败仍保留确定性结果、fallback `analysis_advice` 与 `LLM_ADVICE_UNAVAILABLE` 警告。
- 真实诊断器新增：最多 10 次新增逻辑调用、15 分钟超时、宿主机到已发布 PostgreSQL 端口的专用 session factory、连接池释放和安全聚合指标。

## 修改文件

- `app/core/config.py`：语义规划内部开关，默认关闭。
- `compose.yaml`：交付容器显式使用 `LLM_SEMANTIC_PLAN_ENABLED=false`。
- `app/workflows/draft_review.py`：交付图旁路、正式事实汇总与严格消费门。
- `app/draft_review/facts.py`：分离已确认缺失风险与不确定映射的结果项开关。
- `app/results/advice.py`：所有正式风险均可获取模型建议或 fallback。
- `scripts/draft_review_real_diagnostic.py`：真实任务预算、宿主机 checkpoint 连接和安全指标。
- `tests/unit/test_core.py`、`tests/unit/test_draft_facts.py`、`tests/unit/test_draft_review_workflow.py`、`tests/unit/test_draft_review_real_diagnostic.py`、`tests/unit/test_result_advice.py`：直接覆盖交付路径和容错行为。

## 接口、数据和配置变化

- API：无公开接口或任务参数变化。
- 数据库/迁移：无新增迁移；复用已生效的 `0003_extraction_checkpoint`。
- 配置：新增内部 `LLM_SEMANTIC_PLAN_ENABLED`，默认 `false`。
- 兼容性：`FINAL_COMPARE` 未修改；DRAFT_REVIEW 的公开结构保留，语义规划兼容字段在交付模式为空列表。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest tests/unit/test_core.py tests/unit/test_result_advice.py tests/unit/test_draft_facts.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py -q` | 通过 | `101 passed, 1 warning` |
| `ruff check`（上述变更文件） | 通过 | 无 lint 错误 |
| `.venv\Scripts\python.exe -m compileall -q app scripts` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误 |

## 真实三文件验证

- 固定文件：融资租赁合同（回租）、对应模板、项目方案确认函。
- checkpoint 来源：`real_667625e6654d8f3e`；每个有效运行均复用 92 个 checkpoint，并为新任务保存 92 条可复用记录。
- 诊断文件：`.real-diagnostic-temp/draft-review-delivery-20260826-160931.jsonl`。
- 最近有效运行：2 次新增逻辑/HTTP 调用（`FACT_MAPPING`、`FACT_MAPPING_REVIEW`），44.563 秒，网关探测 0，`SEMANTIC_PLAN` 请求 0。
- 本阶段累计新增真实逻辑调用为 6，低于 10 次上限；没有 OCR 调用。
- 结果：`FAILED`，首个业务失败阶段为 `NUMERIC_VALIDATION_AND_FORMAL_DIFF`，错误分类为 `DYNAMIC_CHECK_INCOMPLETE`；未发布半成品，因此正式风险、通过、差异和建议数量均为 0。
- 两个更早的诊断文件（`155840`、`160035`）在 checkpoint 连接初始化前停止，均为 0 LLM 调用；随后已改为诊断器专用的宿主机 PostgreSQL session factory，避免 Compose 主机名在宿主机解析失败。

## 重要决策

- 已明确验证：语义规划不是本次有效运行的调用来源，JSONL 的 `SEMANTIC_PLAN` 操作数为 0。
- 读取 checkpoint 的安全聚合计数显示：参考资料事实评审为接受；目标合同同时有接受和明确拒绝的候选。明确拒绝、且未被映射消费的候选被过滤，不得形成通过结论。
- 对被映射消费的任一目标或辅助事实，若未通过独立事实评审，仍保持 `DYNAMIC_CHECK_INCOMPLETE`，不以放宽门槛换取成功。

## 已知问题与风险

- 最新真实运行在映射与映射复核 HTTP 均成功后，仍在正式事实消费门失败；当前安全诊断未保存模型输出，不能在不重发真实调用的前提下区分未接受映射、映射复核覆盖不足或被映射事实未通过评审。
- 因严格门要求，当前没有正式 DRAFT_REVIEW 结果；不得将模板检查单独当作完整多文档检查交付。

## 下一步建议

1. 在不记录合同文本或模型响应的前提下，为映射/映射复核新增“提案数、接受数、未覆盖数、被拒绝事实引用数”安全聚合指标，并强制映射复核覆盖每一项提案及缺失要求。
2. 仅在上述明确修复后，新建一次任务；继续复用 `real_667625e6654d8f3e` checkpoint，禁止原样重跑。
3. 获得 `SUCCEEDED` 的三文件正式结果后，停止业务架构迭代，转入后端/前端构建、Alembic、Docker、API/Worker/控制台和内网部署验收。

## 下一会话首先阅读

- `docs/progress/20260826-161113_draft-review-delivery-path.md`
- `docs/progress/20260826-153608_structured-extraction-map-reduce-closure.md`
- `app/workflows/draft_review.py`
- `scripts/draft_review_real_diagnostic.py`

## 交接摘要

- 语义规划已在交付模式默认旁路，真实 JSONL 验证为 0 次语义规划请求。
- 事实矩阵、缺失风险、双侧差异、建议 fallback 和 Decimal 规范化均有定向测试覆盖。
- 真实三文件运行可稳定复用 92 个 checkpoint；诊断器的宿主机 PostgreSQL 连接已修复。
- 当前新阻塞在映射结果未满足正式事实消费的严格门，不是语义规划、OCR、网关探测或 checkpoint 基础设施。
- 本阶段未 commit、未 push；`.real-diagnostic-temp/` 已保留且未纳入版本控制。
