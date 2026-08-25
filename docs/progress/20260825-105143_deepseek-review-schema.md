# 任务进度：DeepSeek 动态评审 Schema 与真实样本门控

## 基本信息

- 时间：2026-08-25 10:51:43 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / DIAGNOSE / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；开始前已有多项未提交修改，本次继续增量修改 LLM Schema、OpenAI Client、定向评测器、相关测试和文档，未回退、清理、提交或推送其他改动

## 用户目标

修复 DeepSeek 事实评审返回空决策仍符合 Schema 的问题：评审必须恰好覆盖输入的 N 个候选，失败时只允许一次携带遗漏身份的纠错；修改后先做 1 次真实探测，成功才复验 3 次。评审通过后允许用同模型诊断模式运行 952 字符脱敏样本，但不得形成正式独立共识或 `PASS`。

## 本次完成

- `FactReview.decisions` 改为至少 1 项，生成的基础 JSON Schema 包含 `minItems=1`。
- 每次事实评审根据当前 payload 生成动态 Schema，将 `decisions.minItems` 和 `maxItems` 同时设为候选数 N。
- Adapter 在 Pydantic 校验后继续验证决策数量、候选复合身份集合和唯一性，确保每个输入候选恰好出现一次。
- 评审身份校验失败时生成只包含期望数量、遗漏身份、重复身份和额外身份的安全纠错指令；评审结构纠错固定最多 1 次，不受更高全局重试配置影响。
- 评测器支持动态 Schema、身份失败纠错、1 次探测成功后 3 次复验，以及门控后的 `SAME_MODEL_DIAGNOSTIC` 真实样本抽取和评审。
- 新增 6144-token 真实样本专用复验入口，只在 4096-token 响应明确 `finish_reason=length` 后使用一次，不改变生产默认 token 配置。
- 补充 MockTransport 测试，验证动态 `minItems=maxItems=N`、空决策触发一次携带遗漏候选身份的纠错，以及第二次完整响应成功。

## 修改文件

- `app/adapters/llm/schemas.py`：`FactReview.decisions` 增加最小项目数约束。
- `app/adapters/llm/openai_client.py`：新增动态评审 Schema、输入相关身份校验、安全纠错内容和评审专用一次结构重试。
- `scripts/llm_model_eval.py`：实现 1+3 评审门、动态 Schema、同模诊断真实样本与 6144-token 单次复验。
- `tests/unit/test_openai_llm_client.py`：覆盖动态 Schema 和针对性纠错。
- `README.md`：记录评审数量、身份覆盖和纠错边界。
- `docs/progress/20260825-105143_deepseek-review-schema.md`：本记录。

## 接口、数据和配置变化

- API：无路由或请求参数变化。
- LLM 响应约束：`FactReview.decisions` 从允许空数组改为至少 1 项；运行时进一步精确约束为 N 项。
- LLM 调用：评审校验失败最多增加 1 次结构纠错；网络/HTTP 重试策略未修改。
- 结果模式：真实样本仅标记 `SAME_MODEL_DIAGNOSTIC`、`independent_review=false`，不能形成正式独立共识。
- 数据库/迁移：无。
- `.env`、正式模型默认值、`LLM_ENABLED`、`LLM_MAX_OUTPUT_TOKENS=4096`：未修改。
- 下载器、OCR、Embedding、Rerank：未修改或调用。

## 真实调用证据

本轮共执行 6 次 HTTP，全部请求及实际模型均为 `DeepSeek-V4-Flash-0731`，使用 `json_schema`、`temperature=0`、`trust_env=False`。未输出完整响应、合同正文、证据原文或事实值。

### 合成事实评审

| 阶段 | 调用 | 结果 | 耗时 |
|---|---:|---|---|
| 首次门控探测 | 1 | HTTP 200；严格 JSON/Schema；5/5 决策、5/5 正确、身份和证据完整；未使用纠错 | 6,696 ms |
| 成功后复验 | 3 | 3/3 HTTP、Schema、5/5 决策、业务判断、身份和证据全部通过；决策指纹 3/3 一致；均未使用纠错 | 6,551–7,199 ms，中位 7,145 ms |

网关真实接受动态 `minItems=maxItems=5` Schema。与上一记录中 3 次稳定返回空决策相比，动态结构约束解决了评审列表为空的问题。针对性纠错路径已由 MockTransport 验证，但本次真实响应首次即完整，因此没有消耗纠错调用。

### 952 字符脱敏样本

样本本地解析为 952 字符、10 个块、1 个抽取分块。仅记录安全状态：

| max output tokens | 调用 | HTTP / 模型 | 结束原因 | 耗时 | 结果 |
|---:|---:|---|---|---:|---|
| 4096 | 1 | HTTP 200 / 身份匹配 | `length` | 63,482 ms | 响应截断，不是完整 JSON，未进入 Schema/证据校验 |
| 6144 | 1 | HTTP 200 / 身份匹配 | `length` | 88,860 ms | 仍截断，不是完整 JSON；按上限停止 |

