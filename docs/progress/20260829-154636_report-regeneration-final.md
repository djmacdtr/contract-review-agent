# 任务进度：受控报告再生成与跨文件证据补全

## 基本信息

- 时间：2026-08-29 15:46:36 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / TEST
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：5af48ac
- 工作树状态：dirty；保留本会话前已有的 DRAFT_REVIEW、Advice、页码和诊断改动

## 用户目标

以成功任务 `tsk_01M161GFY6Q7YSP07R877XQM2B` 为只读来源，创建一个新的内部报告再生成任务，复用三份文档级抽取快照，仅重跑跨文件映射、规则、Advice 和真实 DOCX 页码补全，并保留来源报告作为回退版本。

## 本次完成

- 新增私有 `TaskService.create_report_regeneration()`：校验来源成功状态、结果 Schema、三份文件和文档级快照；按 `_report_regeneration_source_task_id` 做幂等门禁；创建新任务、新文件 ID和“报告再生成”事件。
- 新增 `ReportRegenerationWorkflowExecutor`：本地文件直读、快照缺失时禁止事实抽取模型调用、来源结果文件身份重映射、模板风险/差异/通过项基线保留、动态映射/规则/Advice 复用、严格 DOCX 页码和公开证据页码范围校验。
- 新增宿主机运维脚本 `scripts/regenerate_draft_report_host.py`：固定来源任务和脱敏文件目录，安全预检、单次任务创建、宿主机 Worker 注入及安全统计输出。
- 新增文件身份重映射、未知/旧文件身份拒绝、本地文件直读适配器测试。
- 只读确认来源为 `SUCCEEDED/DRAFT_REVIEW`，存在 3 个 `document-extraction-v1` 快照，无既有报告再生成子任务、无其他 PENDING/RUNNING 任务。
- 唯一再生成任务已创建：`tsk_01M167E69YV0MGNHENB9HW10DG`。来源任务和来源结果未修改。

## 修改文件

- `app/services/task_service.py`：新增内部报告再生成任务创建方法和幂等门禁。
- `app/workflows/report_regeneration.py`：新增快照守卫、文件身份映射、结果基线合并和严格页码门禁。
- `scripts/regenerate_draft_report_host.py`：新增宿主机单次再生成运维入口。
- `tests/unit/test_report_regeneration.py`：新增文件身份映射和本地文件安全校验测试。
- `docs/progress/20260829-154636_report-regeneration-final.md`：记录本轮实现和验收状态。

## 接口、数据和配置变化

- API：无公开路由、请求模型、响应结构变化；新增方法仅供内部脚本调用。
- 数据库/迁移：无 Schema 或迁移变化；新任务复用现有 `CheckTask`、`TaskFile`、`TaskResult` 和 checkpoint 表。
- 配置：未修改 `.env`、API Key、正式下载白名单或 OCR 配置。
- 兼容性：来源结果保持只读；新任务使用新的 `TaskFile.file_id`，风险/差异/事实业务 ID不主动改写。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `conda run --no-capture-output -n contract-review-agent-py312 python -m pytest tests/unit/test_report_regeneration.py tests/unit/test_draft_review_workflow.py -q` | 通过 | 58 passed，1 warning |
| `conda run --no-capture-output -n contract-review-agent-py312 python -m pytest tests/unit/test_report_regeneration.py -q` | 通过 | 3 passed，1 warning |
| `ruff check app/workflows/report_regeneration.py app/services/task_service.py scripts/regenerate_draft_report_host.py tests/unit/test_report_regeneration.py` | 通过 | All checks passed |
| `python/conda compileall`（本轮变更 Python 文件） | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅显示既有换行格式提示，无 diff 错误 |
| `docker compose ps` | 通过 | API healthy，PostgreSQL running/healthy，Docker Worker 未运行 |
| 唯一宿主机再生成脚本 | 未完成 | 任务创建后在执行器启动前触发 `REPORT_REGENERATION_SETUP_ERROR`；无任务领取、无 OCR/映射/Advice/页码 HTTP 调用 |

## Docker 与运行状态

