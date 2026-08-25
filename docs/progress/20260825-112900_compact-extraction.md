# 任务进度：紧凑事实抽取与两阶段语义规划

## 基本信息

- 时间：2026-08-25 11:29:00 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / FIX / DIAGNOSE / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；开始前已有大量并行会话未提交修改，本次仅增量修改相关 LLM、事实、工作流、评测、测试和 README 内容，未回退、清理、提交或推送其他修改

## 用户目标

解决 952 字符脱敏 DOCX 在 4096/6144 tokens 下真实事实抽取截断的问题：使用紧凑事实 Schema、程序证据回查、高召回数值候选和抽取后语义规划；先通过合成测试，再只做一次受控真实抽取及同模诊断评审。

## 本次完成

- 新增紧凑第一阶段抽取 Schema，仅返回开放式 `profile` 与 `facts`；删除模型响应中的 `missing_field_keys`、`semantic_concepts`、`validation_specs`、`source_file_id`、`evidence_text` 和 `normalized_hint`。
- 程序按位置回查证据并补齐现有完整 `FactCandidate`，校验来源文件、原值、位置和事实身份唯一性；下游结果 Schema 保持兼容。
- 增加通用高召回数值候选扫描，按正文长度和候选数量分批；保留动态字段与现有全部值类型，不使用固定字段清单。
- 新增内部 `plan_semantics()` 和 DRAFT_REVIEW `plan_semantics` 节点，在事实评审与跨文档映射后生成受限语义概念和声明式数值规则；公开 API、任务请求、结果 Schema、FINAL_COMPARE 和确定性文字差异逻辑未改变。
- 为概念、规则、数组、字段长度和数值 AST 增加安全边界；超限失败或分批，不静默截断。
- 评测器增加安全指标：请求/Schema/响应字符数、usage token、finish reason、数组计数、字符串最大长度、AST 节点/深度和校验状态；未打印或保存合同正文、完整响应或密钥。

## 修改文件

- `app/adapters/llm/schemas.py`、`openai_client.py`、`base.py`：紧凑抽取/语义规划内部契约、证据回填和 Adapter 方法。
- `app/draft_review/facts.py`、`numeric_rules.py`、`app/workflows/draft_review.py`：数值候选、批次、证据校验、AST 限制和内部语义规划节点。
- `scripts/llm_model_eval.py`：紧凑抽取评测、安全膨胀指标和单次 `--deepseek-compact-real` 入口。
- `tests/unit/test_openai_llm_client.py`、`test_llm_model_eval.py`、`test_draft_facts.py`、`test_draft_review_workflow.py`、`test_numeric_rules.py`：Schema、证据回查、动态字段、数值类型、AST、语义规划和诊断回归。
- `README.md`、`app/adapters/llm/__init__.py`：行为和内部能力说明/导出。

## 接口、数据和配置变化

- API：无 FastAPI 路由、任务请求参数或结果 Schema 变化；新增的是 LLM Adapter/工作流内部方法和状态。
- 抽取响应：模型使用 `CompactDocumentFactExtraction`；程序向下游恢复为既有 `DocumentFactExtraction`。
- 语义规划：内部 `SemanticPlanResponse`，在跨文档映射后写回目标文档内部抽取值。
- 配置：`LLM_MAX_OUTPUT_TOKENS` 保持 4096；未新增真实 token 上限或修改 `.env`。
- 数据库/迁移、OCR、Embedding、Rerank：未修改、未调用。

## 合成验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 Ruff | 通过 | All checks passed |
| 定向 compileall | 通过 | 无输出，退出码 0 |
| 定向 pytest | 通过 | 55 passed，1 个既有 LangGraph pending-deprecation warning |
| 合成事实/Schema/证据/数值/语义规划回归 | 通过 | 覆盖动态字段、主要数值类型、证据回查、AST 深度和工作流节点 |
| 952 字符本地安全预检 | 通过 | 10 块、952 字符、71 个高召回数值候选；紧凑 payload 约 10,241 字符，Schema 约 3,058 字符 |

## 真实调用证据

命令：`python scripts/llm_model_eval.py --deepseek-compact-real --real-sample <脱敏样本> --max-calls 3`

- 本次真实样本共 2 次 HTTP：第一阶段抽取 1 次，抽取成功后 SAME_MODEL_DIAGNOSTIC 事实评审 1 次；未调用语义规划真实阶段、MiniMax 或其他模型。
- 实际模型均为 `DeepSeek-V4-Flash-0731`，`json_schema`，`temperature=0`，`trust_env=False`，默认上限 4096。
- 抽取：HTTP 200，耗时 65,573 ms，首字节 65,573 ms，响应 13,732 字符，prompt/completion/total tokens 为 4,585/3,565/8,150，`finish_reason=stop`，严格 JSON/Schema/证据通过，47 个唯一事实，6 个值类型。
- 抽取未截断，未使用结构纠错；安全指标只记录结构和长度，不记录事实值或正文。
- 同模诊断评审：HTTP 200，耗时 76,684 ms，响应 14,168 字符，prompt/completion/total tokens 为 28,511/3,485/31,996，`finish_reason=stop`，47/47 候选决策、身份和证据通过，未使用纠错。
- 诊断结果仅标记 `SAME_MODEL_DIAGNOSTIC`、`independent_review=false`，不形成正式独立共识或 `PASS`。

## Docker 与运行状态

- API：未启动、停止或重启。
- Worker：未启动、停止或重启。
- PostgreSQL：未操作。
- 控制台：未操作。
- 最终是否保持运行：本任务未改变任何服务状态。

## 已知问题与风险

- 语义规划真实阶段按用户限定的真实复验范围未调用；已通过合成测试和工作流测试，后续真实里程碑再验证。
- 当前独立第二模型和 MiniMax 路由问题仍未解决；本次同模评审不能替代正式独立共识。
- 952 字符样本抽取出 47 个事实，说明高召回结果仍可能需要后续业务粒度和重复事实回归，但本次不新增单文档语义冲突检测。
- 全仓 pytest、Docker、OCR 和大型真实合同测试按要求未执行。

## 下一步建议

1. 在独立模型可用后，仅验证语义规划/事实评审的独立模型门，不重复本次 DeepSeek 抽取。
2. 使用合成或脱敏多文件样本验证语义规划的跨文件证据归属、批次数量和 AST 规则覆盖。
3. 里程碑验收前再安排全仓、Docker、OCR 和大型真实合同测试。

## 下一会话首先阅读

- `docs/progress/20260825-112900_compact-extraction.md`
- `docs/progress/20260825-105143_deepseek-review-schema.md`
- `app/adapters/llm/openai_client.py`
- `app/adapters/llm/schemas.py`
- `app/draft_review/facts.py`
- `app/workflows/draft_review.py`
- `scripts/llm_model_eval.py`

## 交接摘要

紧凑事实抽取和内部语义规划已实现，完整结果接口保持兼容。合成定向测试 55 个通过；952 字符样本使用 4096 上限完成 1 次抽取和 1 次同模诊断评审，均 `stop`、严格 Schema、证据通过，抽取 47 个唯一事实，未再截断。未提交、未推送、未操作 Docker/OCR/数据库；正式独立共识仍待第二模型。
