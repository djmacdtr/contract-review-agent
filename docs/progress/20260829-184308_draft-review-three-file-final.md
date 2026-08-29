# 任务进度：三文件全新 DRAFT_REVIEW 高质量链路验收

## 基本信息

- 时间：2026-08-29 18:43:08 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / TEST / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；保留本轮及此前所有未提交修改，未执行 reset、clean、commit 或 push。

## 用户目标

通过公开 DRAFT_REVIEW 接口创建唯一一条全新三文件任务，使用标准工作流完成全新事实抽取、跨文件映射、动态规则、Advice、真实 DOCX 页码和控制台验收；不使用旧任务业务结果、事实快照或恢复身份，仅按文件 SHA 复用 OCR 与页码缓存。

## 本次完成

- 将持久化 DOCX 页码 sidecar 缓存接入标准 `DocumentParsingRouter`；缓存按文件 SHA 和现有 sidecar 版本读取并重绑定当前任务文件 ID。
- 标准起草解析最多并行 2 份文件；sidecar 命中时不调用 OCR，未命中时才走现有 OCR 缓存/外部解析并回写 sidecar。
- 将标准 DRAFT_REVIEW 的公开页码补全改为严格检查，仅要求公开差异两侧和关联风险证据具备合法物理页码；内部事实结构不参与门禁。
- 新增仅用于本轮验收的公开 API/标准 Worker 脚本 `scripts/draft_review_three_file_public_acceptance.py`，不读取旧任务结果或事实 checkpoint。
- 执行唯一全新公开任务并保留其失败诊断；没有 retry、第二任务或旧任务修改。

## 修改文件

- `app/documents/page_location_cache.py`：新增复用现有 `ExtractionCheckpoint` 表的内容寻址 sidecar 缓存。
- `app/documents/router.py`：标准 DOCX 路由读取/写入 sidecar，并将三文件解析并发限制为 2。
- `app/documents/page_locations.py`：新增公开证据页码覆盖校验。
- `app/workflows/draft_review.py`：标准工作流接入 sidecar 缓存，解析要求覆盖全部 DOCX，`page_enrich` 使用严格公开门禁。
- `tests/unit/test_document_router.py`、`tests/unit/test_docx_page_locations.py`：sidecar 命中、外部解析回写和公开页码门禁测试。
- `scripts/draft_review_three_file_public_acceptance.py`：公开 API 创建、宿主机标准 Worker 执行及安全摘要脚本。
- `docs/progress/20260829-184308_draft-review-three-file-final.md`：本次实现和唯一外部验收记录。

## 接口、数据和配置变化

- API：未新增或修改公开接口；唯一任务通过 `POST /api/v1/draft-reviews` 创建。
- 数据库/迁移：未新增表或迁移；页码 sidecar 复用 `ExtractionCheckpoint`，来源任务和旧结果未修改。
- 配置：未改写正式 `.env`；宿主机 Worker 仅使用运行时覆盖：本地 HTTP 白名单、页码启用、LLM 并发 2、抽取并发 2、HTTP 重试 1、模型 GLM-5.3-Flash、Text/Advice `json_object`。
- 兼容性：标准新任务未设置 `source_task_id`，options 无旧恢复标记；事实抽取仍按当前文件重新执行。历史 retry/report-regeneration 代码未删除且未被本次路径调用。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `pytest tests/unit/test_document_router.py tests/unit/test_docx_page_locations.py` | 通过 | 31 passed |
| `pytest tests/unit/test_draft_review_workflow.py tests/unit/test_draft_facts.py tests/unit/test_result_advice.py` | 通过 | 112 passed |
| 变更范围 Ruff | 通过 | 所有检查通过 |
| 相关 Python compileall | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅保留既有换行提示 |
| `npm run test:format` | 通过 | 前端格式测试通过 |
| `npm run typecheck` | 通过 | 无类型错误 |
| `npm run build` | 通过 | Vite 构建完成，仅有 bundle size 提示 |
| 唯一全新任务 | 失败并停止 | `DYNAMIC_CHECK_INCOMPLETE`，首个安全失败为 `FACT_EXTRACTION / numeric / LLM_OUTPUT_TRUNCATED` |

