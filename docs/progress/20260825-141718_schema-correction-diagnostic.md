# 任务进度：安全 Schema 摘要与一次结构纠错

## 基本信息

- 时间：2026-08-25 14:17:18 +08:00
- 状态：PARTIAL
- 任务类型：FIX / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`926afac fix(draft-review): release expanded table checks`
- 工作树状态：dirty；保留上一阶段未提交修改及本记录，`.real-diagnostic-temp` 未触碰。

## 本次修改

- 在紧凑事实抽取的 Pydantic 校验失败处生成安全摘要：只保留归一化字段路径、错误类型、计数、总数和截断标记。
- 数组下标统一归一化为 `*`，不使用或保存 Pydantic 的 `msg`、`input`、`ctx`、原始响应或事实值。
- 将摘要仅作为内部异常属性和下一次结构纠错提示传递；不新增业务错误码、公开 API、数据库字段或诊断模块。
- DRAFT_REVIEW 失败日志在摘要存在时追加安全摘要；对外仍返回 `DYNAMIC_CHECK_INCOMPLETE`，不暴露内部详情。
- 生产配置默认值未改变；本次真实诊断显式设置 `LLM_STRUCTURE_RETRY_ATTEMPTS=1`，测试分别覆盖 0 次和 1 次纠错。

## 定向验证

| 命令 | 结果 |
|---|---|
| `.venv\Scripts\python.exe -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_template_checks.py tests/unit/test_comparison.py tests/unit/test_result_schema_v21.py tests/unit/test_risk_model.py tests/unit/test_result_advice.py -q` | 146 passed；1 个既有依赖弃用告警 |
| `ruff check` 变更 Python 文件 | 通过 |
| 变更 Python 文件定向 `compileall` | 通过 |
| `git diff --check` | 通过 |

测试覆盖安全路径摘要、`facts.*.value_type / literal_error`、摘要不含非法原值和 Pydantic 原始字段、0 次不重试、1 次纠错成功，以及工作流公开错误和内部日志边界。

## 唯一一次真实三文件诊断

固定使用目标合同、同名模板和 `项目方案确认函.docx`，设置 `LLM_SAME_MODEL_DIAGNOSTIC=true`、`LLM_MAX_OUTPUT_TOKENS=4096`、`LLM_STRUCTURE_RETRY_ATTEMPTS=1`、OCR 关闭；未增加辅助资料，未执行第二个真实任务。

### 安全聚合指标

- LLM HTTP 调用：3 次；其中结构纠错调用 1 次。
- 调用阶段：全部为 `FACT_EXTRACTION`；失败位置为目标合同第 2 个抽取分块。
- 阶段耗时：约 97,585 ms。
- 请求字符数：合计 145,938，单次最大 59,123。
- 模型响应字符数：合计 8,736。
- `finish_reason`：3 次均为 `stop`；截断：否。
- usage tokens：prompt 32,823；completion 2,856；total 35,679。
- 已完成阶段：下载、解析、模板比较、事实抽取调用。
- 未完成阶段：事实评审、跨文档映射、映射评审、语义规划、数值执行、建议生成。
- 失败类别：`LLM_RESPONSE_SCHEMA_INVALID`；对外错误仍为 `DYNAMIC_CHECK_INCOMPLETE`。
- 正式差异项：0；左右证据完整性：未产生正式结果；AI 建议：0。
- 诊断数据未进入正式风险、通过项或独立共识。

本次只保留上述安全聚合指标；未输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL。

## 结论与下一步

一次结构纠错未能使目标合同第 2 分块通过紧凑抽取 Schema，按要求立即停止真实调用。本阶段未跑通左右差异和一句建议链路；后续应先使用离线构造响应进一步覆盖该 Schema 失败形态，并在获得新的真实调用授权前不重复执行三文件任务。

未执行全仓测试、Docker、OCR、前端检查、MiniMax 或完整五文件任务；本阶段不提交、不推送。