- API：运行中且 healthy。
- Worker：Docker Worker 未运行；宿主机 Worker 未持续运行。
- PostgreSQL：运行中且 healthy。
- 控制台：任务列表路径 `/console/#/tasks`；本轮未完成报告页验收。
- 最终是否保持运行：保留 API/PostgreSQL；未启动持续 Worker。

## 重要决策

- 报告再生成不复用公开 retry 语义，不绕过 retry 幂等门禁；同一成功来源最多一个带 `_report_regeneration_source_task_id` 的子任务。
- 文档级快照缺失时通过 LLM 守卫安全失败，绝不从再生成链路隐式回退到全文事实抽取。
- 页面门禁只接受外部真实 DOCX sidecar 的页码；不使用段落、表格、行列位置或估算页码。

## 已知问题与风险

- 唯一新任务 `tsk_01M167E69YV0MGNHENB9HW10DG` 当前仍为 `PENDING/QUEUED`，`attempt_count=0`；本轮未完成真实报告。
- 首次脚本版本的宿主机设置阶段只输出了泛化的 `REPORT_REGENERATION_SETUP_ERROR`，没有保留底层异常类型；后续若获授权继续运维，应先对该待处理任务做本地设置阶段诊断，不重复创建任务。
- 由于没有执行映射/Advice/页码请求，当前没有新任务的映射调用数、Advice 覆盖率、页码覆盖率、结果统计或控制台报告地址。
- 页面视觉验收由用户负责；本轮未打开报告页。

## 下一步建议

1. 在不创建第二个再生成任务的前提下，定位 `tsk_01M167E69YV0MGNHENB9HW10DG` 的宿主机设置启动异常；先确认本地执行器构造阶段，不调用外部服务。
2. 若明确属于本地启动缺陷，再由用户决定是否使用该同一待处理任务执行唯一宿主机 Worker；出现任何外部失败后停止，不创建新任务。
3. 成功后补写实际安全统计、结果和控制台路径；未成功前不要宣称报告再生成闭环。

## 下一会话首先阅读

- `AGENTS.md`
- `app/workflows/report_regeneration.py`
- `scripts/regenerate_draft_report_host.py`
- `app/services/task_service.py`
- `docs/progress/20260829-154636_report-regeneration-final.md`

## 交接摘要

已实现私有报告再生成任务服务、快照守卫、文件 ID 映射、严格页码门禁和宿主机运维脚本。离线定向测试 58 passed，静态检查通过。唯一任务 `tsk_01M167E69YV0MGNHENB9HW10DG` 已创建但仍 PENDING，首轮启动在领取前发生泛化设置错误，没有发出外部请求；来源成功报告未修改。不要 retry 或新建第二个任务。

## 后续受控诊断与唯一执行结果

- 新增 `--setup-only --task-id tsk_01M167E69YV0MGNHENB9HW10DG` 诊断模式，仅查询数据库并构造本地组件；结果为 `SETUP_OK`，外部调用数为 0，三份文件、TaskFile 映射、解析器、LLM 客户端、工作流执行器和 WorkerRunner 均构造成功。
- 修正脚本失败摘要不得携带 SQLAlchemy `Row` 对象，仅保留 checkpoint 行数等安全字段；未改变任务或来源数据。
- 使用同一待处理任务执行一次：Worker 已领取，`attempt_count=1`，随后在 `SNAPSHOT_PREFLIGHT` 安全停止，错误为 `REPORT_REGENERATION_SNAPSHOT_INCOMPLETE / DOCUMENT_EXTRACTION_CHECKPOINT_MISSING`，`fact_extraction_calls=0`。未发出映射、Advice 或页码外部请求。
- 任务当前为 `FAILED / FACT_EXTRACTION / 75%`；来源任务仍为成功状态且未修改。Docker Worker 保持停止；没有 retry、没有第二个任务、没有重复外部执行。
- 本次未生成新报告、未完成控制台报告页、跨文件证据、Advice 或页码验收；该待处理任务已消耗唯一执行机会，后续需由用户明确授权后再决定是否修复快照加载兼容问题。
