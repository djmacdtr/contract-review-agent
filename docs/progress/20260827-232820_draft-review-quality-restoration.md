# DRAFT_REVIEW 高质量多文档链路恢复进度

## 结论

本阶段已将默认生产图从 KISS 起草检查路径恢复为动态事实抽取、跨文档映射、程序化比较、AI Advice、页码回填和持久化主链，内部版本升级为 `workflow 0.9.0 / rules 0.8.0`。离线单元回归和静态门禁通过，合成结构化并发探测通过并选择全局并发 `3`。

唯一一次全新三文件真实任务没有达到交付验收：任务 `tsk_01M11TRM32FPQ9NRCQD5V93QNY` 失败于 `FACT_EXTRACTION / text`，首个安全子码为 `LLM_OUTPUT_TRUNCATED`，批次 `batch_c64dfb4a9f56f9dc6b777455`，`batch_depth=0`，`unit_count=16`。任务在首个明确失败后停止，未 retry、未创建第二个真实任务。失败后已用合成单测修正截断缩批和文档快照时机，但按一次性验收约束没有再做真实调用，因此当前代码仍需下一次经明确授权的真实验收才能宣告可交付。

## 工作树边界

开始前已在仓库外保存基线状态、tracked diff 和文件哈希：

- `C:\Users\ROG\AppData\Local\Temp\contract-review-agent-baseline-20260827-215954`
- 分支：`feat/draft-review-multidoc`

开始前已存在的修改/未跟踪文件包括 `README.md`、LLM adapter/schema、页码映射/router、`draft_review.py`、OpenAI 单测、KISS `delivery_cross_check.py` 及其测试和两份 KISS 进度记录。本阶段没有覆盖或清理这些内容。

本阶段新增或实质修改的范围为：

- 恢复 `app/workflows/draft_review.py` 默认高质量图，Advice 改为 8 条左右小批、按 `risk_id` 合并和缺失项重试，并增加 95% 模型覆盖率交付门槛。
- 新增 `app/draft_review/mapping.py`，实现本地事实召回、每目标/每文件限量和 ID-only 小批量关系协议。
- 调整 `app/draft_review/extraction.py`、`facts.py`：候选/结构单元 ID 回填证据，逐项保留有效候选，missing-only 数值恢复，text/numeric 独立链，文档 checkpoint v2。
- 真实失败后将多单元截断恢复改为二分缩批，避免 16 单元父批次直接膨胀为 16 个调用；恢复预算独立于初始批次并受每文档绝对调用上限约束。
- 真实失败后将生产多文档抽取改为逐文档 Reduce 后立即保存完整快照；处理顺序优先模板、辅助资料，再处理目标合同。每份文档内部 numeric/text 仍并行，所有请求仍受 OpenAI adapter 全局 Semaphore 限制。
- `app/results/advice.py` 为 Advice 传入双侧差异和关联事实矩阵证据。
- `app/core/config.py`、`app/worker/runner.py` 修正 stale 下限；DOCX/PDF 本地解析、页码 sidecar 和绑定放入 `asyncio.to_thread`。
- 新增安全探测脚本 `scripts/draft_review_llm_readiness.py` 和一次性宿主验收脚本 `scripts/draft_review_real_acceptance.py`；两者只输出计数、阶段和安全错误码，不保存正文、URL、密钥或模型全文。
- 新增映射、Semaphore、恢复预算、partial salvage、Advice 缺失重试、Worker 心跳和逐文档快照测试。

未恢复并保持关闭：`FACT_REVIEW`、`FACT_MAPPING_REVIEW`、`SEMANTIC_PLAN/AST`、同模型双调用共识。未增加公开 API、结果 Schema 或数据库业务表；未扩展印章、鉴权、上传和 Word/PDF 报告。

## 合成并发与关键 Schema 探测

安全记录：`.real-diagnostic-temp/20260827-223442_draft-review-llm-readiness.json`。

- 输入不含合同正文。
- profile 并发波次 `1/2/3/4` 共 `10` 个逻辑调用、`10` 次 HTTP，全部 `200 / finish_reason=stop / request_attempts=1 / structure_retries=0`。
- 波次耗时分别为 `3.109 / 4.235 / 2.140 / 2.282` 秒。
- 虽然并发 4 也通过，生产上限按计划选择 `3`。
- 新映射 Schema：`1` 个逻辑调用、`1` 次 HTTP、2/2 配对返回，耗时 `1.390` 秒。
- Advice Schema：`1` 个逻辑调用、`1` 次 HTTP、2/2 风险返回，耗时 `21.359` 秒。
- readiness 合计：`12` 个逻辑调用、`12` 次 HTTP，无重试、429、5xx、Schema 错误或截断。
- 合成 Advice 模型覆盖率 `100% (2/2)`，fallback `0% (0/2)`。该数字仅证明协议可用，不代表真实合同验收覆盖率。

另有一次集成测试在第一次运行时未正确关闭宿主 `.env` 的 LLM 开关，产生了一个完全合成的 DRAFT_REVIEW 任务；测试夹具随后删除了该任务，未安装计数 transport，因此无法可靠恢复其精确模型调用数。本记录不编造该数字。

## 唯一真实三文件任务

安全记录：

