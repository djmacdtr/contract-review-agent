# 任务进度：真实 Schema 失败安全取证

## 基本信息

- 时间：2026-08-25 14:30:37 +08:00
- 状态：PARTIAL
- 任务类型：FIX / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`926afac fix(draft-review): release expanded table checks`
- 工作树状态：dirty；保留现有并行修改和历史进度记录，`.real-diagnostic-temp` 未触碰。

## 用户目标

修复事实抽取失败链路中的安全摘要传播，使用严格限制的真实三文件诊断获取实际 Schema 失败原因；不修改 Prompt、Schema、字段枚举或业务逻辑。

## 本次完成

- 工作流现在同时从 `LlmClientError` 和裸 Pydantic `ValidationError` 生成安全日志摘要。
- 日志摘要只投影 `path`、`error_type`、`count`；Schema 失败额外记录摘要 `PRESENT`/`MISSING` 状态。
- 保持对外 `DYNAMIC_CHECK_INCOMPLETE`，未增加业务错误码、公开字段、数据库字段或接口。
- 新增裸校验失败、摘要缺失和原始值不泄露的定向测试。

## 修改文件

- `app/workflows/draft_review.py`：补齐裸 ValidationError 摘要和安全日志投影。
- `tests/unit/test_draft_review_workflow.py`：增加日志摘要存在、缺失和裸校验失败用例。
- 其他已有修改文件保持不回退、不覆盖。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化。
- 配置：生产默认值未变化；本次诊断临时使用结构重试 0、最多 2 次 LLM 调用。
- 兼容性：公开错误仍为 `DYNAMIC_CHECK_INCOMPLETE`；客户结果和业务检查逻辑未改变。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 Python pytest | 通过 | 148 passed；1 个既有 LangGraph 弃用告警 |
| 变更 Python 文件 Ruff | 通过 | 无 lint 错误 |
| 变更 Python 文件定向 `compileall` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误 |
| 全仓 pytest / Docker / OCR / 前端 / MiniMax | 未执行 | 按本阶段范围排除 |

## 唯一一次受限真实取证

固定使用目标合同、同名模板和 `项目方案确认函.docx`，设置 `LLM_MAX_OUTPUT_TOKENS=4096`、`LLM_STRUCTURE_RETRY_ATTEMPTS=0`、OCR 关闭；未增加辅助资料。

- 实际 LLM 调用：2 次；均为首轮事实抽取，无结构纠错调用。
- 阶段：下载、解析、模板比较、事实抽取；在第 2 次调用后达到调用上限。
- 调用总耗时：约 39,607 ms。
- 请求字符数：合计 102,321，单次最大 59,123。
- 响应字符数：合计 5,858。
- `finish_reason`：2 次均为 `stop`；截断：否。
- usage tokens：prompt 22,977；completion 1,791；total 24,768。
- 两次抽取均未触发 Schema 失败日志，因此实际 `validation_summary` 为空；这表示本次上限内没有暴露失败原因，不表示摘要内容丢失。
- 达到调用上限后未继续第三次抽取，也未进入事实评审、映射、映射评审、语义规划、数值执行或建议生成。
- 正式差异、左右证据和 AI 建议：本次未生成。

本次只捕获安全聚合指标；未输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL。

## 已知问题与下一步

- 真实失败分块仍未定位；本次前两个分块均通过，调用上限阻止了后续分块，因此没有可用于 Schema 定向修复的真实摘要。
- 已完成离线摘要缺失保护，但未修改 Prompt、Schema 或模型行为。
- 后续需在获得新的真实调用授权后，使用更高但仍受控的抽取分块上限获取实际安全摘要；获得摘要后先离线复现并只修复对应校验边界，再另行执行端到端验证。

本阶段不提交、不推送。
