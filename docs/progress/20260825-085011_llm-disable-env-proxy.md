# 任务进度：LLM 客户端禁用环境代理

## 基本信息

- 时间：2026-08-25 08:50:11 +08:00
- 状态：COMPLETED
- 任务类型：FIX / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；开始任务前已有多项无关未提交修改，本任务仅修改 LLM 客户端、对应单元测试并新增本记录

## 用户目标

将 `OpenAIContractLlmClient` 的所有真实 HTTP Client 统一设置为 `trust_env=False`，避免读取宿主机系统代理导致内网 LLM 请求超时或返回 502；增加不调用真实合同文件的定向回归测试，并保持下载器和 OCR 的既有代理策略不变。

## 本次完成

- 为模型列表探测使用的 `httpx.AsyncClient` 显式设置 `trust_env=False`。
- 为结构化 completion 共用的 `httpx.AsyncClient` 显式设置 `trust_env=False`，覆盖事实抽取、事实评审、事实映射、映射评审和建议生成。
- 新增定向回归测试，通过 `MockTransport` 执行模型探测和结构化 completion，并验证两处生产客户端构造均收到 `trust_env=False`。
- 未修改下载器或 OCR 客户端，未调用真实 LLM、OCR 或合同文件。

## 修改文件

- `app/adapters/llm/openai_client.py`：禁用 LLM 客户端的环境代理读取。
- `tests/unit/test_openai_llm_client.py`：增加两条 HTTP 客户端创建路径的代理配置回归测试。
- `docs/progress/20260825-085011_llm-disable-env-proxy.md`：记录本次修复和验证结果。

## 接口、数据和配置变化

- API：无公开 API Schema 变化。
- 数据库/迁移：无。
- 配置：无新增配置；LLM HTTP 请求不再读取系统代理环境变量。
- 兼容性：下载器和 OCR 代理策略保持不变；显式注入的测试 transport 继续受支持。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest tests/unit/test_openai_llm_client.py -q` | 通过 | 9 passed in 0.58s；仅使用 MockTransport |
| `python -m ruff check app/adapters/llm/openai_client.py tests/unit/test_openai_llm_client.py` | 通过 | All checks passed |
| `python -m compileall -q app/adapters/llm/openai_client.py tests/unit/test_openai_llm_client.py` | 通过 | 无输出，退出码 0 |
| `git diff --check -- app/adapters/llm/openai_client.py tests/unit/test_openai_llm_client.py` | 通过 | 无空白错误；Git 仅提示工作区 LF/CRLF 转换策略 |

## Docker 与运行状态

- API：未启动、停止或重启。
- Worker：未启动、停止或重启。
- PostgreSQL：未操作。
- 控制台：未操作。
- 最终是否保持运行：本任务未改变任何服务状态。

## 重要决策

- 在每个 `OpenAIContractLlmClient` 自建 `httpx.AsyncClient` 的位置显式传入 `trust_env=False`，不把该行为扩散到全局 HTTP 配置。
- 回归测试拦截实际生产构造路径并使用内存 MockTransport 返回安全结构化响应，不依赖本机代理状态或外部网络。

## 已知问题与风险

- 未执行真实 LLM 调用；用户已提供同一 API Key 下直连成功的近期证据，本次按定向回归范围不重复消耗外部调用。
- 未执行全仓 pytest 或 Docker 验收；本次是局部客户端参数修复，已按快速开发检查完成定向验证。

## 下一步建议

1. 在后续里程碑验收或服务重启后，使用不含合同正文的最小 `/v1/models` probe 确认部署环境直连行为。

## 下一会话首先阅读

- `app/adapters/llm/openai_client.py`
- `tests/unit/test_openai_llm_client.py`
- `docs/progress/20260825-085011_llm-disable-env-proxy.md`

## 交接摘要

`OpenAIContractLlmClient` 的模型探测与结构化 completion 客户端均已显式使用 `trust_env=False`。定向单元测试覆盖两条创建路径并验证参数，9 项测试通过；Ruff、编译和 diff 检查通过。下载器、OCR、服务状态和真实合同文件均未触碰。
