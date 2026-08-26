# DRAFT_REVIEW 单模型交付闭环记录

## 状态

`SUCCEEDED`

本阶段已完成单模型交付模式下的真实三文件 DRAFT_REVIEW 闭环。默认关闭事实评审、映射复核和语义规划；抽取事实由程序执行来源、位置、证据、身份、置信度及数值规范化校验，映射由单次模型调用产生后再经过程序规则校验。

## 实际架构与配置

- 新增内部开关：`LLM_FACT_REVIEW_ENABLED=false`、`LLM_MAPPING_REVIEW_ENABLED=false`、`LLM_SEMANTIC_PLAN_ENABLED=false`、`LLM_REQUIRE_INDEPENDENT_MODEL=true`。评审开关重新开启时，独立模型安全门仍生效；同模型双调用不描述为独立共识。
- `qualified_fact_refs(require_review=False)` 作为单模型交付的统一事实消费门，只接受文件身份、证据位置、quote、事实身份、置信度和程序数值规范化均通过的事实；不读取旧事实评审决定。
- `FACT_MAPPING` 只消费合格目标事实和合格辅助事实。`MATCH` 进入事实矩阵，数值等价性仍由程序和 `Decimal` 判断；不确定映射只可形成不确定风险，不能形成通过项；非法事实、文件或位置引用直接失败。
- `FACT_MAPPING_REVIEW`、`FACT_REVIEW`、`SEMANTIC_PLAN` 在本次交付路径均未调用。正式结果元数据保持兼容的 `independent_review=false`、`review_mode=NOT_RUN`，对外限制说明使用非技术化文字。
- checkpoint 版本保持 `profile-v2`、`numeric-v2`、`text-v4`，按文件哈希、稳定批次和 payload 摘要复用；事实评审 checkpoint 保留但单模型交付不读取。
- 真实诊断器继续使用宿主机 PostgreSQL `127.0.0.1:15432`，容器配置仍使用 `postgres:5432`；`trust_env=False` 保持开启。未修改公开接口、任务参数、业务表、公开结果 Schema 或 `FINAL_COMPARE`。
- 诊断汇总现在分别统计正式风险项建议、模型直接建议和 fallback 建议，避免把模型返回空建议列表误报为正式结果没有建议。

## 网关与安全边界

- 本次任务跳过已完成的部署级结构化输出探测，直接使用已验证的 `json_schema`；`probe_http_calls=0`。
- 成功任务未记录合同正文、quote、完整模型响应、Key、授权头、签名 URL、模型隐藏配置或位置明细；JSONL 仅保存长度、状态、错误类别和聚合计数。

## 离线验证

执行的定向测试：

```text
.venv\Scripts\python.exe -m pytest tests/unit/test_core.py tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py -q
```

结果：`140 passed, 1 warning`。

本次诊断汇总字段修正后执行：

```text
D:\AI_study_env\miniconda3\Scripts\ruff.exe check --output-format concise scripts\draft_review_real_diagnostic.py
.venv\Scripts\python.exe -m compileall -q app scripts
git diff --check
```

结果：Ruff、compileall 和 `git diff --check` 均通过。未运行全仓测试、OCR、Docker 全验收、五文件或 200 页任务。

## 真实三文件验证

成功验收使用固定的融资租赁合同（回租）合同、其模板和项目方案确认函；旧 `.real-diagnostic-temp/` 内容、锁和指标全部保留，新任务使用唯一 JSONL：

`.real-diagnostic-temp/single-model-recovery-20260826-172503.jsonl`

成功任务安全汇总：

- 耗时约 `108.015` 秒；HTTP 调用 `2`，逻辑调用 `2`；新增调用为 `FACT_MAPPING=1`、`AI_ADVICE=1`。
- checkpoint 复用 `64`、保存 `64`：目标合同 `profile/numeric/text` 完成批次 `30`，辅助资料完成批次 `31`，模板不进入动态事实批次；三份文件恢复次数均为 `0`。
- `FACT_REVIEW=0`、`FACT_MAPPING_REVIEW=0`、`SEMANTIC_PLAN=0`、网关 probe `0`、OCR `0`；目标合同和模板没有新增抽取请求，辅助资料仅有建议/映射所属的 1 次业务请求记录。
- 工作流完成：下载、解析、模板比较、事实抽取、事实映射、程序数值校验/正式差异、建议生成、建议阶段和结果持久化。
- 结果 Schema 校验通过，结论为 `RISK_FOUND`；正式差异 `39`，事实矩阵消费映射 `3`，事实通过 `3`，事实缺失 `295`，事实冲突 `0`，事实不确定 `0`，双侧证据对 `21`。
- AI 建议调用成功；模型直接返回的建议条目为 `0`，现有正式结果 fallback 已为每个 `risk_item` 补齐一句 `analysis_advice`，建议覆盖率为 `100%`。原任务 JSONL 生成于诊断字段修正前，未保存结果正文或风险项明细，因此无法从旧安全汇总还原正式风险总数；后续诊断将同时记录 `risk_item_count`、正式建议数、模型建议数和 fallback 数。

另有一次运行前置参数错误：第一次命令传入的源任务 ID 少了末尾 `e`，未命中 checkpoint，发出 `3` 次概况 HTTP/逻辑调用后在 `FACT_EXTRACTION` 返回 `DYNAMIC_CHECK_INCOMPLETE`。该次不是业务验收任务，未复用其结果；随后使用正确源任务 ID 和新的唯一任务完成本记录中的成功验收，未原样重跑失败任务。

## 当前未完成项与下一步

- 本阶段业务闭环已完成；未执行完整交付验收项目：全仓测试、前端构建、Alembic 在线复核、Docker 构建与 Compose 冒烟、API/Worker/控制台验证、五文件、OCR 和甲方内网部署联调。
- 下一步进入独立部署验收阶段；继续保留现有 checkpoint 和 `.real-diagnostic-temp/`，不再扩展单模型业务架构。
