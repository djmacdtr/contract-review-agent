# 恢复 OCR/LLM 配置并探测上游

## 目的

恢复验收前被关闭的 LLM/OCR 开关，重建 API/Worker，并执行不含合同内容的健康、模型和合成 OCR 探测。

## 实际操作

- 将 `.env` 中 `LLM_ENABLED`、`OCR_ENABLED` 从 `false` 恢复为 `true`。
- 执行 `docker compose up -d --build api worker`。
- 未执行 `down -v`、卷删除、数据库清空或迁移破坏性操作。
- 未输出密钥、合同正文、完整上游响应或完整 URL。

## 结果

- Settings：`llm_configured=true`，`ocr_configured=true`。
- PostgreSQL：healthy，数据未清理。
- API：running/healthy；`/health` 成功。
- Worker：running。
- `/ready`：数据库 `ok`，OCR/LLM 配置均为 `true`。
- 模型列表探测：失败。宿主机安全错误码为 `LLM_UPSTREAM_ERROR`；API 容器安全错误码为 `LLM_NETWORK_ERROR`。未确认抽取、独立评审和 advice 三个模型，不执行真实 LLM 文档调用。
- 合成单页 OCR probe：已进入实际 OCR 解析接口，返回 `OCR_RESPONSE_INVALID`，响应不是客户端预期结构。未执行真实合同 OCR 基线。

## 判断与未解决风险

- 原先无法使用的直接原因是两个启用开关为 `false`，该问题已恢复。
- 当前剩余问题属于上游连通性/协议响应：宿主机到 LLM 端口可达但请求异常；Docker API 容器到 LLM 返回网络错误；OCR 接口返回非预期响应结构。需由上游网关/网络管理员确认服务状态、容器路由和接口协议后再继续验收。
- 在模型列表和 OCR probe 都通过前，不运行单文档双模型或五文件 HYBRID。

## 最终服务状态

API、Worker、PostgreSQL 均保持运行；未进行浏览器或视觉验收。
