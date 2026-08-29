# 任务进度：低置信度映射安全降级与唯一恢复

## 基本信息

- 时间：2026-08-29 14:07:11 +08:00
- 状态：COMPLETED
- 任务类型：FIX
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：5af48ac
- 工作树状态：dirty；保留此前页码、抽取、恢复、映射和进度记录修改

## 用户目标

将低于 0.85 的映射或缺失要求安全降级为不确定，不阻断报告、不进入正式风险或通过结论；随后从指定失败任务执行唯一宿主机恢复。

## 本次完成

- 低置信度普通映射不再抛出 `MAPPING_CONFIDENCE_INVALID`，而是丢弃并标记参考文件不确定。
- 低置信度缺失要求不再加入正式缺失集合，并标记参考文件不确定。
- 身份、文件、位置、证据和结构校验仍保持致命失败。
- 增加安全映射诊断计数：接受映射 2 项，低置信度丢弃 2 项。
- 从指定来源任务执行唯一一次恢复，任务成功并生成持久化结果。

## 修改文件

- `app/workflows/draft_review.py`：实现低置信度映射/缺失要求安全降级和安全计数。
- `tests/unit/test_draft_review_workflow.py`：增加低置信度映射及缺失要求回归测试，并验证正式结论不受污染。
- `docs/progress/20260829-140711_mapping-confidence-recovery.md`：记录本次实现、验证和真实恢复结果。

## 接口、数据和配置变化

- API：未修改。
- 数据库/迁移：未修改。
- 配置：未修改。
- 兼容性：保留 0.85 置信度阈值；未修改 Numeric、Text、Advice、Prompt、Schema、checkpoint 身份、页码或公开结果 Schema。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest tests/unit/test_draft_review_workflow.py -k "mapping or single_model_uncertain" -q` | 通过 | 15 passed，38 deselected |
| `ruff check app/workflows/draft_review.py tests/unit/test_draft_review_workflow.py` | 通过 | All checks passed |
| `python -m compileall -q app/workflows/draft_review.py` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 仅有 Git 行尾转换提示 |

## 唯一恢复验收

- 来源任务：`tsk_01M15ZWY9NMWZEQ5K9DWK5W56V`
- 来源成功 checkpoint：3
- 新任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`
- 任务状态：`SUCCEEDED / COMPLETED / 100`
- 执行耗时：534.328 秒
- 总 LLM HTTP 请求：72；状态均为 HTTP 200
- 映射模型运行：1 次，`GLM-5.3-Flash`，请求尝试 1 次
- 映射 `finish_reason`：结果模型运行记录未持久化该字段，安全报告无法取得，不作推断
- 结果阶段：`COMPLETED`
- diff 数：39
- risk 数：39
- 通过项数：3
- fact matrix 数：264
- 非空建议数：39/39
- 风险关联差异数：39/39
- 映射诊断：接受映射 2，低置信度丢弃 2
- 新任务成功 checkpoint：3；结果摘要中的 `checkpoint_reused` 为 0，事实抽取运行记录请求尝试为 0
- warnings：7 项，包含 `LLM_ADVICE_UNAVAILABLE`、`DOCX_MERGED_CELLS_SIMPLIFIED` 和 `TEMPLATE_TABLE_STRUCTURE_EXPANDED`
- 控制台任务列表：`/console/#/tasks`
- 控制台报告：`/console/#/tasks/tsk_01M161GFY6Q7YSP07R877XQM2B/report`

## Docker 与运行状态

- API：健康运行，`127.0.0.1:8000`。
- Worker：Docker Worker 保持停止；宿主机 Worker 已完成并退出。
- PostgreSQL：健康运行，`127.0.0.1:15432`。
- 本地临时文件服务：已停止。
- 控制台视觉检查：由用户负责，本次未代替用户执行浏览器视觉验收。

## 重要决策

- 低置信度项不降低阈值、不伪装为可信结论，也不生成正式风险或通过项。
- 本次唯一恢复成功后未再次调用 retry、未创建第二个任务、未运行 Text/Numeric Canary 或全量测试。

## 已知问题与风险

- Advice 阶段产生 `LLM_ADVICE_UNAVAILABLE` warning；当前 39 项风险仍全部具有非空建议，需区分模型建议与 fallback 的具体覆盖情况时应进一步查看结果元数据。
- 本次通过 3 项而非历史基线的 4 项；本轮未修改通过项逻辑。
- 结果文件页码未在本次宿主机恢复中启用，未作为本轮验收项确认。

## 下一步建议

1. 人工打开控制台报告，抽查映射低置信度项未形成正式风险/通过结论，并核对建议文本质量。
2. 后续如需继续处理 Advice warning，应单独诊断，不回退低置信度映射安全降级。

## 下一会话首先阅读

- `app/workflows/draft_review.py`
- `tests/unit/test_draft_review_workflow.py`
- `docs/progress/20260829-140711_mapping-confidence-recovery.md`

## 交接摘要

低置信度映射和缺失要求已改为安全丢弃并标记不确定，定向测试 15 项通过，静态检查通过。唯一宿主机恢复任务 `tsk_01M161GFY6Q7YSP07R877XQM2B` 已 `SUCCEEDED / COMPLETED / 100`，39 项风险、39 条非空建议、3 项通过；映射接受 2、低置信度丢弃 2。Docker Worker 停止，API/PostgreSQL 健康，未 commit/push/reset/clean。
