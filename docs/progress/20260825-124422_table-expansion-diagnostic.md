# 扩展表格放行与三文件真实诊断

日期：2026-08-25

## 本阶段修改

- 模板检查对形状不一致的表格生成稳定的 `TABLE_STRUCTURE_EXPANDED` 差异项，保留两侧文件身份、表格位置、表格文本和差异分段，并标记确定的结构变化及人工复核要求。
- 扩展表格继续保留 `TEMPLATE_TABLE_STRUCTURE_EXPANDED` warning，但跳过不可靠的逐单元格必填规则；兼容表格的既有单元格检查不变。
- DRAFT_REVIEW 仅在整体对齐不可靠时终止，不再因扩展表格终止；结果风险、左右证据和建议链路继续接收结构差异。
- Python 和前端结果类型新增 `TABLE_STRUCTURE_EXPANDED`；表格通过项不会在存在结构差异时生成。
- 新增模板结果回归和含扩展表格的工作流合成回归；未改动 FINAL_COMPARE、确定性文字差异算法或公开任务请求结构。

## 合成验证

定向验证通过：

- `python -m pytest tests/unit/test_draft_review_workflow.py tests/unit/test_draft_template_checks.py -q`：24 passed。
- `python -m pytest tests/unit/test_comparison.py tests/unit/test_result_schema_v21.py tests/unit/test_risk_model.py tests/unit/test_result_advice.py -q`：71 passed。
- Ruff：变更 Python 文件通过。
- 定向 `compileall`、`npm --prefix frontend run typecheck`、`git diff --check`：通过。

合成用例确认扩展表格会继续进入事实抽取和建议链路，生成正式结构差异、风险和建议；同时不会生成扩展表格的逐单元格业务差异或“表格内容未发生变化”通过项。

## 唯一一次真实三文件诊断

输入固定为同一目标合同、同一模板和一份辅助资料，设置 `LLM_SAME_MODEL_DIAGNOSTIC=true`、`LLM_MAX_OUTPUT_TOKENS=4096`、结构重试次数为 0；未扩展辅助资料，未执行第二次真实任务。

- 总耗时：约 72.1 秒。
- LLM 调用次数：2，均为事实抽取。
- 抽取调用 1：请求 59,580 字符，Schema 3,058 字符，响应 5,274 字符，`finish_reason=stop`，prompt/completion/total tokens 为 13,344/1,238/14,582，未截断，约 19.4 秒。
- 抽取调用 2：请求 43,519 字符，Schema 3,058 字符，响应 10,110 字符，`finish_reason=stop`，prompt/completion/total tokens 为 9,850/2,851/12,701，未截断，约 49.7 秒。
- 已完成阶段：下载、解析、模板比较、事实抽取调用。
- 未完成阶段：事实抽取结果安全校验后以 `DYNAMIC_CHECK_INCOMPLETE` 终止，因此未进入映射、映射评审、语义规划、数值执行、正式差异结果或 AI 建议生成。
- 正式差异、正式风险、通过项和建议覆盖率：本次任务未生成正式结果。
- 真实脚本只保留上述聚合指标和安全错误代码，没有输出或保存合同正文、事实值、完整模型响应、响应片段或密钥；临时下载工作区已清理。

## 下一步

先基于安全错误定位本次事实抽取校验失败原因，并继续使用合成/离线输入加固；在获得明确修复和新的执行授权前，不重复本次真实三文件调用。确认真实抽取安全通过后，再安排三文件链路的后续验证，最后才考虑扩展辅助资料范围。
