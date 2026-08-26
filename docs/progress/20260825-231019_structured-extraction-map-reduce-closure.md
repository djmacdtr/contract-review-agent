# 结构化抽取 Map–Reduce 稳定性闭环记录

日期：2026-08-25

## 实际架构与配置

- 每份输入文档先执行一次 `CompactDocumentOverview` 概况抽取。模板只执行概况抽取，继续由确定性模板检查负责事实比较。
- 目标合同和辅助资料按段落、表格行规划事实单元；超宽表格行按确定性列组拆分，并保留 `table_index/row/column`。结构单元使用稳定 `unit_id`，叶批次使用由文件和单元集合计算的稳定 `batch_id`。
- LangGraph 抽取使用 `Send` 将批次发送到独立 Map task；Reduce 负责批次去重、来源和证据回填、覆盖率、数值候选分类及身份冲突检查。只有 Reduce 通过后才允许进入跨文档工作流。
- 事实模型响应不再重复文件身份、证据原文或完整文档概况；程序按输入位置回填 `source_file_id`、证据文本和稳定事实身份。
- 实际配置：最终事实 payload 上限 12,000 字符、每批数值候选 48、每批事实 24、模型最大输出 6,144 tokens、预估输出上限 4,800 tokens、单任务抽取并发 2、最大拆分深度 8、单文档绝对逻辑调用上限 128。
- 预估输出 token 使用固定公式：`ceil((256 + 最大事实数 × 220 + 数值候选数 × 48) / 2)`。动态预算为 `N + max(2, ceil(N × 30%))`，另受 128 次绝对上限约束。
- 新增 `InMemoryExtractionCheckpointStore` 和 `ExtractionCheckpointStore` 内部接口，验证了稳定批次的幂等写入、恢复读取和输入摘要变化拒绝复用；本阶段未接入 PostgreSQL Checkpointer，也未新增服务或数据库表。

## 网关结构化输出探测

使用完全合成的极小请求分别探测 `json_schema` 和 `json_object`。两种模式均返回 HTTP 200、`finish_reason=stop`、合法 JSON 且通过探测 Schema；探测确认了实际返回结构，不只检查 HTTP 状态码。因此本次抽取选择 `json_schema`。

探测器和真实诊断客户端均使用 `trust_env=False`，安全指标不写入 Key、授权头、合同正文或完整模型响应。真实任务诊断器内另执行了 2 次合成探测请求；此前单独执行的探测命令也只发送合成内容。

## 错误分类与恢复实现

- `finish_reason=length` 映射为截断错误，不做结构纠错，由 Map–Reduce 二分批次。
- `finish_reason=stop` 的非法 JSON 只做一次严格纠错；仍失败时交给批次二分，禁止 Markdown 围栏和宽松 JSON 修复。
- Schema 和证据错误仅发送安全字段摘要纠错一次；证据位置、原值和数值候选分类在程序侧严格回查。
- timeout、502、503 仅在同一 HTTP 调用内有限退避重试一次；鉴权和其他非临时错误直接失败。
- 单元仍失败、批次覆盖不完整、数值候选未分类、批次身份冲突或恢复预算耗尽均返回 `DYNAMIC_CHECK_INCOMPLETE`，不生成半成品正式差异、通过项或建议。

## 离线测试与检查

实际执行的相关 pytest 合并命令覆盖：

```text
tests/unit/test_draft_facts.py
tests/unit/test_openai_llm_client.py
tests/unit/test_draft_review_workflow.py
tests/unit/test_draft_review_real_diagnostic.py
tests/unit/test_llm_model_eval.py
tests/unit/test_llm_schemas.py
tests/unit/test_draft_review_checkpoints.py
```

结果：`115 passed, 1 warning`。警告来自依赖的 LangGraph checkpointer 弃用提示，不影响测试结果。

另外通过：

- 相关 `ruff check`：通过。虚拟环境没有 `ruff` Python 模块，因此使用环境中可用的 Ruff 可执行文件完成同一检查。
- `\.venv\Scripts\python.exe -m compileall -q app scripts`：通过。
- `git diff --check`：通过；仅有 Git 关于工作树换行符的提示。

新增测试覆盖了 Send Map–Reduce 成功和分片恢复、最小失败门、严格证据校验、非法 JSON/截断/HTTP 重试、Schema 摘要纠错、表格列组、候选分类、身份冲突、断点幂等和并发诊断指标归属。

## 一次真实三文件任务

本次唯一真实任务使用新的安全文件：

- `.real-diagnostic-temp/draft-review-structured-map-reduce-20260825-223230.jsonl`
- `.real-diagnostic-temp/draft-review-structured-map-reduce-20260825-223230.lock`

旧锁、旧指标和 `.real-diagnostic-temp/` 中原有内容均保留，未重跑真实任务。

安全总计：

- 逻辑调用：80 次，其中 3 次文档概况、74 次目标合同事实批次、3 次辅助资料事实批次。
- HTTP：144 次，其中诊断器内合成探测 2 次，真实任务请求 142 次。
- 真实任务 HTTP 按可核对的请求起始事件计：目标合同 137 次、模板概况 1 次、辅助资料 4 次；并发响应完成事件的文件归属受旧采集器共享上下文影响，不能把旧的 `completed` 字段作为可靠依据。
- 目标合同计划 74 批，辅助资料计划 3 批，模板事实批次为 0。恢复批次实际发出 0 次：首轮目标合同存在大量失败批次，Reduce 在恢复路由发出前判定恢复预算不足并安全终止。
- 失败分类：目标合同记录 55 次非法 JSON、6 次证据位置/事实回查失败；辅助资料记录 1 次证据位置/事实回查失败。所有已记录模型响应为 HTTP 200、`finish_reason=stop`，未出现 length、timeout、502 或 503。

工作流阶段：

- 已完成：下载、解析、模板确定性比较、三份文档概况抽取、首轮事实 Map。
- 首个失败阶段：`FACT_EXTRACTION` 的 Reduce/恢复门，最终安全错误为 `DYNAMIC_CHECK_INCOMPLETE`。
- 未进入：跨文档事实映射、语义规划、数值校验、正式 `FINAL_COMPARE` 结果组装、AI 建议和结果持久化。
- 正式差异数量：0；双侧证据数量：0；AI 建议数量：0。诊断结果为空，未发布不完整正式结果。

本次真实任务启动时旧诊断采集器仍使用共享的当前调用字段，在抽取并发为 2 时造成部分响应完成事件的文件名串写。实现已随后改为 async context 绑定，并新增并发采集测试；本次任务不因该诊断问题重跑，最终报告以上述可核对的逻辑调用起始事件、HTTP 请求起始事件和安全总计为准。

## 未完成项与下一步

- 真实网关虽然通过了合成 `json_schema` 探测，但复杂事实批次仍出现较多非法 JSON 和证据位置失败；需要在下一次受控运行前，基于安全错误分类继续定位复杂 Schema 与模型输出的兼容性，不得读取或打印完整响应。
- 本次目标合同首轮计划 74 批，失败批次超过其 `max(2, ceil(N × 30%))` 恢复预算，因此没有发出恢复批次；这是安全终止而不是不受控扩容。
- PostgreSQL Checkpointer 仍只完成内部接口和内存离线验证，后续可在不新增业务表、不引入 Redis/Celery/微服务的前提下接入既有任务事件持久化。
- 下一步应先审查网关复杂 Schema 的安全摘要指标和批次规划参数，再由新的唯一锁/指标文件执行单独受控验证；本轮真实失败后没有原样重跑。
