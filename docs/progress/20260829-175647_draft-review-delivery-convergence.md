# 任务进度：起草检查交付收敛

## 基本信息

- 时间：2026-08-29 17:56:47 +08:00
- 状态：PARTIAL
- 任务类型：FIX / TEST / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`5af48ac`
- 工作树状态：dirty；包含既有 DRAFT_REVIEW、checkpoint、页码、Advice、诊断脚本和测试改动，本轮未覆盖或清理。

## 用户目标

在不新增公开 API 和不重复完整链路试错的前提下，将显式文档快照、跨文件业务阶段、Advice、页码 sidecar 拆开验证，并在零外部调用门禁通过后继续同一再生成任务。

## 本次完成

- 完成显式三份文档快照的加载、证据校验、文件 ID 重绑定和当前任务物化。
- 完成独立页码 sidecar 缓存读取/生成路径及零外部调用门禁。
- 修正 checkpoint 证据校验不应受展示页码影响的问题，并增加回归测试。
- 执行唯一再生成任务至第三次尝试，保留其安全失败结果；未创建第二个任务。

## 修改文件

- `app/draft_review/extraction.py`：页码中性文档 checkpoint 证据校验。
- `app/documents/page_locations.py`：sidecar 缓存身份、序列化、重绑定和校验能力。
- `scripts/regenerate_draft_report_host.py`：snapshot-only、page-sidecar-only、零调用门禁、显式快照执行和首个失败诊断保留。
- `tests/unit/test_draft_facts.py`、`tests/unit/test_page_sidecar_cache.py`：页码中性 checkpoint 与 sidecar 回归测试。
- 其余工作树修改均为前序会话已存在的用户改动，本轮未重写。

## 接口、数据和配置变化

- API：未修改公开接口；未调用公开 retry。
- 数据库/迁移：未新增表或迁移；sidecar 复用 `ExtractionCheckpoint`，来源结果未修改。
- 配置：未改写正式 `.env`；脚本内部关闭 OCR/LLM 的预检路径仅用于离线门禁。
- 兼容性：文档 checkpoint 仍按 SHA、版本和证据校验；物理页码仅作为展示 sidecar，不参与事实身份。

## 范围与约束

- 来源任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`
- 唯一再生成任务：`tsk_01M167E69YV0MGNHENB9HW10DG`
- 本轮未创建新任务，未调用公开 retry；来源结果保持只读。
- Docker Worker 保持停止；未执行 reset、clean、commit、push，也未清理 `.real-diagnostic-temp/`。

## 已完成的离线固化

- 再生成工作流支持显式注入 `document-extraction-v1`，绕过普通 `batch_id/payload_digest` 查找和事实抽取节点。
- 来源快照按文件 SHA-256 和抽取版本唯一加载，完成三份文档的证据校验、文件 ID 重绑定及当前任务物化。
- 页码 sidecar 独立使用现有 `ExtractionCheckpoint` 表缓存，owner 为 `sys_page_location_cache_v1`，版本为 `docx-page-location-v1`；缓存值不含正文。
- `--page-sidecar-only` 仅读取现有 OCR 解析缓存并完成本地 DOCX 页码映射；无缓存时不调用 OCR。
- 文档 checkpoint 校验已将物理页码作为展示字段从证据一致性校验中隔离，仍保留正文、结构、身份和证据回查校验。
- sidecar 文件级失败诊断保留首个安全 `failure_stage/failure_code`，不被汇总缓存检查覆盖。

## 零外部调用前置结果

`--snapshot-only` 结果为 `SNAPSHOT_OK`：

- 来源文档快照：3/3
- 正式解析上下文：3/3
- 证据重绑定：3/3
- 当前任务物化：3/3
- 事实抽取调用：0
- 当前任务三份文档事实数量：目标 264、模板 0、辅助资料 23

`--page-sidecar-only` 结果为 `PAGE_SIDECARS_OK`：

- sidecar：3/3
- 外部调用：0
- 目标合同：46 页；映射 1668/2958 个结构位置
- 模板：24 页，使用已缓存 sidecar
- 辅助资料：2 页，使用已缓存 sidecar
- 最大并发：2；单文件计算上限：300 秒

零调用重排队门禁结果为通过：

- 来源快照 3/3
- 正式上下文 3/3
- 证据重绑定 3/3
- 当前任务物化 3/3
- 页码 sidecar 3/3
- 旧文件引用残留：0
- 预计 OCR 调用：0
- 预计事实抽取调用：0
- 重排队时任务为 `attempt_count=2`、`max_attempts=3`

## 唯一任务执行结果

宿主机 Worker 已领取同一任务并执行第三次；没有重新创建任务，也没有再调用 OCR 或 LLM：

- 任务状态：`FAILED`
- 当前尝试次数：3/3
- 最终数据库阶段：`TEMPLATE_COMPARE`
- 持久化错误：`SNAPSHOT_EVIDENCE_REBIND_FAILED`
- 首个安全错误阶段：`SNAPSHOT_INJECTION`
- 事实抽取调用：0
- LLM HTTP 调用：0
- 当前任务成功抽取 checkpoint：3
- 控制台路径：`/console/#/tasks`
- 报告路径：`/console/#/reports/draft/tsk_01M167E69YV0MGNHENB9HW10DG`

