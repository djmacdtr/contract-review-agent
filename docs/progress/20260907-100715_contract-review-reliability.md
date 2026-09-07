# 合同审查任务稳定性增强

## 变更摘要

- 增加任务级单调恢复预算，默认最多额外 600 秒，并将安全恢复统计写入内部结果 metadata。
- 补齐 LLM `httpx.RequestError` 的有限退避重试：默认最多 2 次额外尝试；保留现有 HTTP 状态重试、超时策略和结构修正策略。
- 为正式 Mapping 增加一次完整同 payload 恢复，并使用现有 `extraction_checkpoint` 表保存/复用通过严格校验的结果；checkpoint digest 排除任务本地文件 ID 和物理页码，加载时重新绑定当前文件身份与位置。
- 保持事实抽取拆分、Advice 缺失项恢复/fallback、业务规则、Prompt、Schema、公开 API、数据库结构和 Worker 并发不变；Advice 仅接入统一预算。
- 为 DRAFT_REVIEW 和 FINAL_COMPARE 共用 DOCX 页码恢复：先从 page-free 结果重建 sidecar，必要时仅对错误指向的 DOCX 刷新 OCR 一次，完整公开页码校验通过后才提交缓存。
- 解析阶段发现 sidecar 缺失时也进入同一受限恢复入口，不估算或伪造页码。

## 验证

- 边界审计修正：Mapping checkpoint 身份固定使用实际 `LLM_EXTRACTION_MODEL`；页码 sidecar/OCR 恢复改为先暂存，完整页码校验成功后才提交缓存；每个任务最多一次 sidecar 本地重建和一次 DOCX OCR 刷新。
- `docker compose -f compose.yaml -f compose.server-parity.yaml config --quiet`：通过。
- Docker Desktop 已启动，`postgres`、`api`、`worker`、`nginx` 均健康；`/health`、`/ready`、`/nginx-health` 和控制台入口通过。
- `docker compose -f compose.yaml -f compose.server-parity.yaml exec -T api alembic current`：`0003_extraction_checkpoint (head)`。
- `docker compose --profile tools run --rm test`：`568 passed, 1 warning`；16 项数据库集成测试已在容器中执行并进入业务断言。
- 变更文件定向 Ruff：通过；`python -m compileall -q app scripts`：通过；`git diff --check`：通过。
- `ruff check app tests scripts` 仍有 11 个既有问题，分布在 `app/adapters/downloader.py`、`app/draft_review/delivery_cross_check.py`、`scripts/e2e_final_compare.py`、`tests/integration/conftest.py` 和 `tests/unit/test_graphs.py`；未在本轮改动。
- 真实五文件脚本按用户给定命令仅执行一次，因脚本自身 `GIT_PREFLIGHT_FAILED` 门禁停止。五份文件库存校验通过，两个历史提交均在当前 HEAD 祖先链上；但工作区存在本轮及既有未允许改动。脚本在创建任务前退出，未生成任务 ID、未调用真实 OCR/LLM、未创建第二任务、未重试。
- 脚本生成的 `.real-diagnostic-temp/five-file-reliability-acceptance.json` 和 `.lock` 保留作阻断证据；未清理缓存、数据库或历史报告。

## 工作区约束

本轮未执行 commit、push、reset、clean、数据库清理或 `docker compose down -v`；既有用户修改和未跟踪文件保持原样。由于真实任务门禁失败，未进入封版、显式暂存、镜像构建或离线升级包步骤。当前 Docker 服务保持运行，未为绕过门禁改变工作区。

## 五文件验收门禁修正与最终结果

- 验收脚本新增 `--allow-reviewed-worktree-sha256`。指纹由当前 `HEAD`、排除 `backups/`、`tmp/`、`.real-diagnostic-temp/` 后的 tracked 二进制 diff SHA-256、未跟踪文件相对路径及文件 SHA-256 确定性生成；报告只输出路径、状态和摘要，不输出文件正文。
- 门禁验证通过：默认无指纹拒绝；正确指纹 `608730d03e74cf64a6b064f9c9e877b92dfb4e6aadc9b1964f06bb8847b2f5f9` 通过；错误指纹拒绝；增加临时未跟踪文件后旧指纹拒绝，临时文件已删除。
- Worker 停止期间，健康检查通过，活动任务数为 0，五文件库存通过，5/5 OCR 缓存和 4/4 DOCX sidecar 命中，无预热调用；OCR relay 仅监听 `127.0.0.1:18017`。
- 唯一真实任务命令执行一次，任务 ID `tsk_01M1WWS992GE90P7FBTB5A6K6K`，`source_task_id=null`。任务在 `GENERATING_ADVICE`、92% 时严格失败：`DOCX_PAGE_LOCATION_INCOMPLETE` / `PUBLIC_DIFF_PAGE_MISSING`，公开差异证据要求 40 项、覆盖 39 项，缺少 1 项。
- 本次真实调用统计：OCR HTTP 0 次，LLM HTTP 80 次（finish reason：`stop` 73、`length` 7）；未执行 retry、未创建第二任务。失败后已恢复 Docker Worker。
- 由于真实任务失败，Advice/最终风险报告验收、后续提交、推送、镜像和离线升级包步骤全部停止；不把本次结果标记为封版通过。

