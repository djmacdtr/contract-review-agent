# DeepSeek 模型配置切换

## 完成内容

- 将事实抽取、评审和建议模型统一设置为 `DeepSeek-V4-Flash-0731`。
- 已同步修改本机 `.env`、`.env.example`、应用默认配置、Mock 默认模型、README 和相关测试预期。
- 当前运行配置中不再存在 `GLM-5.2` 或 `GLM-5.2-reviewer` 引用。

## 验证

- 配置加载结果：extraction、review、advice 均为 `DeepSeek-V4-Flash-0731`。
- 定向测试：`40 passed`。
- Ruff、compileall、`git diff --check`：通过；仅有既有 Windows 行尾提示。

## 边界

- 未调用真实 LLM、OCR 或其他甲方服务。
- 未重建或重启 Docker 服务；运行中的容器仍使用启动时加载的旧配置，需在后续正式切换流程时重建或重启生效。
- 未修改工作流、数据库、公开接口或业务结果。
- 未 commit、push、reset 或清理现有工作区。