失败发生在带物理页码的文档上下文重绑定：checkpoint 事实本身没有页码，而页码 sidecar 为展示位置补充了 `page/physical_pages`，旧校验路径将展示字段误纳入证据位置匹配。该问题已在本地代码中修正为页码中性校验，并有定向回归测试；由于第三次执行已耗尽任务尝试次数，本轮不再重排队验证。

## 离线验证

- `tests/unit/test_draft_facts.py tests/unit/test_page_sidecar_cache.py tests/unit/test_report_regeneration.py`：50 passed
- `tests/unit/test_draft_review_workflow.py`：55 passed
- 变更范围 Ruff：通过
- 相关 Python compileall：通过
- `git diff --check`：通过（仅有既有换行提示）

## 未完成项

- 本轮未得到成功的最终再生成报告，因此尚未完成动态跨文件结果、Advice 覆盖率和控制台视觉验收。
- 第三次任务执行已达到 `max_attempts`，按计划不能再次 retry、重排队或创建第二个任务；后续若继续，需要新的明确运维授权和新的任务生命周期方案。
- Docker Worker 仍保持停止，未执行正式部署恢复。

## Docker 与运行状态

- API：`contract-review-api-1` healthy，端口映射保持现状。
- Worker：Docker Worker 保持停止；宿主机 Worker 已完成唯一第三次领取后停止。
- PostgreSQL：`contract-review-postgres-1` healthy。
- 控制台：任务列表和报告路径已记录，但本轮没有成功报告可供最终视觉验收。
- 最终是否保持运行：API/PostgreSQL 保持运行；Docker Worker 保持停止，避免继续领取任务。

## 重要决策

- 页码属于展示 sidecar；文档快照的正文和证据校验必须忽略物理页字段，但不能忽略逻辑位置、身份或正文回查。
- 第三次执行已经耗尽任务尝试次数，即使本地修复已通过离线测试，也不再修改数据库状态或重新调用外部服务。

## 下一步建议

1. 由具备明确运维授权的后续会话评估新的任务生命周期方案；当前任务不可再 retry。
2. 如继续验收，先用已通过的页码中性 checkpoint 测试和 `SNAPSHOT_OK/PAGE_SIDECARS_OK` 结果作离线基线，再规划一次全新、受控的再生成任务。
3. 在新的成功任务完成后，再恢复 Docker Worker 并由用户进行控制台视觉验收。

## 下一会话首先阅读

- `AGENTS.md`
- `README.md`
- `app/draft_review/extraction.py`
- `scripts/regenerate_draft_report_host.py`
- 本文件及 `docs/progress/20260829-170017_explicit-snapshot-injection.md`

## 交接摘要

- 三份来源快照已唯一命中并物化，事实抽取调用为 0。
- 三份页码 sidecar 已缓存，外部调用为 0。
- 零调用门禁通过后，同一再生成任务已第三次领取。
- 执行在页码绑定后的 checkpoint 证据重绑定处失败，未产生 LLM/OCR 调用。
- 本地已加入页码中性校验并通过 50+55 项定向测试。
- 任务当前 `FAILED`，`attempt_count=3/3`；不得再次 retry 或新建同类任务。
- Docker Worker 停止，API/PostgreSQL 保持健康。
- 最终成功报告、跨文件结果、Advice 覆盖和控制台视觉验收仍未完成。