## PDF 页码修复验收结果

- 页码副本构造已修正：`page_free_result_copy` 接收显式 DOCX 文件 ID 集合，递归继承 `file_id/source_file_id`；仅移除 DOCX location 的 `page` 和页码锚点，PDF 原生页码及未知归属页码均保留。
- 增加混合 DOCX/PDF、外层 PDF 文件 ID 继承、PDF 缺页不触发 sidecar/OCR、DOCX 恢复及 `PERSISTING_RESULT / 95% / 正在校验并补全公开证据页码` 回归测试。
- 定向测试：`test_reliability_recovery.py` 12 passed；`test_draft_review_workflow.py` 57 passed；定向 Ruff、`compileall`、`git diff --check` 通过。
- 容器全量测试：`568 passed, 1 warning`；无数据库迁移，Alembic 仍为 `0003_extraction_checkpoint (head)`。
- 修复后工作区指纹为 `9128b78513da275001123556e2788e657455e72e29c08435c1345619ce3bc177`；旧指纹已验证失效。
- 唯一新的真实任务 ID `tsk_01M1WY021GQ4JWTZWMRK366Q63`，`source_task_id=null`，未 retry、未创建第二任务。任务在 `FACT_EXTRACTION / 75%` 失败：`DYNAMIC_CHECK_INCOMPLETE`，底层 `LLM_OUTPUT_TRUNCATED`，`finish_reason=length`，`batch_depth=2`，`unit_count=4`。
- 本次真实调用统计：OCR HTTP 0 次，LLM HTTP 63 次（`stop` 57、`length` 6）。任务未进入页码补全，因此本次未能验证最终 PDF 页码覆盖；安全错误报告已保留，Worker 已恢复。
- 由于真实任务再次失败，未执行 commit、push、镜像、离线升级包或 SHA-256 发布步骤；失败任务及前一次失败任务均保持不动。

## 稳定性边界审计与最终离线门禁

- 完成全链路边界审计并补齐受限恢复：共享 600 秒恢复预算、下载有限重试、LLM 网络最多 3 次总请求、Mapping 单次逻辑恢复、结构化抽取叶子恢复、同任务瞬态 Worker 恢复及幂等完成处理；Advice 现有分批、缺失项补偿和 fallback 规则保持不变。
- 配置边界固定为：任务瞬态恢复最多 1 次、数据库写入最多 1 次、下载最多 2 次额外尝试、LLM 抽取叶子恢复最多 1 次、文本拆分最大深度不超过 4；鉴权、额度、文件、身份和证据错误不进入重试路径。
- 服务器等价环境重建完成：`api`、`worker`、`postgres`、`nginx` 运行；`/health`、`/ready`、`/nginx-health` 均返回 200；Alembic 为 `0003_extraction_checkpoint (head)`；主库只读状态查询显示既有 `FAILED|32`、`SUCCEEDED|22`，无 `PENDING/RUNNING`。
- 主机单元测试：`572 passed, 1 warning`。容器全量测试：`588 passed, 1 warning`；16 项数据库集成测试在容器中实际执行并进入业务断言。定向 Ruff、`python -m compileall -q app scripts`、`git diff --check` 均通过；11 个历史 Ruff 问题未处理。
- 主机直接运行 Worker 集成 fixture 仍无法解析 Docker 内部 `postgres` 服务名；该环境差异不作为代码失败，数据库集成以服务器等价容器结果为准。
- 本阶段未创建 Canary 或新的真实五文件任务；两个既有失败任务保持不动。未执行 commit、push、reset、clean、数据库/缓存清理或 `docker compose down -v`，未进入封版步骤。

## 最终 Canary 门禁结果

