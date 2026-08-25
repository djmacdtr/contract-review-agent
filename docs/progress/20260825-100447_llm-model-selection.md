# 任务进度：集团 LLM 网关模型选型评测

## 基本信息

- 时间：2026-08-25 10:04:47 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / TEST / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；开始前已有多项无关未提交修改，本任务只增量修改 LLM Client、DRAFT 内部评审 payload、相关单测，并新增安全评测脚本和本记录

## 用户目标

依据网关实时模型列表，对 `Qwen3.8-27B`、`MiniMax-M2.7`、`DeepSeek-V4-Flash-0731` 使用相同合成请求完成事实抽取、独立评审、映射评审、风险 Advice 和原生结构化输出能力评测；满足安全、质量、独立模型和延迟门后才允许对 952 字符脱敏样本复验并更新正式模型配置。

## 本次完成

- 新增可复现的安全评测脚本，固定 `temperature=0`、`trust_env=False`、`stream=false`，通过流式读取响应体记录 HTTP 首包与总耗时。
- 安全摘要只保留状态、耗时、计数、Schema/证据合规率和不可逆哈希；不打印或落盘 Key、完整响应、合同正文、证据原文或事实值。
- 为生产与评测提取共用 completion body，并支持 `prompt_only`、`json_object`、`json_schema` 三种评测模式；生产现有开关仍只控制 JSON Schema。
- 将原始输入块加入内部 `review_facts` payload，使独立评审具备核对来源证据的实际输入；未修改公开 Pydantic Schema。
- 使用稳定安全哈希评估事实身份；现场发现 `value_type` 需要进入一致性身份后已修正，未放松证据或 Schema 门。
- 完成 15 次真实合成 HTTP 调用并按早停规则结束；未重复 `/v1/models`，未调用真实 OCR、下载器、数据库或完整工作流。
- 没有形成“抽取模型与评审模型不同、质量合格、共同响应模式和延迟均达标”的组合，因此没有发送 952 字符脱敏样本，也没有修改正式模型默认值或 `.env`。

## 修改文件

- `app/adapters/llm/openai_client.py`：提取共用角色 Prompt/请求构造，生产请求统一 `temperature=0`，保留 `trust_env=False`。
- `app/workflows/draft_review.py`：内部事实评审 payload 增加原始块。
- `scripts/llm_model_eval.py`：新增安全、受调用预算约束的模型选型评测器。
- `tests/unit/test_openai_llm_client.py`：覆盖三种响应格式与零温度请求。
- `tests/unit/test_draft_review_workflow.py`：验证独立评审收到原始块。
- `tests/unit/test_llm_model_eval.py`：覆盖首包采集、严格 JSON、围栏拒绝、安全摘要、事实哈希和调用上限。
- `docs/progress/20260825-100447_llm-model-selection.md`：记录评测证据与结论。

## 接口、数据和配置变化

- API：无公开 API 或结果 Schema 变化。
- 数据库/迁移：无。
- LLM 网关请求：新增固定 `temperature=0`；继续 `stream=false` 和 `trust_env=False`。
- 内部工作流：`review_facts` payload 新增 `blocks`，现有 Client Protocol 和响应 Schema 不变。
- 模型默认配置：未修改；没有合格独立模型组合，不能把不完整选型写入正式默认值。
- `.env`：未读取或写出完整内容，未修改真实 Key。
- Embedding/Rerank：未启用、未调用、未修改。

## 真实合成评测证据

所有请求使用相同合成数据与角色 Prompt，且 `temperature=0`。首包与总耗时在本次非流式网关响应中基本相同。

| 请求模型 | 可归因 HTTP 成功 | JSON / Schema | 证据与业务质量 | 延迟与稳定性 | 结论 |
|---|---|---|---|---|---|
| `Qwen3.8-27B` | 3/3 HTTP 200，实际模型 3/3 匹配 | prompt、JSON Schema、JSON Object 均 0/3 有效 JSON；prompt 一次 `finish_reason=length` | 未进入 Schema/证据校验 | 259,580–281,533 ms，中位 266,305 ms，全部超过 240 秒门槛 | 不推荐任何角色；慢且结构化输出失败 |
| `MiniMax-M2.7` | 3/3 HTTP 200，但实际模型均为 `DeepSeek-V4-Flash-0731` | 返回内容 3/3 为严格 JSON/Schema，但不可归因给 MiniMax | 证据有效；类型覆盖均为 80%，且模型身份错误 | 22,206–37,081 ms，中位 33,885 ms | 不推荐；网关未按请求模型执行，无法作为独立模型 |
| `DeepSeek-V4-Flash-0731` | 所有 9 次调用 HTTP 200、实际模型匹配 | JSON Schema 能力通过；全部严格 JSON/Schema | 抽取角色 1/4 完整通过，证据率 100%，类型完整率 25%；评审 3/5 判断正确；Advice 3/3 通过 | 抽取中位 31,372 ms、最大 39,041 ms；评审 15,337 ms；Advice 中位 14,696 ms | Advice 单角色合格；抽取不稳定，评审不合格 |

补充稳定性：DeepSeek 抽取的事实身份集合安全哈希在现场摘要中 3/4 相同，但该版本哈希未包含 `value_type`；现场已修正评测器，不能把 75% 误写成完整事实语义稳定率。业务门已独立捕获类型覆盖漂移，完整抽取成功率仅 25%。DeepSeek Advice 三次均一一覆盖 3 个风险、内容不重复且无技术标识，严格结构/风险覆盖稳定率 100%；措辞哈希三次不同，精确文本一致率 33.3%。

## 角色选型结论

