# DRAFT_REVIEW 结构化抽取快速止损闭环记录

日期：2026-08-26

## 实际架构与配置

- 文档概况仍为每份输入文档一次调用；模板不进入事实抽取。
- 事实抽取改为同一稳定批次下的两条内部链：数值候选按 `candidate_index` 分类，非数值事实按 `unit_id + quote` 回查。文件身份、原值、证据和正式位置均由程序回填。
- 普通段落、表格行和必要的表格列组作为结构单元；`batch_id` 使用文件 SHA-256、结构单元集合和 `structured-map-reduce-v2` 生成。
- LangGraph 每波使用 `Send` 扇出，波次归约后才调度下一波；首波成功率低于 90% 或连续两次格式/证据失败立即熔断。
- Reduce 校验结构单元覆盖率、数值候选恰好分类、quote/位置回查、重复身份和冲突。
- 生产默认值：payload 12,000 字符；数值候选 24；非数值事实 12；最大输出 4,096；单条抽取超时 180 秒；任务并发 2；波次 6；恢复预算 `max(2, ceil(N×20%))`；目标合同逻辑上限 40；三文件事实抽取总上限 50。
- 预检对目标合同得到 542 个结构单元、361 个带业务上下文的数值候选、17 个配对批次；参考函得到 26 个结构单元、19 个数值候选、1 个配对批次。无业务上下文的裸序号/目录数字不进入候选；业务类型数字仍全部进入候选并要求分类。
- 已加入内存 checkpoint 和 SQLAlchemy PostgreSQL checkpoint Store，迁移 `0003_extraction_checkpoint` 创建内部 `extraction_checkpoint` 表；不修改业务表，不新增 Redis/Celery/微服务。

## 网关结构化输出探测

- 新探测使用完全合成输入，不包含合同内容，并以 `trust_env=False` 建立 HTTP 客户端。
- `json_schema` 和 `json_object` 各执行数值候选 Schema、非数值事实 Schema 各 3 次，共 12 次合成 HTTP 请求。
- 两种模式均达到 3/3 合法 JSON、Schema 通过、`finish_reason=stop`、非空、无代码围栏；选择顺序最终选择 `json_schema`。
- 安全指标仅包含状态码、finish reason、content/reasoning 字符数、空内容/代码围栏、JSON 边界、解码错误类别和位置比例、Schema 摘要；不保存 Key、授权头、合同正文或完整响应。
- 旧简单探测保留，但不再用于自动启用生产模式。

## 离线验证

已执行：

```text
python -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py tests/unit/test_llm_schemas.py tests/unit/test_draft_review_checkpoints.py tests/unit/test_structured_extraction_v2.py tests/unit/test_llm_structured_output_probe.py -q
```

结果：最终回归 124 passed，1 个既有 LangGraph 弃用警告。

Ruff、`python -m compileall -q app scripts` 和 `git diff --check` 均通过。Alembic 离线 `upgrade head --sql` 通过并包含新 checkpoint 表；`alembic check` 因本地 PostgreSQL 主机未启动/不可解析而未能连接，未修改数据库。

## 真实 Canary

- 新锁：`.real-diagnostic-temp/draft-review-v2-canary-20260826-093000.lock`
- 新指标：`.real-diagnostic-temp/draft-review-v2-canary-20260826-093000.jsonl`
- 复杂探测 12 次合成 HTTP；Canary 5 次业务调用，5/5 成功。
- 代表单元覆盖普通段落、长段落、数值密集单元、普通表格行和宽表格行；Canary 禁止结构纠错，仅保留客户端单次网络瞬时重试能力。

## 唯一一次新的完整三文件任务

- 新锁：`.real-diagnostic-temp/draft-review-v2-full-20260826-094500.lock`
- 新指标：`.real-diagnostic-temp/draft-review-v2-full-20260826-094500.jsonl`
- 合成探测：12 次 HTTP；任务阶段：15 次 HTTP、15 次逻辑调用（3 次文档概况、6 次数值链、6 次文本链）。
- 首个失败阶段：目标合同 `TEXT_FACT_EXTRACTION`。首波 6 个配对批次的文本证据校验失败，触发首波成功率熔断；未进入恢复二分、事实 Reduce、跨文档映射、语义规划、数值校验、正式差异或 AI 建议。
- 批次状态：目标合同计划 17 个配对批次、首波发出 6 个、成功归约 0 个、恢复 0 次；模板计划 0 个事实批次且概况成功；参考函计划 1 个配对批次但在目标首波熔断前未发出。文档概况 3/3 完成，事实 Reduce 0/2 个动态事实文档完成。
- 真实任务总计：27 次 HTTP（含 12 次合成探测）、15 次逻辑调用；正式差异 0；双侧正式证据 0；AI 建议 0；状态 `DYNAMIC_CHECK_INCOMPLETE`。
- 未读取或输出模型响应正文，未原样重跑该失败任务。旧 `.real-diagnostic-temp/` 内容、旧锁和旧指标均保留。

## 当前未完成项与下一步

- 当前真实首因是大批次非数值事实 quote/证据回查失败，需要在离线 Mock 中补充“文本链证据失败后按批次细分”的覆盖，并进一步缩小文本链的单批结构单元密度；不能通过宽松 JSON 修复或放宽证据校验解决。
- 完整三文件闭环尚未达到门槛，因此没有发布正式结果，也没有执行五文件、200 页、OCR、Docker 或全仓验收。
- PostgreSQL checkpoint 的接口、模型、迁移和内存语义已完成；待数据库可用后执行真实 `upgrade/check` 和 Worker 恢复验收。
- 本阶段不再重跑失败的完整三文件任务；下一次真实运行必须使用修正后的新任务，并仅复用同文件 SHA、同抽取版本且已验证成功的 checkpoint。
