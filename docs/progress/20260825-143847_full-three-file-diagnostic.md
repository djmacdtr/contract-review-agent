# 任务进度：完整三文件诊断

## 基本信息

- 时间：2026-08-25 14:38:47 +08:00
- 状态：PARTIAL
- 任务类型：DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`ab4f17a fix(draft-review): preserve safe validation diagnostics`
- 工作树状态：dirty；仅新增本次诊断进度记录，`.real-diagnostic-temp` 未触碰。

## 阶段提交

本阶段已有安全摘要、表格证据回查、事实错误分类、工作流日志和定向测试已形成提交：

- `ab4f17a fix(draft-review): preserve safe validation diagnostics`
- 提交包含 6 个代码/测试文件和 3 个此前进度记录。
- 未提交或推送 `.real-diagnostic-temp` 及其他无关内容。

## 定向验证

提交前已完成既定验证：

- 定向 pytest：148 passed；1 个既有 LangGraph 弃用告警。
- 变更 Python 文件 Ruff：通过。
- 变更 Python 文件定向 compileall：通过。
- `git diff --check`：通过。
- 未执行全仓 pytest、Docker、OCR、MiniMax、前端视觉验收或其他真实任务。

## 唯一完整三文件真实诊断

固定使用目标合同、同名模板和 `项目方案确认函.docx`，设置 `LLM_SAME_MODEL_DIAGNOSTIC=true`、`LLM_MAX_OUTPUT_TOKENS=4096`、`LLM_STRUCTURE_RETRY_ATTEMPTS=1`、OCR 关闭；未增加辅助文件。总实际 LLM 请求上限为 16 次，首次真实失败立即停止。

### 安全聚合结果

- 实际 LLM 请求：4 次；其中结构纠错 1 次。
- 阶段：下载、解析、模板比较、事实抽取。
- 失败位置：目标合同第 3 个抽取分块。
- 总耗时：约 49,077 ms。
- 请求字符数：合计 187,602，单次最大 59,123。
- 响应字符数：合计 8,531。
- `finish_reason`：4 次均为 `stop`；截断：否。
- usage tokens：prompt 42,202；completion 2,614；total 44,816。
- 失败类别：`LLM_RESPONSE_SCHEMA_INVALID`。
- 结构化日志捕获：`validation_summary_status=MISSING`，安全摘要项为空；没有可记录的真实 `path/error_type/count`。
- 未进入：事实评审、跨文档映射、映射评审、语义规划、数值执行和建议生成。
- 正式差异、左右证据和 AI 建议：0 / 未生成。

摘要缺失表示本次失败没有产生可用的 Pydantic 字段级摘要，不能据此推测是 `literal_error`、`too_long`、`extra_forbidden` 或其他具体约束，也未修改 Prompt、Schema、字段枚举或业务逻辑。

本次仅记录上述安全指标；未输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL。

## 结论与下一步

- 阶段提交已完成，真实完整链路仍为 PARTIAL。
- 本次真实失败没有字段级安全摘要，不进行针对性 Schema 修复，不继续消耗模型调用。
- 后续需先确认无摘要的现有错误路径是否需要安全地保留既有内部错误类型，再决定是否需要新的受控诊断授权；不扩大辅助文件范围。
- 若后续获得真实字段摘要，先用合成响应复现并通过定向测试，再另行执行端到端验证。

本阶段不再提交、不推送。