- `LLM_EXTRACTION_MODEL`：没有合格正式候选。DeepSeek 是唯一可归因且结构化输出可用的模型，但 4 次角色运行只有 1 次满足全部动态数值类型覆盖，不能上线。
- `LLM_REVIEW_MODEL`：没有合格正式候选。DeepSeek 对抗评审正确率仅 60%，且即使合格也不能与自身组成独立双模型；MiniMax 实际被路由为 DeepSeek；Qwen 已失败。
- `LLM_ADVICE_MODEL`：`DeepSeek-V4-Flash-0731` 单角色表现最佳并通过本轮 Advice 门，但由于整体不存在可部署抽取/评审组合，本轮不单独修改正式默认配置。
- 映射评审：未执行。事实评审没有两个合格入围者，按早停规则不继续扩大调用。
- 952 字符脱敏样本：未执行。不存在不同模型的合格抽取/评审组合，真实数据门未打开。

## 参数结论

- `LLM_NATIVE_STRUCTURED_OUTPUT`：DeepSeek 的 JSON Schema 能力真实通过，但无法证明最终三个角色模型共同支持，本轮保持关闭且不改默认值。
- `LLM_TIMEOUT_SECONDS`：保持 300 秒；Qwen 已因单次超过 240 秒淘汰，不提高超时迁就。
- `LLM_MAX_OUTPUT_TOKENS`：保持 4096；Qwen 出现一次截断但未入围，不执行 6144-token 复验。
- `LLM_CHUNK_MAX_CHARS`：保持 12000；本轮未进入 952 字符真实样本，缺少调整依据。
- `LLM_STRUCTURE_RETRY_ATTEMPTS`：默认保持 2；选型未成功，不把计划中的 1 次重试写入正式配置。评测器自身只允许一次显式纠错且本次未触发。
- `temperature`：生产与评测请求统一固定为 0，提高可复现性；Pydantic 和证据校验继续保留。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest tests/unit/test_llm_model_eval.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_facts.py tests/unit/test_result_advice.py tests/unit/test_core.py -q` | 通过 | 最终 52 passed，1 个既有 LangGraph pending-deprecation warning |
| `python -m ruff check`（本轮 Python 变更文件） | 通过 | All checks passed |
| `.venv\Scripts\python.exe -m compileall -q`（本轮 Python 变更文件） | 通过 | 无输出，退出码 0 |
| `git diff --check`（本轮文件） | 通过 | 无空白错误；Git 仅提示现有 LF/CRLF 工作区策略 |
| 真实合成模型矩阵 | 部分完成并安全收敛 | 15 次 HTTP；调用上限 44；无网络自动重试；无真实合同内容 |
| 脱敏真实样本 | 未执行 | 没有合格独立模型组合，按门控停止 |
| 全仓 pytest / Docker / Compose / 数据库 / OCR / 五文件工作流 | 未执行 | 明确排除在本轮范围外 |

## Docker 与运行状态

- API：未启动、停止或重启。
- Worker：未启动、停止或重启。
- PostgreSQL：未操作。
- OCR：未调用。
- 控制台：未操作。
- 最终是否保持运行：本任务未改变任何服务状态。

## 重要决策

- 实际 `model` 与请求模型不一致是硬失败；MiniMax 返回的 DeepSeek 内容不能记作 MiniMax 能力，也不能满足独立评审要求。
- 严格 JSON/Schema 通过不替代业务质量门；DeepSeek 的抽取类型漂移和评审误判均阻止正式选型。
- Advice 可单独表现良好，但不能掩盖抽取/评审组合缺失，也不能据此启动真实合同样本。
- 没有合格组合时保留旧默认值只是避免写入未经证实的新配置，不代表旧 `GLM-5.2` 配置重新获得有效性；LLM 应继续保持禁用。

## 已知问题与风险

- 网关对 `MiniMax-M2.7` 的请求实际返回 DeepSeek，需要平台侧确认模型路由、别名或响应 `model` 字段。
- Qwen 三种模式都超过延迟门并无法返回有效 JSON；其 JSON Schema/JSON Object 参数被 HTTP 接受不等于能力生效。
- 目前只有一个可归因且结构化可用的文本模型，无法满足项目独立双模型约束。
- DeepSeek 抽取的事实类型稳定性和独立评审准确率不足；不允许通过放松 Schema、证据或独立评审门上线。
- 当前代码和 `.env.example` 中的旧模型名仍未被新证据替代；在新组合验收前不得启用正式 LLM 工作流。

## 下一步建议

1. 将 MiniMax 的“请求模型与实际模型不一致”证据交给网关平台团队，修复路由后只重跑 MiniMax 的最小合成抽取、评审和 Advice。
2. 要求平台提供至少两个能够在响应中正确标识自身的文本模型；独立评审模型必须与抽取模型不同。
3. 平台修复后复用 `scripts/llm_model_eval.py`，不要重复已失败的 Qwen 路径；只有形成合格组合后才运行 952 字符脱敏样本并更新默认配置。

## 下一会话首先阅读

- `scripts/llm_model_eval.py`
- `docs/progress/20260825-100447_llm-model-selection.md`
- `app/adapters/llm/openai_client.py`
- `app/workflows/draft_review.py`

## 交接摘要

已完成安全评测器和 15 次真实合成调用。Qwen 慢且三种模式均无有效 JSON；MiniMax 三次均被网关实际路由为 DeepSeek；DeepSeek JSON Schema 与 Advice 可用，但抽取仅 1/4 完整通过、评审仅 3/5 正确。没有不同模型的合格抽取/评审组合，因此未发送脱敏合同、未执行映射评审、未更新模型默认值。生产请求已统一 `temperature=0`，独立事实评审现在会收到原始块；公开 Schema、`.env`、Embedding/Rerank 和服务状态均未改变。
