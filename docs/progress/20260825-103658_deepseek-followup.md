# 任务进度：DeepSeek 定向复验与评审上下文优化

## 基本信息

- 时间：2026-08-25 10:36:58 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / DIAGNOSE / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；开始前已有缺页识别、比较、OCR、前端及 LLM 等多项未提交修改，本次仅在 LLM Prompt、配置、事实评审分批、DRAFT 工作流、结果 metadata、定向评测器、相关测试和文档上增量修改，未回退或清理其他改动

## 用户目标

修正事实评审用例，将正文中证据、原值和位置均匹配的两个期限事实分别判为 `ACCEPT`；只对 DeepSeek 重新评测事实评审和抽取；限制长文档评审上下文并增加不可形成正式共识的同模诊断模式。确定性文本对齐产生的原始版本差异必须完整保留，LLM 只补充跨文档事实映射和程序数值校验。

## 本次完成

- 修正合成事实评审期望：三个真实候选要求 `ACCEPT`，证据错位日期和篡改数量要求 `REJECT/UNCERTAIN`；未新增同文档语义数值冲突检测。
- 强化事实抽取 Prompt，要求逐块扫描金额、日期、期限、期数、数量、比例和利率，不得漏掉同字段不同位置的真实值，不得换算或修正原值。
- 强化事实评审 Prompt，明确逐条独立判断、不得因其他位置存在不同值而拒绝真实事实、每条输入候选必须恰好一个决策。
- 新增 DeepSeek-only 评测入口，固定 `json_schema`、`temperature=0`、无结构纠错和最多 14 次预算；本轮实际使用 6 次。
- 将生产事实评审改为候选证据父块及前后邻近块，并按序列化 payload 字符数顺序分批；表格单元格可回溯所属表格块。
- 严格合并批次结果：候选决策必须完整且唯一，拒绝跨批引用和批间模型身份漂移；总置信度取批次最小值，所有批次完成后才可标记证据完整。
- 新增显式同模诊断模式。诊断结果不会进入独立共识、模型风险或模型通过项；没有确定性风险时只能返回 `REVIEW_REQUIRED`。
- 增加回归测试，确认同模诊断仍完整保留确定性文本差异及对应模板风险，LLM 不参与过滤原始差异。
- 合成抽取 3/3 通过，因此没有实现候选恢复第二阶段；该分支只在单阶段仍遗漏时才需要。

## 修改文件

- `app/adapters/llm/openai_client.py`：强化抽取和逐事实评审 Prompt；继续使用 `trust_env=False`、`temperature=0`。
- `app/draft_review/facts.py`：新增证据邻近块选择、评审分批、表格父块定位和严格批次合并。
- `app/workflows/draft_review.py`：顺序执行并汇总事实评审批次；实现同模诊断结果门禁和 metadata。
- `app/core/config.py`、`.env.example`：新增评审批次、邻近块和同模诊断配置；禁止通过旧独立门禁开关授权同模共识。
- `app/schemas/results.py`：metadata 增加向后兼容的 `independent_review`、`review_mode` 可选字段。
- `scripts/llm_model_eval.py`：修正评审期望并新增 DeepSeek 定向复验入口和安全摘要。
- `tests/unit/test_llm_model_eval.py`、`tests/unit/test_draft_facts.py`、`tests/unit/test_draft_review_workflow.py`、`tests/unit/test_core.py`：补充定向回归。
- `README.md`：说明评审上下文、同模诊断边界及确定性差异不可被 LLM 过滤。

## 接口、数据和配置变化

- API：无路由或请求结构变化；结果 Schema 版本仍为 `2.1`。
- 结果 metadata：新增可选 `independent_review` 和 `review_mode`；诊断执行模式为 `HYBRID_DIAGNOSTIC`。
- 数据库/迁移：无。
- 新配置：`LLM_REVIEW_BATCH_MAX_CHARS=12000`、`LLM_REVIEW_CONTEXT_BLOCKS=1`、`LLM_SAME_MODEL_DIAGNOSTIC=false`。
- 配置安全：`LLM_REQUIRE_INDEPENDENT_MODEL=false` 不再允许启动；同模诊断仅允许 `development/test/evaluation`。
- 正式模型默认值、`LLM_ENABLED`、`LLM_NATIVE_STRUCTURED_OUTPUT` 和真实 `.env`：未修改。
- 下载器、OCR、Embedding、Rerank：未修改或调用。

## DeepSeek 真实合成评测

所有调用请求模型和实际模型均为 `DeepSeek-V4-Flash-0731`，使用 JSON Schema、`temperature=0`、`trust_env=False`，无网络重试和结构纠错。终端及本记录未输出完整响应、证据原文或事实值。

