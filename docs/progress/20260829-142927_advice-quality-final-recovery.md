# Advice 质量提升与最终页码验收进度

## 状态

- 状态：BLOCKED（Advice Canary 未通过，未创建最终正式任务）
- 时间：2026-08-29 14:29:27 +08:00
- 分支：`feat/draft-review-multidoc`
- 工作树：dirty；此前用户修改全部保留
- Docker Worker：未运行
- API/PostgreSQL：只读核对时健康

## 本次实现

- Advice 增加内部响应格式覆盖，仅 `generate_advice` 使用 `json_object`；Numeric、Text、映射及其他请求保持原有模式。
- Advice 输出上限按首次 4096 Canary 截断后的约定提升为 8192。
- 新增真实生产范围 Advice Canary：按当前报告的正式 8 项批次选择 payload 最大批次，校验风险 ID 完整性、重复、严格 Schema、技术字段和具体性。
- 宿主机恢复脚本增加 Advice 格式覆盖和真实 DOCX 页码解析器配置；本次未进入正式恢复，因此未启用实际页码任务。
- 调用计数器仅保留 HTTP 状态、模型、响应格式和 `finish_reason` 等安全元数据，不保存响应正文。

## 定向验证

| 检查 | 结果 |
|---|---|
| Advice/映射客户端定向 pytest | 通过，13 项相关测试 |
| Advice Canary 辅助单元测试 | 通过 |
| Ruff | 通过 |
| compileall | 通过 |
| `git diff --check` | 待本记录后执行最终检查 |

## 真实 Canary

### 首次试探

- 范围：脚本错误选择了当前报告的 7 项末批，不满足正式 8 项门禁。
- HTTP：1 次，状态 200。
- 模型/格式：`GLM-5.3-Flash` / `json_object`。
- `finish_reason`：`length`。
- 处理：不视为有效 8 项 Canary；未创建正式任务。

### 最后一次合规 Canary

- 来源结果任务：`tsk_01M161GFY6Q7YSP07R877XQM2B`
- 范围：正式 8 项风险批次。
- 输出上限：8192。
- HTTP：1 次，状态 200。
- 模型/格式：`GLM-5.3-Flash` / `json_object`。
- `finish_reason`：`stop`。
- 结果：应用层 Advice 严格校验未通过，安全报告仅保留 `ValueError`，未保存正文或完整响应。
- 诊断文件：`.real-diagnostic-temp/advice-quality-canary-20260829.json`、`.real-diagnostic-temp/advice-quality-canary-20260829-8192.json`

根据止损规则，本次不再重放 Canary，不创建 retry 任务，不调用外部服务。

## 未完成项

- 未从 `tsk_01M15ZWY9NMWZEQ5K9DWK5W56V` 创建最终正式恢复任务。
- 未验证 100% 模型 Advice、0 fallback。
- 未执行最终任务的 DOCX 真实页码补全和控制台报告检查。
- 不能宣称 Advice 质量或最终页码验收闭环完成。

## 保护项

- 未修改公开 API、数据库 Schema、Numeric、Text、映射、Prompt 或 checkpoint 身份。
- 未执行 commit、push、reset、clean；未清理 `.real-diagnostic-temp/`。
