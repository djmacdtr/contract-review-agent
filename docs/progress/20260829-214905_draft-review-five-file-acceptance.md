# 任务进度：五文件 DRAFT_REVIEW 真实验收

## 基本信息

- 时间：2026-08-29 21:49:05 +08:00
- 状态：BLOCKED
- 任务类型：TEST
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`6d8166d`
- 工作树状态：dirty；新增五文件验收脚本和本轮 `.real-diagnostic-temp/` 摘要，`backups/` 保留未跟踪

## 用户目标

通过公开 DRAFT_REVIEW 接口，以一份目标合同、一份模板和三份不同类型辅助资料创建唯一全新五文件任务，使用标准高质量链路完成动态交叉比对、Advice、真实页码和控制台验收。

## 本次完成

- 新增独立的五文件公开验收脚本，使用文件列表保留三个 REFERENCE，不调用内部创建、retry 或再生成入口。
- 完成 Git 基线、服务、任务队列、五份本地文件和内容 SHA 预检。
- 缓存预热后确认 5/5 OCR 解析缓存命中、4/4 DOCX 页码 sidecar 命中、PDF 真实页数来自 OCR 解析缓存；正式任务执行期间 OCR 调用为 0。
- 通过公开 POST 创建唯一新任务 `tsk_01M16W32545DN9NC65XXEPJG1D`，数据库身份确认 5 个新文件、3 个 REFERENCE、`source_task_id=null`、无私有兼容选项。
- 使用宿主机标准 Worker 执行；Docker Worker 在任务执行期间停止。
- 任务在 `FACT_MAPPING` 阶段停止，未进入结果持久化、Advice 或页码补全；按止损规则未 retry、未创建第二任务。
- Docker Worker 已恢复；API、PostgreSQL 和任务队列状态已复核。

## 修改文件

- `scripts/draft_review_five_file_public_acceptance.py`：五文件公开创建、内容缓存预检、宿主机标准 Worker 执行和安全结果摘要。
- `docs/progress/20260829-214905_draft-review-five-file-acceptance.md`：本轮真实验收记录。

## 接口、数据和配置变化

- API：未修改；正式任务仅调用公开 `POST /api/v1/draft-reviews`，之后只 GET 轮询。
- 数据库/迁移：未修改；新增一条正式五文件任务及其执行审计/Checkpoint 数据，历史报告未修改。
- 配置：仅脚本运行时覆盖；`GLM-5.3-Flash`、Mapping `json_schema`、Text/Advice `json_object`、并发 2、页码 sidecar 开启。未改写正式 `.env`。
- 兼容性：新任务未设置 `source_task_id`，未加载旧任务结果、旧业务 ID 或旧事实快照。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m ruff check scripts/draft_review_five_file_public_acceptance.py` | 通过 | 验收脚本无 Ruff 错误 |
| `python -m compileall -q scripts/draft_review_five_file_public_acceptance.py` | 通过 | 编译成功 |
| `git diff --check` | 通过 | 无空白错误 |
| 五文件预检 | 通过 | 五份文件存在且 SHA 唯一；API/ready 200；活动任务 0 |
| OCR/页码缓存审计 | 通过 | OCR 5/5；DOCX sidecar 4/4；PDF 页数缓存 1/1；正式任务 OCR=0 |
| 唯一正式任务 | 阻塞 | `tsk_01M16W32545DN9NC65XXEPJG1D`，`RUNNING→FAILED`，`CROSS_VALIDATE / 80%` |

## 真实任务安全摘要

- JSON 摘要：[five-file-acceptance-20260829.json](D:/work/contract_review/contract-review-agent/.real-diagnostic-temp/five-file-acceptance-20260829.json)
- 任务状态：`FAILED / CROSS_VALIDATE / 80%`
- 首个错误：`failure_stage=FACT_MAPPING`、`failure_code=LLM_OUTPUT_TRUNCATED`
- 安全上下文：`chain=mapping`、`file_id=fil_01M16W3255PTYA688BDDZ2TKAT`、`batch_depth=0`、`unit_count=24`、`request_attempts=1`、`structure_retries=0`
- LLM：68 次 HTTP 请求，全部 HTTP 200；finish reason 为 `length=4`、`stop=64`
- OCR：正式任务 0 次 HTTP 请求
- 任务结果接口：详情 GET HTTP 200；结果 GET HTTP 409，未生成 TaskResult
- 控制台任务路径：`/console/#/tasks/tsk_01M16W32545DN9NC65XXEPJG1D`

## Docker 与运行状态

- API：running，health HTTP 200
- Worker：已恢复 running；启动日志确认结构化输出已启用、模型为 `GLM-5.3-Flash`
- PostgreSQL：running，ready HTTP 200
- 控制台：静态页服务正常；失败任务可在任务列表/详情查看
- 最终是否保持运行：是；Docker Worker 已恢复，宿主机临时 Worker 和文件服务已结束

## 重要决策

- 五文件任务失败后遵守唯一任务止损规则，不调用 retry、不调整 Prompt/Schema/Token、不创建第二任务。
- `LLM_OUTPUT_TRUNCATED` 发生在跨资料 Mapping 的 24 单元批次，当前仅记录事实，不把它误报为 OCR、网络或页码问题。

## 已知问题与风险

- 本轮未完成五文件报告，因此尚无动态风险/通过项、Advice 覆盖率或五文件公开页码覆盖结果。
- 映射首批 24 单元返回截断，需后续专门决定映射输出协议或批次策略；本记录不授权本轮继续外部调用。

## 下一步建议

1. 保留失败任务和 JSON 摘要，先针对 Mapping 截断做离线诊断。
2. 不重复执行本轮五文件任务，除非用户明确授权新的验收轮次。
3. 保持 Docker Worker、API 和 PostgreSQL 当前运行状态。

## 下一会话首先阅读

- `AGENTS.md`
- `scripts/draft_review_five_file_public_acceptance.py`
- `docs/progress/20260829-214905_draft-review-five-file-acceptance.md`

## 交接摘要

五份文件已通过公开接口创建唯一任务 `tsk_01M16W32545DN9NC65XXEPJG1D`。
五份文件 SHA 唯一，OCR 解析缓存和 DOCX 页码 sidecar 已命中，正式执行 OCR=0。
新任务无 source_task_id、无旧任务兼容参数、包含 3 个 REFERENCE。
宿主机 Worker 完成抽取后在 Mapping 24 单元批次收到 `LLM_OUTPUT_TRUNCATED`。
任务已停止为 FAILED/80%，没有 TaskResult、Advice 或页码验收结果。
未 retry、未创建第二任务，Docker Worker 已恢复且 API/PostgreSQL 健康。
