# 任务进度：共识回归补强与外部验收门

## 基本信息

- 时间：2026-08-22 17:10:07 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；保留前序未提交修改和本次修改，未清理、未回退、未提交

## 用户目标

补齐 DRAFT_REVIEW 双模型共识、advice 证据过滤、动态数值开关的回归测试，并按 PostgreSQL、全量测试、模型/OCR probe、真实五文件解析、单文档 LLM、五文件 HYBRID 的顺序执行验收。

## 本次完成

- 为独立评审失败、模型不独立、主/评审低置信度、证据不完整新增回归；均断言不产生事实自动风险或通过，且进入人工复核。
- 为非法 advice `evidence_refs` 新增回归和通用修复：无效引用被过滤，追加复核 warning/item，并同步结论、摘要描述和统计。
- 为 `check_numeric_consistency=false` 新增回归，确认动态数值规则不产生检查、风险、复核或通过项。
- 仅启动 `postgres`，未执行卷删除、数据库清空或 Compose down。
- 执行无合同内容模型列表探测、合成单页 OCR probe，以及关闭 LLM 的真实五文件解析基线。

## 修改文件

- `app/workflows/draft_review.py`：统一刷新 advice 后的结论、统计和摘要；非法 advice 证据进入人工复核。
- `tests/unit/test_draft_review_workflow.py`：增加共识门、advice 引用和数值开关回归覆盖。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化；未清空数据库。
- 配置：无变化。
- 兼容性：无效 advice 证据现在明确形成 `LLM_ADVICE_EVIDENCE_REVIEW_REQUIRED` warning 和 `LLM_ADVICE_EVIDENCE_UNVERIFIED` 人工复核项。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest -q tests\unit\test_draft_review_workflow.py` | 通过 | `9 passed, 1 warning` |
| `.venv\Scripts\python.exe -m pytest -q tests\unit` | 通过 | `143 passed, 1 warning` |
| `.venv\Scripts\python.exe -m pytest -q` | 部分通过 | `143 passed, 15 errors, 1 warning`；15 个 integration fixture 在宿主机解析 Compose 内部主机 `postgres` 时失败，未进入业务断言 |
| `ruff check app\workflows\draft_review.py tests\unit\test_draft_review_workflow.py` | 通过 | 无问题 |
| `.venv\Scripts\python.exe -m compileall -q app` | 通过 | 无输出 |
| `git diff --check` | 通过 | 无空白错误；仅工作树 CRLF 提示 |
| `OpenAIContractLlmClient.probe_models()` | 失败并安全映射 | `LLM_UPSTREAM_ERROR`；未发送合同内容，未输出凭据 |
| 合成单页 `scripts.ocr_live_probe` 等价调用 | 失败并安全映射 | `OCR_NOT_CONFIGURED`；仅合成内容 |
| 真实五文件解析基线，LLM 关闭 | 部分通过 | 4 DOCX 成功；唯一 PDF 因 `OCR_NOT_CONFIGURED` 安全失败 |

## Docker 与运行状态

- PostgreSQL：通过 `docker compose up -d postgres` 启动，最终 `healthy`。
- API：未启动，保持 Exited。
- Worker：未启动，保持 Exited。
- 控制台：未启动；浏览器/截图/视觉验收由用户手工负责。
- 最终是否保持运行：仅 PostgreSQL 保持运行。

## 真实解析基线

- TARGET DOCX：276 blocks、4 tables、`python-docx`；1 个合并单元格简化 warning。
- TEMPLATE DOCX：276 blocks、4 tables、`python-docx`；3 个合并单元格简化 warning。
- 法律合规报告 DOCX：80 blocks、1 table、`python-docx`；无 warning。
- 项目方案确认函 DOCX：10 blocks、1 table、`python-docx`；1 个合并单元格简化 warning。
- 评审意见 PDF：外部解析器未配置，返回 `OCR_NOT_CONFIGURED`；未回退为本地 PDF 文本解析。

## 已知问题与风险

- 宿主机全量 pytest 不能解析 Docker Compose 服务名 `postgres`；容器本身 healthy，integration fixture 需在 Compose `test` 容器或显式宿主机数据库地址下运行。
- `/v1/models` 返回 `LLM_UPSTREAM_ERROR`；未确认抽取、独立评审和 advice 三个配置模型可用，故未执行真实单文档 LLM 或五文件 HYBRID。
- OCR 未配置，PDF 解析和完整五文件基线未完成；不得以 DOCX 局部成功宣称五文件验收通过。

## 下一步建议

1. 恢复 LLM 网关后先重跑无合同内容 `/v1/models`，确认抽取、独立评审与 advice 三个模型 ID 都可用。
2. 配置外部 OCR 后重跑合成 OCR probe，再完成五文件解析基线。
3. 两个前置门通过后，用 `项目方案确认函.docx` 执行唯一一次单文档双模型事实抽取与证据人工抽查。
4. 单文档通过后，运行一次完整五文件 HYBRID；浏览器视觉验收仍由用户负责。

## 下一会话首先阅读

- `docs/progress/20260822-171007_consensus-regression-and-acceptance.md`
- `docs/progress/20260822-162945_dynamic-multifile-llm-consensus.md`
- `app/workflows/draft_review.py`
- `tests/unit/test_draft_review_workflow.py`

## 交接摘要

共识门、advice 非法证据和动态数值开关的回归已补齐，单元测试为 143 passed。
非法 advice 引用现在过滤后必然触发人工复核，结论和统计同步更新。
PostgreSQL 已启动且 healthy；API/Worker 未启动。
全量 pytest 仍是 143 passed、15 integration setup errors，宿主机不能解析 `postgres`。
模型列表探测为 `LLM_UPSTREAM_ERROR`，合成 OCR 为 `OCR_NOT_CONFIGURED`。
四份 DOCX 真实解析成功，PDF 因 OCR 未配置失败；真实 LLM 和完整 HYBRID 未执行。