- 使用精确失败批次 `batch_0f05921c771817faa0876707` 和来源任务 `tsk_01M1WY021GQ4JWTZWMRK366Q63` 执行一次只读 Text Canary，输出保存在 `.real-diagnostic-temp/reliability-exact-text-canary.json`。
- Canary 在调用 LLM 前安全停止：`TEXT_BATCH_DIAGNOSTIC / TEXT_SOURCE_FILE_NOT_UNIQUE`；安全输出未包含合同正文、模型响应或凭据，`llm_calls=0`。
- 原因是失败任务的错误元数据指向 TARGET 文件，而现有只读诊断脚本要求唯一 REFERENCE 文件，无法无猜测地绑定精确批次来源；未修改诊断脚本、未绕过文件身份校验。
- 按门禁要求立即停止：未创建正式五文件任务、未重试 Canary、未 retry 任一历史失败任务；Docker Worker 未被停止且仍保持运行。未执行 commit、push、reset、clean、数据库/缓存清理或 `docker compose down -v`。

## Text 诊断门禁修正与 Canary 结果

- `text_grounding_diagnostic.py` 已修正：失败元数据带 `file_id` 时跨角色精确选择任务文件；重建 `LocalFile` 沿用真实角色；无 `file_id` 时才按唯一角色回退；文件缺失、SHA 不符和批次不匹配均安全停止。
- TARGET 失败批次支持使用同一正式 TEMPLATE 生成模板候选链后重建，未修改生产工作流；对应测试覆盖 TARGET、REFERENCE、角色回退、文件缺失、SHA 不符和批次不匹配。
- 定向测试：`20 passed, 1 warning`；诊断脚本与对应测试 Ruff、compileall、diff check 均通过。
- 同一失败任务 `tsk_01M1WY021GQ4JWTZWMRK366Q63`、同一精确批次 `batch_0f05921c771817faa0876707` 的最终 Canary 成功：TARGET 文件 `fil_01M1WY021GQ4JWTZWMRK366Q64`，4 单元、深度 2，`GLM-5.3-Flash`，`llm_calls=1`，`accepted_fact_count=0`，`checkpoint_written=false`；安全报告保存在 `.real-diagnostic-temp/reliability-exact-text-canary-final.json`。
- Canary 成功后尚未创建正式五文件任务；下一步仅执行一次正式任务，失败即停止。

## 最终五文件正式验收结果

- 停止 Docker Worker 后，API、Ready、Nginx 健康检查均为 200，执行一次全新公开五文件任务；任务 ID `tsk_01M1XBQ4G6VJB235GXZJC3JQR6`，`source_task_id=null`，数据库确认 `SUCCEEDED / COMPLETED / 100%`。
- 五文件及三份参考资料门禁通过；公开证据页码覆盖 `94/94`，`missing_evidence_count=0`；Advice `31/31` 非空；风险 `31`，通过项 `14`。
- 真实任务调用统计：LLM HTTP `80` 次，HTTP 200 全部成功，finish reason `stop=74`、`length=6`；OCR HTTP `0` 次；未发现异常重试放大。任务耗时约 `540.625s`。
- 运行时 metadata：`FACT_EXTRACTION=5`、`FACT_MAPPING=3`、`RISK_ADVICE=4`；配置仍为 `GLM-5.3-Flash`、LLM 并发 2、OCR 重试 0、LLM HTTP 重试 1；任务身份中 `file_count=5`、`reference_file_count=3`、`source_task_id=null`。
- 正式报告中的 Advice `fallback_count=6`、`model_count=25`。虽然所有 Advice 非空且主体验收通过，但按本轮“出现 fallback 即停止封版”门禁，未执行 commit、push、镜像或离线升级包构建。
- 正式验收结束后已恢复 Docker Worker；API、Ready、Nginx 再次为 200；本轮时间窗口内仅创建上述一个新任务，未 retry、未创建第二任务。报告保存在 `.real-diagnostic-temp/five-file-reliability-final-after-canary.json`。

## Advice 零 Fallback 修复与 Canary 门禁结果