- `.real-diagnostic-temp/20260827-224238_draft-review-real-acceptance.json`
- `.real-diagnostic-temp/20260827-224238_draft-review-real-acceptance.lock`

环境和约束：

- Docker Worker 在任务前停止；API/PostgreSQL 保持运行。
- 宿主机 WorkerRunner 使用当前工作树代码、宿主数据库映射、内网 LLM/OCR、只读本地 HTTP 文件服务。
- 全局 LLM 并发 `3`，FACT/MAPPING review、Semantic Plan 和同模型共识关闭。
- 原始三份脱敏文件未修改；任务结束后本地文件服务关闭并恢复 Docker Worker。
- 任务开始前活动任务数为 `0`，过程中 heartbeat 持续在约 2～10 秒内更新，没有 stale 回收、重复领取或 ownership 丢失。

真实调用和耗时：

- 总耗时 `2272.344` 秒。
- `DOWNLOAD` `0.359` 秒；`PARSING` `2064.547` 秒；`TEMPLATE_COMPARE` `0.157` 秒；`FACT_EXTRACTION` `206.406` 秒。
- OCR：`5` 次 HTTP，其中 `3` 次记录到 `200`；三份 DOCX 均完成外部页码解析后进入模板和事实阶段。其余两次为 transport 未记录最终状态的尝试，未保存响应。
- LLM：`64` 次 HTTP，其中 `63` 次记录到 `200`；均发生于动态事实抽取，任务未进入 FACT_MAPPING 或 Advice。
- 失败前保存 checkpoint：`profile-v2=3`、`numeric-v2=23`、`text-v4=4`，本任务无 checkpoint reuse。

最终状态：

- `FAILED / FACT_EXTRACTION / 75`
- `failure_stage=FACT_EXTRACTION`
- `chain=text`
- `batch_id=batch_c64dfb4a9f56f9dc6b777455`
- `failure_code=LLM_OUTPUT_TRUNCATED`
- `batch_depth=0`
- `unit_count=16`

由于失败发生在结果构造前：

- 没有持久化 `fact_matrix`、风险项或通过项，不能声称跨资料验收通过。
- 没有进入真实 Advice；真实模型建议覆盖率、fallback 覆盖率均为“不可计算”，不能写成 0% 或 100%。
- 模板比较阶段已执行完毕且未报错，但没有最终结果对象可供完整质量验收。

## 失败后的精确修复

没有发起第二个真实任务。只以失败子码和合成数据完成两项修复：

1. 16 单元 text 批次截断现在按 `16 → 8 → 4` 二分，每次 payload 身份不同；新增测试以 7 次调用完成 16 单元覆盖。旧实现会按单元展开并在第三个恢复父批次触发过小的 30% 恢复预算。
2. 文档 checkpoint 不再等待所有文档全局 Reduce。生产多文档调用逐文档完成并立即保存 v2 完整快照；新增测试证明辅助资料成功后，即使后续目标合同失败，辅助资料的完整文档快照仍存在。

这些修复只通过离线测试验证，尚未获得第二次真实授权，不能据此把本轮正式验收改记为成功。

## 测试与门禁

- 最终单元测试：`399 passed / 1 warning`，`17.10s`。
- 截断/文档 checkpoint 定向测试：`47 passed / 1 warning`。
- 在 Docker Worker 停止、`LLM/OCR/DOCX_PAGE_LOCATION` 明确关闭的宿主测试环境中，集成测试：`16 passed / 1 warning`，`15.15s`。该结果完成于最后两项抽取恢复修复之前；为保留失败任务记录，没有再次运行会 TRUNCATE 任务表的现有集成夹具。
- 相关文件 Ruff：通过。
- `compileall`：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示，无 whitespace error。

现有 `tests/integration/conftest.py` 的 autouse fixture 对配置的数据库执行 `TRUNCATE task_event, task_result, task_file, check_task CASCADE`。本轮第一次宿主集成回归误连当前容器数据库，导致旧任务行（包括两个对照任务）被清空；`.real-diagnostic-temp` 中旧安全指标和三份原文件仍保留。没有可用备份，因此未尝试伪造或恢复历史业务记录。后续必须为集成测试使用独立数据库，并在测试启动前增加非测试库防误用门槛。

## API、控制台与进程状态

- 控制台静态入口 `GET /console/` 返回 `200 text/html`。
- 失败任务详情 API 返回 `200`，列表按本次 `client_reference_id` 查询总数为 `1`，状态与数据库一致为 `FAILED / FACT_EXTRACTION / 75`。
- 应用内浏览器当前报告 `No browser is available`，所以未完成点击/截图级控制台验收；不能标记“报告可正常查看”。失败任务本身也没有结果报告可供验收。
- Docker Worker 已恢复为 running。

## 交付判定与下一步

当前代码恢复方向和离线实现已完成，但真实交付验收未通过。下一次真实任务必须由用户重新明确授权；在此之前建议先为集成测试建立隔离库，然后只复用本次已保存分片/文档 checkpoint 做一次受控 retry 验证：成功文档抽取调用应为 0，随后再核对非空 fact matrix、动态通过项、风险证据、真实 Advice ≥95%、fallback ≤5% 和控制台报告。不得在本记录基础上自动创建新任务。

本阶段未执行 commit、push、reset、clean；未删除或清理 `.real-diagnostic-temp`。
