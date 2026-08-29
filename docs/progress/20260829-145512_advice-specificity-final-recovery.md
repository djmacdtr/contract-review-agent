# Advice Canary 动态业务锚点修正与最终验收记录

## 状态

BLOCKED：唯一 Advice Canary 仍有一项未通过具体性门禁，未创建最终页码恢复任务。

## 实施内容

- Canary 不再要求建议原样包含风险标题或差异标题。
- 从相关差异两侧文本、删除/新增片段、缺失摘要和关联文件名提取动态锚点。
- 支持业务词、金额、比例、日期、期限、编号和字母数字标识。
- 过滤通用结构词和既有 Advice 空话黑名单。
- 数值和编号使用边界匹配，避免短数字误命中更长数值。
- 未修改 Advice Prompt、Schema、模型、Token、生产质量门或公开接口。

## 离线验证

- 动态锚点与 Advice 结果定向测试：`19 passed`。
- DRAFT_REVIEW Advice 定向测试：`4 passed`。
- Ruff：通过。
- compileall：通过。
- `git diff --check`：通过；仅有既有工作树的换行格式提示。
- 未运行全量测试或其他外部探针。

## 唯一 Canary

- 来源结果任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`。
- 生产范围批次：8 项风险。
- HTTP 调用：1 次，状态 `200`。
- 配置模型：`GLM-5.3-Flash`。
- 响应模式：`json_object`。
- `finish_reason`：`stop`。
- 返回项：`8/8`，唯一项：`8`。
- Advice 逐项质量计数：`DUPLICATED=0`、`INTERNAL_ID=0`、`MULTI_SENTENCE=0`、`TECHNICAL_TERM=0`。
- 动态具体性计数：`7/8`。
- 首个安全失败码：`ADVICE_CANARY_NOT_SPECIFIC`。

## 未完成项

- 未执行 `tsk_01M15ZWY9NMWZEQ5K9DWK5W56V` 的最终恢复任务。
- 未验证最终报告的 39 项风险、3 项通过、页码、证据、建议覆盖率和控制台地址。
- 按止损规则未再次调用外部模型、未调用 retry、未创建第二个任务。

## 工作树保护

保留现有未提交修改和 `.real-diagnostic-temp/`；未执行 commit、push、reset、clean 或清理临时诊断目录。