真实抽取两档均失败，因此没有执行真实同模评审，没有生成合同事实结果、风险、通过项或正式结论。

## 当前结论

- DeepSeek 合成事实评审：通过。动态 Schema 后探测加复验共 4/4 完整、正确且稳定。
- DeepSeek 合成事实抽取：沿用上一记录的 3/3 通过证据。
- DeepSeek 952 字符真实抽取：不通过。4096 和受控 6144 两档都因输出长度截断。
- 同模真实诊断链路：未完成；抽取门失败，未进入评审。
- 整体状态仍为 `PARTIAL`，但当前阻塞已从事实评审转为真实样本抽取输出膨胀。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 pytest：评测器、LLM Client、事实、DRAFT 工作流、Advice、配置 | 通过 | 61 passed，1 个既有 LangGraph pending-deprecation warning |
| `ruff check`（本轮相关 Python 文件） | 通过 | All checks passed |
| `compileall -q`（本轮相关 Python 文件） | 通过 | 无输出，退出码 0 |
| `git diff --check`（本轮相关文件） | 通过 | 无空白错误；仅现有 LF/CRLF 工作区提示 |
| DeepSeek 评审 1+3 门控 | 通过 | 4 次 HTTP，无纠错，完整决策率 100% |
| 952 字符真实样本 | 失败并停止 | 2 次 HTTP，4096/6144 均明确截断 |
| 全仓 pytest / Docker / Compose / 数据库 / OCR / 五文件工作流 | 未执行 | 明确不在本轮范围 |

## Docker 与运行状态

- API：未启动、停止或重启。
- Worker：未启动、停止或重启。
- PostgreSQL：未操作。
- 控制台：未操作。
- 最终是否保持运行：本任务未改变任何服务状态。

## 重要决策

- `required_decision_count` 不再只作为输入提示；输出 JSON Schema 和 Adapter 身份校验都强制执行。
- 评审纠错只处理结构和身份覆盖问题，不因业务判断与预期不同而诱导模型改判。
- 动态 Schema 已由真实网关验证，不需要放松 Pydantic 或从自然语言猜测 JSON。
- 6144-token 只用于一次明确截断复验；复验仍截断后停止，不继续提高 token 上限。
- 同模诊断可以验证工程链路，但不能替代 MiniMax 或其他真实独立模型。

## 已知问题与风险

- 952 字符输入产生超过 6144 tokens 的输出，说明 DeepSeek 在真实抽取 Schema 下输出明显膨胀；尚未安全取得完整响应，无法判断事实数量、证据质量或膨胀字段来源。
- 当前不应仅继续提高 token 上限；需要通过更紧凑的抽取任务、数组上限、字段长度约束或按需候选恢复降低输出规模。
- 候选恢复第二阶段此前因合成抽取 3/3 而未实现；真实样本截断表明应重新评估“程序候选定位 + 模型语义分类”的紧凑路径。
- 仍没有可归因的第二个独立评审模型；MiniMax 路由问题待网关方确认。
- 评审分批尚未经过几十页或两百页真实合同验证。

## 下一步建议

1. 不再重试当前自由抽取 Prompt；先设计安全的输出压缩门，限制每块事实/概念/规则数量和证据长度，或启用程序数值候选定位后的严格分类 Schema。
2. 使用相同合成数据验证紧凑抽取不会降低开放字段召回、证据逐字回查和类型覆盖，再只对 952 字符样本执行 1 次受控复验。
3. 紧凑抽取通过后，复用本轮已通过的动态事实评审 Schema 完成同模诊断评审。
4. MiniMax 路由修复后，只验证其独立评审能力，不重复 Qwen 或完整模型矩阵。

## 下一会话首先阅读

- `docs/progress/20260825-105143_deepseek-review-schema.md`
- `docs/progress/20260825-103658_deepseek-followup.md`
- `app/adapters/llm/openai_client.py`
- `app/adapters/llm/schemas.py`
- `scripts/llm_model_eval.py`

## 交接摘要

动态 `minItems=maxItems=N` 和 Adapter 身份校验已解决 DeepSeek 空评审问题：真实探测加复验 4/4 均完整返回 5/5 决策，业务判断和证据全部通过，耗时约 6.6–7.2 秒。随后 952 字符脱敏样本在 4096 和一次受控 6144 tokens 下都明确截断，真实抽取未通过，因而未执行同模真实评审。当前整体仍为 PARTIAL，阻塞从评审转为真实抽取输出膨胀。61 个定向测试、Ruff、编译和空白检查通过；服务、数据库和 OCR 未操作。