- Advice payload 已修正为只遍历当前选中风险的 `evidence_keys`，单风险恢复仅发送对应风险、关联差异、关联事实和文件清单。
- 增加内部 `SingleRiskAdviceResponse` 和 `generate_advice_item()`：`json_object`、关闭思考、1024 tokens、严格沿用现有 Advice 质量校验；单项恢复共享 600 秒预算、最多 16 次、并发不超过 2。
- Advice `model_runs` 现记录成功、失败和恢复请求的安全诊断字段；内部 metadata 增加批次失败、单项修复、fallback 风险和失败码统计，不记录模型正文或合同全文。
- 定向 Advice/LLM/工作流/Canary 测试：`140 passed`；变更文件 Ruff、compileall、git diff check 通过。服务器等价容器全量测试：`604 passed, 1 warning`，16 项数据库测试实际进入业务断言。
- 宿主机直接运行全量测试得到 `588 passed, 16 errors`；16 个错误均为 fixture 连接 `postgres:5432` 的环境解析失败，容器等价全量测试已覆盖并通过。
- Advice-only Canary 只读加载成功任务 `tsk_01M1XBQ4G6VJB235GXZJC3JQR6`，精确识别 `risk_diff_000014`～`risk_diff_000019`；9 次 HTTP 均为 200，`accepted=6`、`fallback=0`、六个风险唯一且完整覆盖，OCR/数据库写入均为 0。
- Canary 报告初版因诊断脚本错误要求返回顺序与输入一致而标记 `FAILED`，实际集合和质量门槛均已满足；已修正为集合相等并检查唯一性，保留初版失败报告 `.real-diagnostic-temp/advice-recovery-canary-final.json`。
- 按“Canary 未通过即停止、不得重复调用”门禁，本轮未再次调用 Advice Canary，未创建新的五文件任务；未执行 commit、push、reset、clean、数据库/缓存清理或 `docker compose down -v`。Docker Worker 保持运行。

## 离线 Canary 重判与唯一正式任务结果

- 对原 Advice Canary 报告执行纯离线重判，未调用模型、OCR 或数据库写入；原报告 SHA-256 为 `4006800b5b299f9c02f8100cbc828f16e2e95393ab99d50513b4b075b28e5897`，重判文件为 `.real-diagnostic-temp/advice-recovery-canary-final-rejudged.json`，结论为 `SUCCEEDED`。
- 重建服务器等价 API/Worker 镜像后，停止 Worker，使用新指纹 `78756e37b578d4bca7f9848f33eac0d2d6be42cdd584034feda366775903ea08` 执行唯一一次全新五文件任务；任务 ID `tsk_01M1XEAXSH7SGT5T16JDH63RS7`，`source_task_id=null`。
- 正式任务状态为 `SUCCEEDED / COMPLETED / 100%`；五文件、三份参考资料、公开证据页码 `98/98`、`missing_evidence_count=0`、OCR HTTP 0、LLM HTTP 74 且均 200。
- Advice 门禁失败：`risk_count=32`、`model_count=29`、`fallback_count=3`；精确 fallback 风险为 `risk_diff_000011`、`risk_diff_000012`、`risk_diff_000015`。安全失败报告为 `.real-diagnostic-temp/five-file-advice-zero-fallback-failure.json`，原正式报告 SHA-256 为 `234a716a27ad956cc612096f8d144be4fe3d9562877c2056385064e565a4346e`。
- 根因确认：DRAFT_REVIEW 使用 `app/workflows/draft_review.py` 内部 Advice 批处理树，当前未接入 `generate_advice_item()`；先前增强的 `app/results/advice_batches.py` 未覆盖该生产路径。
- 已恢复 Docker Worker；API、Nginx、PostgreSQL、Worker 健康检查均通过。按正式任务 fallback 门禁停止，不创建第二个任务，不 commit、push 或构建发布包。

## DRAFT_REVIEW Advice 共享接线与最终五文件验收

- 删除 DRAFT_REVIEW 内嵌 Advice 分批树，统一调用共享 `generate_advice_in_batches()`；保留 `require_dynamic_anchor=True` 和同一 600 秒恢复预算。增加生产工作流集成测试及静态门禁，禁止 DRAFT_REVIEW 自行实现 Advice 分批逻辑。
- 定向 Advice/工作流/可靠性测试：`86 passed`；Advice-only Canary 测试：`11 passed`；变更文件 Ruff、`compileall`、`git diff --check` 和服务器等价 Compose 配置检查通过。
- 服务器等价容器全量测试：`606 passed, 1 warning`；16 项数据库集成测试实际进入业务断言；Alembic 仍为 `0003_extraction_checkpoint (head)`，无前端或数据库迁移变化。
- 使用工作区指纹 `1d8e57fb107f501a40d9c9423ac5c49d7f2c418183df19a291db2709496f05c7` 执行唯一一次全新五文件任务，任务 ID `tsk_01M1XFJATFC1KQW9GVM5ZM3043`，`source_task_id=null`。
- 正式任务 `SUCCEEDED / COMPLETED / 100%`；Advice `30/30` 非空，`fallback_count=0`，单项修复 `4/4` 成功；公开证据页码覆盖 `90/90`，`missing_evidence_count=0`；三份辅助资料均参与；OCR HTTP `0`，LLM HTTP `76` 且全部 200。
- 任务完成后 Docker Worker 已恢复；API、Ready、Nginx 健康检查均为 200。未 retry 历史任务，未创建第二个新任务，未清理数据库、缓存或历史报告。
