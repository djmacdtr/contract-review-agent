# 任务进度：LLM 网关配置与文档诊断

## 基本信息

- 时间：2026-08-24 09:32:31 +08:00
- 状态：DIAGNOSED
- 任务类型：DIAGNOSE / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保护全部既有未提交修改，本次未改代码或配置

## 用户目标

核对当前 LLM 的实际配置，确认是否使用 GLM-5.2，并依据第三方接口文档定位宿主机 LLM 连通性失败原因。

## 本次完成

- 完整阅读并抽取 `第三方接口文档/集团内部LLM网关使用指南.pdf` 的 21 页内容，渲染抽查关键页面后删除全部临时图片。
- 以脱敏方式读取宿主机当前生效配置，仅记录协议、模型 ID、地址结构和超时，不输出密钥或完整地址。
- 对 `/v1/models`、显式模型 `GLM-5.2` 和场景别名 `text` 执行最小合成请求，只输出 HTTP 状态码或异常类型，未发送合同内容。
- 对照网关文档的模型清单、OpenAI 兼容端点及错误码定义完成归因。

## 配置核对

| 配置项 | 当前值/状态 | 文档结论 |
|---|---|---|
| 协议 | `openai` | 匹配 OpenAI 兼容接口 |
| Base URL 结构 | `http://<host>:<port>/v1` | 匹配文档 |
| 抽取模型 | `GLM-5.2` | 文档明确列出，合法 |
| 建议模型 | `GLM-5.2` | 文档明确列出，合法 |
| 评审模型 | `GLM-5.2-reviewer` | 文档未列出，属于无依据的模型 ID |
| 原生结构化输出 | 关闭 | 请求不会携带可疑的 `response_format` |
| 超时 | 300 秒 | 高于文档建议的至少 60 秒 |

文档列出的真实模型为 `GLM-5.2`、`Gemma-4-31B` 和 `MiniMax-M2.7`，另支持 `agent`、`text`、`embedding`、`rerank` 场景别名。文档未出现 `GLM-5.2-reviewer`。

## 测试与验证

| 检查 | 结果 | 说明 |
|---|---|---|
| 当前客户端 `probe_models()` / completion 错误映射复核 | `LLM_UPSTREAM_ERROR` | 客户端将 HTTP 5xx 统一映射为该错误 |
| 默认宿主机路径 `GET /v1/models` | HTTP 502 | 文档定义为后端服务不可用 |
| 默认宿主机路径 `POST /v1/chat/completions`，模型 `GLM-5.2` | HTTP 502 | 合法模型也失败，排除评审模型名导致当前首阶段失败 |
| 默认宿主机路径 `POST /v1/chat/completions`，模型 `text` | HTTP 502 | 场景别名同样失败，支持网关/后端故障判断 |
| `trust_env=false` 直连模型列表 | `RemoteProtocolError` | 对端未返回完整 HTTP 响应 |
| `trust_env=false` 直连两个 completion | `ReadError` | 对端读取阶段断开 |
| 临时 PDF 渲染文件清理 | 通过 | 5 个 PNG 已删除，临时目录已移除 |

## 根因判断

1. 当前直接阻塞是 LLM 网关或其模型后端不可用。按文档，HTTP 502 明确定义为“后端服务不可用”，应由 AI 平台团队处理。
2. 请求能够得到 HTTP 502，而错误密钥应为 401、错误路径或模型通常应为 404，因此当前首阶段故障不是密钥、`/v1` 路径或 `GLM-5.2` 模型 ID 导致。
3. `GLM-5.2-reviewer` 是独立的次要配置缺陷。网关恢复后，执行评审阶段时很可能返回 404；应改为文档列出的另一个真实模型，且必须满足项目的独立模型要求。
4. 绕过环境信任设置的直连表现为协议/读取断开，说明网络路径也需平台侧联合检查；应用当前实际请求路径获得的 502 已足以定位首要故障层级。

## 修改文件

- `docs/llm-gateway-reproduction.md`：补充本次真实请求参数，以及 Python、curl 复现方法。
- `docs/progress/20260824-093231_llm-gateway-doc-diagnosis.md`：新增本次诊断记录。

## 接口、数据和配置变化

- API：无。
- 数据库/迁移：无。
- 代码：无。
- 配置：无；未替换评审模型。
- 外部数据：仅发送“仅回复 OK”的合成文本，未发送任何合同内容。

## Docker 与运行状态

- 本次未启动、停止或调用 Docker 服务。
- API、Worker、PostgreSQL 保持原有停止状态。

## 未解决问题

- 网关 `/v1/models`、`GLM-5.2` completion 和 `text` 别名 completion 均为 HTTP 502。
- 文档未提供 `GLM-5.2-reviewer`；独立评审模型尚未改为真实可用模型，也尚未完成连通性验证。
- 只有平台侧恢复后才能验证真实模型返回结构与抽取/评审全链路。

## 下一步建议

1. 将三个 502 结果、测试时间和网关文档错误码定义提交给 AI 平台团队，检查网关反向代理、GLM 后端注册与零信任网络权限。
2. 平台恢复后先重跑 `/v1/models` 和一条合成 `GLM-5.2` completion，成功后再验证独立评审模型。
3. 经项目负责人确认后，将 `LLM_REVIEW_MODEL` 改为网关实际列出的不同模型；从当前清单看可优先评估 `Gemma-4-31B`，不要使用未登记的 `GLM-5.2-reviewer`。

## 交接摘要

当前抽取和建议均配置为文档支持的 `GLM-5.2`；评审配置为文档不存在的 `GLM-5.2-reviewer`。宿主机实际请求 `/v1/models`、`GLM-5.2` completion 和 `text` alias completion 均返回 HTTP 502，按供应商文档即后端服务不可用，是当前直接阻塞；评审模型名是网关恢复后还需修正的次级问题。
