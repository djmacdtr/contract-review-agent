# 任务进度：Numeric 空候选批次短路与唯一恢复

## 基本信息

- 时间：2026-08-29 12:24:34 +08:00
- 状态：PARTIAL
- 任务类型：FIX
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：5af48ac
- 工作树状态：dirty；保留既有页码、OCR 缓存、LLM、映射、前端及历史诊断未提交修改

## 用户目标

修复 Numeric 空候选批次仍被发送至 GLM 的问题，保持非空批次 checkpoint 可复用，并执行一次唯一 checkpoint 恢复验收。

## 本次完成

- Numeric planner 默认过滤 `numeric_candidate_count == 0` 的批次，保留非空批次顺序、结构和 batch ID。
- 保留过滤前批次数作为内部 checkpoint payload 元数据，避免既有有效 checkpoint 因规划过滤而发生 digest 漂移。
- extraction workflow、OpenAI 客户端和 Numeric Canary 均增加空候选零调用保护。
- Canary 对空批次返回 `SKIPPED_EMPTY`，不发起外部请求。
- 通过一次唯一 retry 验证了空批次门禁未再触发；该 retry 在后续 Text 批次因 `LLM_INVALID_JSON` 失败。

## 修改文件

- `app/draft_review/facts.py`：过滤空 Numeric 规划批次，并提供诊断所需的完整规划视图。
- `app/draft_review/extraction.py`：增加空 Numeric 本地成功短路和恢复子批次过滤。
- `app/adapters/llm/openai_client.py`：禁止构造或发送空 Numeric 请求。
- `scripts/numeric_recovery_diagnostic.py`：增加 `SKIPPED_EMPTY` 诊断结果和空批次精确识别。
- `tests/unit/test_openai_llm_client.py`：增加空 Schema 和零 HTTP 调用测试。
- `tests/unit/test_structured_extraction_v2.py`：增加规划过滤及工作流零空请求测试。
- `tests/unit/test_numeric_recovery_diagnostic.py`：增加空 Canary 跳过测试。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化。
- 配置：无变化；OCR 和页码在本次 host retry 中保持关闭。
- 兼容性：不改变 batch ID、checkpoint 表或公开 retry 接口；`planned_batch_count` 保留过滤前值以维持既有 payload digest。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\\Scripts\\python.exe -m pytest` 五个新增/直接相关用例 | 通过 | 5 passed |
| `.venv\\Scripts\\python.exe -m pytest tests/unit/test_openai_llm_client.py tests/unit/test_structured_extraction_v2.py tests/unit/test_numeric_recovery_diagnostic.py` | 部分通过 | 76 passed、19 failed；失败来自工作区既有旧断言/历史恢复测试及依赖版本差异，未扩展修复 |
| 初始系统 Python pytest 收集 | 未执行用例 | 环境缺少 `rapidfuzz` |
| `ruff check` 变更相关文件 | 通过 | All checks passed |
| `python -m compileall` 变更相关文件 | 通过 | exit 0 |
| `git diff --check` | 通过 | 仅有既有换行格式提示 |
| 唯一 host retry | 失败 | 117 秒；8 次 HTTP，均 200；Text `batch_depth=1`、`unit_count=8` 返回 `LLM_INVALID_JSON` |

## Docker 与运行状态

- API：运行且 healthy，`/health` 返回 200。
- Worker：Docker Worker 已停止；host Worker 已退出。
- PostgreSQL：运行且 healthy。
- 控制台：任务路径 `/console/#/tasks`；失败任务报告路径为 `/console/#/tasks/tsk_01M15W20DRDW8ZHTC9N72XNMTZ/report`。
- 最终是否保持运行：API/PostgreSQL 保持运行，Docker Worker 按失败止损规则保持停止；host 临时文件服务已关闭。

## 重要决策

- 空 Numeric 结构不是模型任务，不进入动态 Schema、HTTP 请求或恢复预算。
- 唯一 retry 的首个明确失败是 Text 非法 JSON，不继续调整 Numeric、OCR、页码或 checkpoint 逻辑。

## 已知问题与风险

- 唯一恢复任务 `tsk_01M15W20DRDW8ZHTC9N72XNMTZ` 未发布结果；失败阶段为 `FACT_EXTRACTION`，错误码为 `LLM_INVALID_JSON`。
- 本次未完成正式报告、39 项差异、4 项通过、页码及控制台结果验收。
- 相关测试文件仍有 19 个既有失败，未在本任务中扩大修复。

## 下一步建议

1. 由后续会话针对 Text `LLM_INVALID_JSON` 的恢复深度或网关响应协议单独制定方案。
2. 不对本次失败任务再次 retry，保留其安全诊断和 checkpoint。
3. 在明确授权后再恢复 Docker Worker；不要清理 `.real-diagnostic-temp/`。

## 下一会话首先阅读

- `app/draft_review/facts.py`
- `app/draft_review/extraction.py`
- `app/adapters/llm/openai_client.py`
- `scripts/numeric_recovery_diagnostic.py`
- `docs/progress/20260829-122434_numeric-empty-batch-retry-report.md`

## 交接摘要

Numeric 空候选批次已在 planner、workflow、LLM client 和 Canary 四层短路，5 个新增定向用例通过，Ruff/compileall/diff check 通过。唯一 retry 已创建并执行，没有再次 retry；空 Numeric 请求未再出现。任务随后在 Text 八单元恢复批次以 `LLM_INVALID_JSON` 失败。API/PostgreSQL healthy，Docker Worker 停止，工作树保持 dirty，未执行 commit、push、reset、clean。
