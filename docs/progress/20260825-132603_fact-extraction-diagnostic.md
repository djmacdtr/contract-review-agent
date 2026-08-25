# 任务进度：事实抽取安全校验诊断

## 基本信息

- 时间：2026-08-25 13:26:03 +08:00
- 状态：PARTIAL
- 任务类型：FIX / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`926afac fix(draft-review): release expanded table checks`
- 工作树状态：dirty，仅包含本阶段 6 个代码/测试文件；`.real-diagnostic-temp` 保留未触碰。

## 用户目标

修复真实三文件 DRAFT_REVIEW 在事实抽取后统一失败的问题，补齐表格单元格证据回查并保留安全错误分类，最终验证动态检查、左右差异证据和一句 AI 建议链路。

## 本次完成

- 紧凑事实抽取 payload 保留表格整体证据，并追加当前批次的表格单元格证据及 `table_index + row + column` 位置。
- 抽取 Prompt 明确表格事实优先使用单元格位置，表格整体事实可使用表格位置。
- 为 profile/事实位置、原值回查、重复事实身份、文件身份、Schema 和上游错误增加显式安全分类。
- DRAFT_REVIEW 失败日志仅记录 task、阶段、文档角色、分块序号、分类和计数；未改变公开错误响应或任务状态接口。
- 扩展表格 `TABLE_STRUCTURE_EXPANDED`、FINAL_COMPARE、确定性文字差异、客户结果 Schema 均未改变。

## 修改文件

- `app/draft_review/facts.py`：表格单元格证据块和证据校验分类。
- `app/adapters/llm/openai_client.py`：抽取 Prompt 和安全失败 code 传递。
- `app/workflows/draft_review.py`：内部安全失败日志。
- `tests/unit/test_draft_facts.py`：表格回查及五类事实失败用例。
- `tests/unit/test_openai_llm_client.py`：证据/Schema 分类用例。
- `tests/unit/test_draft_review_workflow.py`：安全日志和公开错误边界用例。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化。
- 配置：无变化。
- 兼容性：保留 `DYNAMIC_CHECK_INCOMPLETE` 对外错误码；未新增业务检查项、公开结果字段或控制台展示。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_template_checks.py tests/unit/test_comparison.py tests/unit/test_result_schema_v21.py tests/unit/test_risk_model.py tests/unit/test_result_advice.py -q` | 通过 | 143 passed；1 个既有依赖弃用告警 |
| `ruff check` 变更 Python 文件 | 通过 | 无 lint 错误 |
| 变更 Python 文件定向 `compileall` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误 |
| 全仓 pytest / Docker / OCR / 前端检查 / 五文件任务 | 未执行 | 按本阶段范围排除 |

## 唯一一次真实三文件诊断

固定使用一个目标合同、一个模板和一份辅助资料，`LLM_SAME_MODEL_DIAGNOSTIC=true`、`LLM_MAX_OUTPUT_TOKENS=4096`、结构重试为 0、OCR 关闭；未增加辅助资料，未重试。

- 总耗时：约 95.0 秒。
- LLM 调用次数：2，均为 `FACT_EXTRACTION`，均发生在目标合同；在第 2 个抽取分块失败后立即停止。
- 调用 1：请求 59,695 字符，Schema 3,058 字符，响应 5,120 字符，`finish_reason=stop`，prompt/completion/total tokens 为 13,366/1,204/14,570，未截断，约 21.1 秒。
- 调用 2：请求 43,634 字符，Schema 3,058 字符，响应 13,446 字符，`finish_reason=stop`，prompt/completion/total tokens 为 9,872/3,900/13,772，未截断，约 70.1 秒。
- 完成阶段：下载、解析、模板比较、部分事实抽取调用。
- 失败阶段：`FACT_EXTRACTION`，目标合同第 2 个分块。
- 安全错误分类：`LLM_RESPONSE_SCHEMA_INVALID`，`affected_count=1`，累计分类计数为同类 1。
- 显式表格单元格回查未再触发位置或原值错误；本次仍是模型响应 Schema 校验失败。
- 未进入事实评审、跨文档映射、映射评审、语义规划、数值执行或建议生成。
- 正式差异项数量：0；正式 AI 建议：0；双侧证据和建议覆盖率：N/A。
- 诊断数据未进入正式风险、通过项或独立共识。
- 真实脚本只保留安全聚合指标和错误类别，没有输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL；临时下载目录已清理。

## Docker 与运行状态

- API / Worker / PostgreSQL / 控制台：未启动或改变。
- 最终是否保持运行：保持原状态。

## 已知问题与风险

- 表格单元格证据回查缺口已修复并通过离线验证，但真实目标合同第 2 抽取分块仍返回不符合紧凑 Schema 的模型结果。
- 本次真实失败后未保存模型响应，无法从响应内容推断具体 Schema 字段；当前仅保留安全类别和聚合指标。

## 下一步建议

1. 使用离线构造的紧凑响应覆盖 `field_key`、`value_type`、长度上限、额外字段和数组上限等 Schema 失败分支，进一步缩小 `LLM_RESPONSE_SCHEMA_INVALID` 的安全分类范围。
2. 在获得新的真实调用授权前，不重复执行三文件真实任务，不扩展辅助资料，不降低证据和 Schema 安全校验。
3. 真实链路成功后再验证事实评审、映射、语义规划、数值执行、左右差异和一句建议的正式结果边界。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260825-132603_fact-extraction-diagnostic.md`
- `app/draft_review/facts.py`
- `app/adapters/llm/openai_client.py`
- `app/workflows/draft_review.py`

## 交接摘要

表格事实现在支持单元格位置确定性回查，143 项定向测试通过。唯一一次真实三文件任务调用 2 次抽取请求后，在目标第 2 分块以 `LLM_RESPONSE_SCHEMA_INVALID` 停止；两次均 `stop` 且未截断。未产生正式差异或建议，也未继续真实调用。工作区保留本阶段未提交修改，不推送。