| 角色 | 调用 | HTTP / 模型身份 | JSON / Schema | 业务门 | 耗时 | 结论 |
|---|---:|---|---|---|---|---|
| 事实评审 | 3 | 3/3 HTTP 200，3/3 身份匹配 | 3/3 严格 JSON，3/3 Schema | 三次均返回 0/5 决策，候选身份与证据覆盖失败 | 1,293–1,442 ms，中位 1,421 ms | 不通过；旧 `3/5` 结论失效，但修正后暴露稳定空决策问题 |
| 事实抽取 | 3 | 3/3 HTTP 200，3/3 身份匹配 | 3/3 严格 JSON，3/3 Schema | 3/3 类型覆盖完整，3/3 证据有效，8 个事实身份均唯一 | 35,902–40,580 ms，中位 37,058 ms | 通过；不需要候选恢复第二阶段 |

抽取三次均为 8 个事实、7 种值类型，安全事实身份哈希有两个集合，其中一个出现 2 次，精确集合稳定率为 66.7%；业务覆盖、Schema 和证据门均为 100%。评审空决策后新增了 payload 级 `required_decision_count` 等显式要求，但遵守调用边界，本轮未再次真实验证该增强。

## 真实样本门控

- 本地只读预检：脱敏目录中的 `项目方案确认函.docx` 为 952 字符、10 个解析块、1 个 12000 字符抽取分块。
- LLM 真实样本调用：未执行。
- 原因：修正后的事实评审 0/3 通过，联合门未打开；没有把同模诊断当作独立共识，也没有因抽取单角色通过而绕过门禁。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 pytest：评测器、LLM Client、事实、DRAFT 工作流、Advice、配置 | 通过 | 59 passed，1 个既有 LangGraph pending-deprecation warning |
| `ruff check`（本轮相关 Python 文件） | 通过 | All checks passed |
| `compileall -q`（本轮相关 Python 文件） | 通过 | 无输出，退出码 0 |
| `git diff --check`（本轮相关文件） | 通过 | 无空白错误；仅现有 LF/CRLF 工作区提示 |
| DeepSeek-only 合成复验 | PARTIAL | 6 次 HTTP；抽取通过，评审空决策失败 |
| 952 字符脱敏样本 LLM 抽取 | 未执行 | 评审门失败，按计划停止 |
| 全仓 pytest / Docker / Compose / 数据库 / OCR / 五文件工作流 | 未执行 | 明确不在本轮范围 |

## Docker 与运行状态

- API：未启动、停止或重启。
- Worker：未启动、停止或重启。
- PostgreSQL：未操作。
- 控制台：未操作。
- 最终是否保持运行：本任务未改变任何服务状态。

## 重要决策

- 两个期限事实只要各自证据、原值和位置匹配，都应由事实评审 `ACCEPT`；本轮不新增同文档数值冲突风险。
- 文件版本差异始终由确定性文本对齐产生并完整展示，LLM 不得替代、删除或过滤。
- 同模诊断是显式开发能力，不等价于关闭独立评审，也不能生成正式 `PASS`。
- DeepSeek 单阶段抽取已达到 3/3，不引入候选恢复的额外复杂度和调用成本。
- 评审 JSON/Schema 成功但决策列表为空仍是业务失败；空列表不能计为证据合规。

## 已知问题与风险

- DeepSeek 修正评审三次稳定返回空决策；新增显式决策数量要求尚未经过真实调用验证。
- 当前仍没有第二个可归因的独立文本模型；MiniMax 请求实际返回 DeepSeek 的路由问题仍待网关方确认。
- DeepSeek 抽取安全身份集合的精确稳定率为 66.7%，尽管类型、证据和业务覆盖均通过，字段命名或候选粒度仍可能漂移。
- 正式三个模型的共同响应模式仍未形成证据，`LLM_NATIVE_STRUCTURED_OUTPUT` 继续保持关闭。
- 评审分批已通过 Mock 和工作流测试，尚未使用几十页或两百页真实合同验证调用数量、上下文上限和总延迟。

## 下一步建议

1. 用新增 `required_decision_count` payload 对 DeepSeek 事实评审做一次新的、独立预算的 3 次定向验证；仍为空时需要调整评审响应 Schema 或更换真实独立模型，但不能放松完整决策门。
2. 由网关方确认 MiniMax 路由/响应 model 字段；修复后只测试 MiniMax 评审，不重复 Qwen 或完整矩阵。
3. 只有事实评审门和独立模型门均通过后，再发送已预检的 952 字符样本。
4. 后续使用合成长文档或无敏感大文本验证评审分批数量和延迟，不直接使用大型真实合同起步。

## 下一会话首先阅读

- `docs/progress/20260825-103658_deepseek-followup.md`
- `scripts/llm_model_eval.py`
- `app/draft_review/facts.py`
- `app/workflows/draft_review.py`
- `app/adapters/llm/openai_client.py`

## 交接摘要

评审用例已修正为接受两个分别有真实证据的期限事实，且没有新增同文档冲突检测。生产评审已改为证据邻近块分批并严格合并，同模诊断不会形成正式共识或 PASS。DeepSeek 合成抽取 3/3 通过，中位约 37.1 秒，因此未实现候选恢复；修正评审 3/3 均返回空决策而失败。共 6 次真实 HTTP，952 字符脱敏文件仅做本地计数预检，未发送给 LLM。59 个定向测试、Ruff、编译和空白检查通过；服务和数据库未操作。
