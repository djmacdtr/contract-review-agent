# Advice 校验诊断与最终恢复验收记录

## 状态

BLOCKED：唯一 Advice Canary 未通过，未创建最终恢复任务。

## 实施内容

- 在 `app/results/advice.py` 增加共享 Advice 逐项校验函数。
- 安全分类为 `MULTI_SENTENCE`、`DUPLICATED`、`INTERNAL_ID`、`TECHNICAL_TERM`。
- 多句建议仅做确定性换行合并和句间分号归一化；其他三类继续拒绝。
- DRAFT_REVIEW 生产路径与 Canary 共用逐项校验函数。
- 生产路径仅将安全计数写入 metadata；无效建议继续使用 fallback。
- Canary 失败分支保留接受数、分类计数和具体性计数，不保存建议正文或完整响应。

## 离线验证

- Advice、Canary 和 DRAFT_REVIEW 定向测试：`69 passed`。
- 最终诊断分支修正后的 Advice/Canary 定向测试：`15 passed`。
- Ruff：通过。
- compileall：通过。
- `git diff --check`：通过；仅有既有工作树的换行格式提示。
- 未运行全量测试、Text/Numeric Canary 或其他外部探针。

## 唯一 Canary

- 来源结果任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`。
- 正式生产范围批次：8 项风险。
- HTTP 调用：1 次，状态 `200`。
- 配置模型：`GLM-5.3-Flash`。
- 响应模式：`json_object`。
- `finish_reason`：`stop`。
- 失败阶段：`ADVICE_CANARY`。
- 首个安全失败码：`ADVICE_CANARY_NOT_SPECIFIC`。
- 该结果未触发 JSON、Schema、截断或四类 Advice 质量分类错误。

## 未完成项

- 未执行 `tsk_01M15ZWY9NMWZEQ5K9DWK5W56V` 的最终恢复任务。
- 未验证最终任务的页码、39 项风险、3 项通过、控制台报告和 Advice 覆盖率。
- 按止损规则未再次调用外部模型、未调用 retry、未创建第二个任务。

## 工作树保护

保留现有未提交修改和 `.real-diagnostic-temp/`；未执行 commit、push、reset、clean 或清理临时诊断目录。
