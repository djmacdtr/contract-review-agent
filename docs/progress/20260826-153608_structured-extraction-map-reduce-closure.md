# DRAFT_REVIEW 结构化抽取 Map–Reduce 闭环记录

## 状态

`PARTIAL_BLOCKED_AT_SEMANTIC_PLAN`

数据库基础设施已恢复，checkpoint 迁移和读写探测通过；抽取、事实评审、事实映射和映射复核均已完成或复用。修复后的恢复任务在 `SEMANTIC_PLAN` 阶段仍因模型语义事实引用校验失败并伴随一次非法 JSON 纠正失败而止损，因此没有发布半成品结果。

## 实际架构与配置

- PostgreSQL 继续使用现有业务库和现有 checkpoint 表；宿主机仅通过 `127.0.0.1:15432` 访问，Compose 容器仍使用 `postgres:5432`。已执行 `alembic upgrade head`，`0003_extraction_checkpoint` 生效。
- 文档概况、数值抽取、文本抽取和事实评审分别使用独立 checkpoint；版本为 `profile-v2`、`numeric-v2`、`text-v4`、`fact-review-v1`。成功结果按文件哈希、批次摘要和版本幂等复用。
- 目标合同文本链消费模板差异候选；辅助资料按动态全文事实规划；数值链继续扫描全文。语义规划新增紧凑内部线协议：模型只返回当前批次 `fact_ids`、概念和限定事实 AST，证据位置由程序从已验证事实回填。
- 生效配置：目标文本候选 8、辅助文本单元 16、数值有效候选 24、最终 payload 12,000 字符、抽取并发 2、语义事实批次 8、最大拆分深度 8、`max_output_tokens=4096`、抽取绝对上限 128；诊断任务总逻辑调用安全上限 50。`trust_env=False` 保持开启。
- 未修改公开接口、任务参数、业务表、公开结果 Schema 或 `FINAL_COMPARE` 确定性比较逻辑；未引入 Redis、Celery、微服务或 PostgreSQL Checkpointer 依赖。

## 网关结构化输出探测

- 生产任务跳过部署级 12 次复杂探测，继续使用此前合成探测和候选 Canary 已验证的 `json_schema` 模式；本批三文件任务 `probe_http_calls=0`。
- 候选 Canary 曾达到 3/3 合法 JSON、Schema 和证据通过；本次新任务中的抽取/评审/映射 HTTP 响应均为 `200`、`finish_reason=stop`。失败发生在语义规划模型结果的严格引用校验及其一次纠正响应的非法 JSON，不是网关 HTTP 或数据库连接失败。
- 指标仅保存响应长度、JSON 边界、错误类别和计数，不保存 Key、授权头、合同正文、quote 或完整模型响应。

## 离线验证

执行：

```text
.venv\Scripts\python.exe -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py tests/unit/test_structured_extraction_v2.py tests/unit/test_core.py -q
ruff check app/adapters/llm app/core/config.py app/draft_review/facts.py app/draft_review/extraction.py app/workflows/draft_review.py scripts/draft_review_real_diagnostic.py tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py tests/unit/test_structured_extraction_v2.py tests/unit/test_core.py
.venv\Scripts\python.exe -m compileall -q app scripts
git diff --check
```

结果：`140 passed, 1 warning`；Ruff、compileall 和 `git diff --check` 通过。

## 数据库探测

- `0003_extraction_checkpoint` 已升级成功。
- 内存和 SQLAlchemy checkpoint Store 的写入、幂等写入、按 `source_task_id` 读取和值一致性探测通过；合成探测记录按原样保留。

## 真实验证

### 新三文件任务

使用的文件仍为融资租赁合同（回租）、其模版和项目方案确认函；没有增加文件。旧 `.real-diagnostic-temp` 内容、锁和指标均保留。

第一轮新任务：`structured-map-reduce-full-20260826-153500.jsonl`

- checkpoint 复用 70，保存 92；HTTP 29，逻辑调用 29；网关探测 0。
- 事实评审 22 次、事实映射 1 次、映射复核 1 次、语义规划已返回 3 个分片后出现 `LLM_SCHEMA_INVALID`，任务止损。
- 诊断器汇总批次：目标合同 completed 30、恢复 0；项目方案确认函 completed 31、恢复 0；模版不进入动态批次。正式差异、双侧证据和 AI 建议均为 0。

第一次 checkpoint 恢复：`structured-map-reduce-resume-20260826-152700.jsonl`

- 复用 92，HTTP 7，逻辑调用 7；事实映射 1、映射复核 1、语义规划 5。
- 在第三个语义分片出现 `LLM_SEMANTIC_PLAN_INVALID`，没有进入正式结果。

第二次修复后恢复：`structured-map-reduce-resume-20260826-153000.jsonl`

- 复用 92，HTTP 7，逻辑调用 6；事实映射 1、映射复核 1、语义规划 5（含一次结构纠正请求）。
- 首个失败阶段仍为 `SEMANTIC_PLAN`：安全指标记录 `LLM_INVALID_JSON`，同时保留前置语义引用失败子码；其余并发批次按规则取消。没有进入数值校验、正式差异或建议生成。

### 各阶段完成状态

| 阶段 | 状态 |
| --- | --- |
| 下载、解析、模板比较 | 完成 |
| profile/numeric/text 抽取 | 成功 checkpoint 已复用 |
| FACT_REVIEW | 成功 checkpoint 已复用或完成 |
| FACT_MAPPING | 完成 |
| FACT_MAPPING_REVIEW | 完成 |
| SEMANTIC_PLAN | 未完成，当前阻塞 |
| NUMERIC_VALIDATION | 未进入 |
| FINAL_COMPARE/正式结果 | 未发布 |
| AI_ADVICE | 未进入 |

正式差异数量、双侧证据数量、AI 建议数量均为 `0`，因为严格门禁止生成不完整结果。

## 当前未完成项与下一步

1. 当前首个新业务阻塞为语义规划模型的批次内事实引用稳定性；需要下一阶段继续缩小语义批次或进一步简化语义线协议，并保持程序拥有证据回填及严格拒绝规则。
2. 后续任务应沿同一文件哈希和版本复用现有 92 个以上成功 checkpoint；不得重跑已成功的概况、数值、文本和事实评审。
3. 语义规划全部 Reduce 通过后，才允许进入数值校验、正式差异和 AI 建议；在此之前不运行五文件、OCR、Docker 全链路或全仓验收。