## 唯一正式任务结果

- 新任务 ID：`tsk_01M16HJ4ZB4ZXBEG2FKRZF7XSB`
- 创建方式：公开 `POST /api/v1/draft-reviews`，HTTP 202；未调用内部创建、retry 或再生成入口。
- 身份：`source_task_id=null`；无旧兼容 options；三条 `TaskFile` 均为新 ID。
- Worker：宿主机标准 `WorkerRunner` + `DraftReviewWorkflowExecutor`，已领取 1 次。
- 缓存预检：OCR 解析缓存 3/3，DOCX 页码 sidecar 3/3；OCR HTTP 调用 0。
- LLM：HTTP 200 共 69 次，`finish_reason=stop` 52 次、`length` 17 次；映射尚未开始，Advice 尚未开始。
- 失败：阶段 `FACT_EXTRACTION`，链 `numeric`，`batch_depth=2`，`unit_count=1`，`failure_code=LLM_OUTPUT_TRUNCATED`；未执行重试或第二任务。
- 结果：未持久化正式报告，因此尚未完成动态跨文件结果、Advice 和页码报告验收。

## Docker 与运行状态

- API：`contract-review-api-1` healthy，端口 `127.0.0.1:8000`。
- Worker：宿主机 Worker 已停止；Docker Worker 已恢复运行并处于空闲状态。
- PostgreSQL：`contract-review-postgres-1` healthy，端口 `127.0.0.1:15432`。
- 控制台：任务列表路径 `/console/#/tasks`；新任务可见，报告路径因任务失败不可用。
- 最终是否保持运行：API、PostgreSQL 和正式 Docker Worker 保持运行；临时宿主机文件服务已关闭。

## 重要决策

- 页码 sidecar 只作为公开展示证据的物理页绑定，不参与全量内部事实结构覆盖；公开差异和关联风险证据缺页仍严格失败。
- 本次真实任务已按全新任务边界执行；即使失败，也不使用旧任务 checkpoint 或恢复入口继续推进。
- 按用户止损规则，Numeric 叶子截断不在本轮修复或重跑；不通过修改门禁掩盖该失败。

## 已知问题与风险

- 全新任务在事实抽取 numeric 叶子批次遇到 `LLM_OUTPUT_TRUNCATED`，尚未进入跨文件映射、Advice 或结果页码阶段。
- 因任务失败，不能宣称三文件高质量链路或控制台报告闭环完成。
- sidecar 严格公开覆盖逻辑已离线验证，但未被本次失败任务运行到 `page_enrich`。

## 下一步建议

1. 以本次安全错误为止点，单独评估 Numeric 叶子截断恢复策略；未经新的明确授权不要 retry 此任务或创建第二任务。
2. 如需继续正式验收，先针对该叶子批次做离线/定向修复，再按全新任务唯一性重新规划生命周期。
3. 保持现有 Docker Worker、API、PostgreSQL 和工作树状态，避免影响历史报告。

## 下一会话首先阅读

- `AGENTS.md`
- `README.md`
- `app/documents/router.py`
- `app/documents/page_locations.py`
- `app/workflows/draft_review.py`
- 本文件及 `docs/progress/20260829-175647_draft-review-delivery-convergence.md`

## 交接摘要

- 标准公开三文件任务已唯一创建，任务 ID 为 `tsk_01M16HJ4ZB4ZXBEG2FKRZF7XSB`。
- 新任务无来源任务、无旧兼容标记、三份文件 ID 全新。
- OCR 缓存和页码 sidecar 均 3/3 命中，OCR 调用 0。
- 宿主机标准 Worker 执行 69 次 LLM HTTP 200 后，在 Numeric 叶子 `LLM_OUTPUT_TRUNCATED` 失败。
- 未调用 retry，未创建第二任务，未修改旧报告。
- API/PostgreSQL/Docker Worker 已恢复运行；临时文件服务已关闭。
- 页码 sidecar 标准路由接入和严格公开页码门禁已完成并通过定向测试。
- 事实抽取、映射、Advice、结果和控制台报告验收未完成。
