# 任务进度：映射阶段短链路恢复与唯一验收

## 基本信息

- 时间：2026-08-27 12:12:04 +08:00
- 状态：PARTIAL
- 任务类型：FIX / TEST / 里程碑验收
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`23246f2`（本轮未提交）
- 工作树状态：dirty；保留既有页码、前端、checkpoint 和验收修改

## 用户目标

修正跨资料映射阶段的安全错误传播和阶段标识，固定宿主机 Worker 使用 `json_schema`，并以 `tsk_01M10HF8CBD9HSJXYNCAZ7989E` 作为来源执行唯一一次恢复任务。

## 本次完成

- 映射进度从 `FACT_EXTRACTION / 80%` 修正为现有公开阶段 `CROSS_VALIDATE / 80%`。
- 映射失败现在持久化 `FACT_MAPPING`、文件 ID、链路、深度、单元数、底层失败码、请求次数和结构重试次数。
- 映射及映射评审 Schema 校验补充安全 `LLM_RESPONSE_SCHEMA_INVALID` 子码。
- Worker 启动日志仅记录 LLM 有效响应模式、结构化输出开关和模型名。
- 本机 `.env` 仅将 `LLM_RESPONSE_FORMAT` 和 `LLM_NATIVE_STRUCTURED_OUTPUT` 固定为 `json_schema` / `true`；未修改 API Key。
- 完成一次且仅一次真实 retry；未调用 retry 接口第二次，未创建第二个恢复任务。

## 修改文件

- `app/workflows/draft_review.py`：映射阶段、错误子码和安全详情。
- `app/adapters/llm/openai_client.py`：映射 Schema 错误子码。
- `app/worker/runner.py`：Worker 启动安全配置日志。
- `.env`、`README.md`：结构化输出配置和部署说明。
- `tests/unit/test_draft_review_workflow.py`、`tests/unit/test_openai_llm_client.py`：映射错误与 Schema 回归。

## 接口、数据和配置变化

- API：retry 路由、请求和响应结构未修改。
- 数据库/迁移：未修改；错误详情继续使用既有 JSON 字段。
- 配置：宿主机 `.env`、`.env.example` 和部署说明明确使用 `json_schema`；正式文件域名白名单未改为 `fixture-server` 或通配符。
- 兼容性：未修改 checkpoint、页码、差异算法、事实 ID 或 checkpoint 身份。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest -q tests/unit/test_draft_review_workflow.py tests/unit/test_openai_llm_client.py` | 通过 | `79 passed`，使用 `contract-review-agent-py312` 环境 |
| 变更文件 Ruff | 通过 | 变更文件无 Ruff 错误 |
| `python -m compileall -q app` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无 diff 空白错误 |
| `docker compose --profile tools run --rm --build test` | 通过 | `375 passed` |
| 宿主机直接运行 Worker 集成测试 | 未通过 | 9 项因宿主机无法解析 Compose 服务名 `postgres`；未修改业务代码规避 |

## 唯一真实恢复

- 来源任务：`tsk_01M10HF8CBD9HSJXYNCAZ7989E`
- 唯一恢复任务：`tsk_01M10NSNATMYNP4KZPFX6QE1ER`
- retry POST：仅调用 1 次；之后仅使用 GET 轮询。
- 运行路径：Docker Worker 停止；宿主机 Worker、宿主机本地文件服务、Docker PostgreSQL。
- 宿主机 Worker 实际配置：`LLM_ENABLED=true`、`LLM_RESPONSE_FORMAT=json_schema`、`LLM_NATIVE_STRUCTURED_OUTPUT=true`、`ALLOW_HTTP_DOWNLOADS=true`、下载白名单为 `127.0.0.1`；日志未记录密钥。
- 任务结果：`FAILED / FACT_EXTRACTION / 75%`，未进入映射或建议阶段。
- 安全错误：`chain=text`、`batch_depth=0`、`unit_count=1`、`failure_code=LLM_OUTPUT_TRUNCATED`、`failure_stage=FACT_EXTRACTION`。
- checkpoint 计数：来源任务 `profile-v2=3`、`numeric-v2=21`、`text-v4=43`；恢复任务失败时为 `profile-v2=3`、`numeric-v2=22`、`text-v4=18`，未证明抽取阶段调用数为 0。
- 未生成正式报告，因此 39 项差异、4 项通过、页码、局部高亮和建议覆盖率本轮均未验收。
- 任务失败后仅做了一项离线安全修正：确保带普通 JSON 解析 cause 的 `LLM_INVALID_JSON` 不会被错误降级为异常类型；映射回归重新通过 `79 passed`，未再次调用外部服务。

## Docker 与运行状态

- API：运行，`/health` 和 `/ready` 均为 200。
- Worker：已恢复为 Docker Worker，启动日志确认 `json_schema` 和原生结构化输出开启。
- PostgreSQL：运行且 healthy。
- fixture-server：原有 Compose 服务仍运行；正式下载白名单未包含 `fixture-server`。
- 宿主机 Worker、本地 18081 文件服务：已停止。
- `.real-diagnostic-temp/`：未清理、未修改。
- 控制台视觉验收：未执行；按仓库约定由用户手工负责。

## 已知问题与风险

- 本次恢复未命中来源任务的全部当前抽取 checkpoint，实际仍执行了文本抽取并因 `LLM_OUTPUT_TRUNCATED` 失败。
- 因任务未到映射阶段，映射错误传播修复没有真实任务证据，但已有离线回归覆盖。
- 本轮 Docker Worker 镜像在唯一任务结束后按当前工作树重建并恢复；当前常驻 Docker Worker 的页码开关由现有 `.env`/Compose 默认值控制，未在本轮扩大页码配置范围。
- 未提交本轮代码和进度记录；未 push。

## 下一步建议

1. 不要 retry `tsk_01M10NSNATMYNP4KZPFX6QE1ER`，也不要在本轮创建第二个真实任务。
2. 后续如继续处理，应先离线定位当前 `text-v4` checkpoint 规划/命中差异和 `LLM_OUTPUT_TRUNCATED`，再单独取得新的真实任务授权。
3. 只有正式任务成功完成 39/4、页码、局部高亮和建议覆盖率后，才提交页码/映射里程碑。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260827-121204_mapping-recovery-acceptance.md`
- `docs/progress/20260827-113410_delivery-blocker-root-cause.md`
- `app/workflows/draft_review.py`
- `app/draft_review/extraction.py`

## 交接摘要

本轮已完成映射诊断代码、`CROSS_VALIDATE` 阶段标识、Schema 子码和 json_schema 配置固化。
离线测试 `375 passed`。
唯一真实恢复任务 `tsk_01M10NSNATMYNP4KZPFX6QE1ER` 失败于文本抽取最小分片。
明确错误为 `LLM_OUTPUT_TRUNCATED`，不是映射错误。
恢复任务未进入映射、建议或正式结果阶段。
未 retry、未创建第二个任务、未 commit、未 push。
Docker Worker 已恢复，API/PostgreSQL 健康。
宿主机 Worker 和本地文件服务已停止。
