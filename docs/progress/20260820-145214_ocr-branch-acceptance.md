# 任务进度：FINAL_COMPARE OCR 分支收口与 46 页真实验收

## 基本信息

- 时间：2026-08-20 14:52:14 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-ocr`
- 当前提交：`7ca6ef2`（本记录将随第三组文档与验收脚本提交）
- 工作树状态：dirty；README、验收记录、方案、脚本及一处 Ruff 行宽修正待第三组提交
- Pull Request：`https://github.com/djmacdtr/contract-review-agent/pull/1`

## 用户目标

收口 FINAL_COMPARE OCR 分支，将工作流与规则版本升级为 `0.3.0`，补齐 OCR 边界验证，以 46 页文本 PDF 对扫描 PDF 完成一次宿主机真实纵向闭环，重建常驻服务并形成可审查 PR。本轮明确不进行甲方 Docker 网络验证，不扩展 DRAFT_REVIEW、LLM、异步 OCR 或复杂合同规则。

## 本次完成

- 审查 OCR Client、DTO、mapper、解析 Router、FINAL_COMPARE Workflow、控制台及相关测试，确认 API 路由、请求结构、数据库 schema 均未变化。
- 将 FINAL_COMPARE 的 `workflow_version` 与 `rules_version` 从 `0.2.0` 升级到 `0.3.0`；API 版本仍为 `0.2.0`，结果 `schema_version` 仍为 `1.0`。
- 为 OCR 结果补充安全可观测指标：响应字节数、block/table/cell 数、detail 页数及带 bbox 的块/单元格数；不保存供应商完整响应。
- 补充 502/503/504 有限重试、响应大小、无 detail、合并单元格及 0.3.0 版本断言。
- 修复控制台将 PDF 解析器错误标记为 `pypdf` 的问题，统一为 `pdfplumber`；保留 OCR 来源、引擎、置信度、warning、位置与原始 JSON 展示。
- 新增 46 页安全验收脚本，只输出任务 ID、耗时、页数、结构计数、置信摘要和 warning code，不输出合同文本、文件 URL、OCR 地址或鉴权值。
- 使用宿主机 Miniconda Python 3.12 API/Worker、Docker PostgreSQL 和本机只读 fixture server 完成一次真实 OCR 调用。
- 停止全部临时宿主机进程，恢复并重建默认 Compose API/Worker；数据库命名卷和真实任务结果均保留。
- 按计划形成三组交付：OCR 后端、控制台、文档与验收证据；建立 PR #1，未执行合并。

## 修改文件

- `app/adapters/document_parser/`：TextIn 同步客户端、宽松 DTO、响应映射、供应商无关协议与稳定错误码。
- `app/documents/router.py`、`app/documents/models.py`：本地优先解析与扫描 PDF OCR 回退，坐标/来源/置信度模型。
- `app/workflows/final_compare.py`：OCR 感知的真实比对、0.3.0 版本和限制说明。
- `app/core/config.py`、`.env.example`、`compose.yaml`：OCR 安全配置边界；示例不含真实值。
- `frontend/src/`：OCR 元数据、中文状态/枚举标签和任务详情展示。
- `tests/`：OCR Client、mapper、Router、Workflow、Worker、低置信度与边界回归。
- `scripts/ocr_live_probe.py`、`scripts/e2e_ocr_local.py`、`scripts/e2e_ocr_acceptance.py`：安全探测和宿主机闭环脚本。
- `README.md`、`docs/plans/20260820_ocr-document-parser-integration.md`、`docs/progress/`：运行说明、设计依据和跨会话记录。

## 接口、数据和配置变化

- API：路由和请求结构不变；FINAL_COMPARE 结果仍使用 schema 1.0，`mock=false`、`execution_mode=RULE_BASED`。
- 数据库/迁移：无 schema 变化；`alembic check` 未发现缺失迁移。
- 配置：复用 `OCR_ENABLED`、`OCR_BASE_URL`、`OCR_API_KEY`、`OCR_TIMEOUT_SECONDS`；新增/文档化 `OCR_AUTH_HEADER`、`OCR_MAX_RESPONSE_MB`、`OCR_HTTP_RETRY_ATTEMPTS`、`OCR_RETRY_BACKOFF_SECONDS`、`OCR_LOW_CONFIDENCE_THRESHOLD`。
- 兼容性：DOCX 与文本型 PDF 不调用 OCR；仅本地 PDF 解析触发 `OCR_REQUIRED` 时回退外部解析。OCR 未启用或未完整配置时保持安全失败。

## 46 页真实验收

只读输入：

- baseline：`融资租赁合同_电子印章示例_原版46页.pdf`
- target：`融资租赁合同_电子印章示例_原版46页_扫描版.pdf`

验收数据：

| 指标 | 结果 |
|---|---:|
| 任务 ID | `tsk_01M0EYR11PYDE4WKEY1XZTNP1Z` |
| 任务终态 | `SUCCEEDED / COMPLETED / 100` |
| 任务总耗时 | 52.391 秒 |
| OCR 服务耗时 | 42.980 秒 |
| baseline parser | `pdfplumber` |
| target parser | `textin-document-parser` |
| OCR 页数 | 46 / 46 |
| OCR 引擎版本 | `3.20.11` |
| OCR 响应大小 | 5,339,580 bytes |
| API 结果大小 | 1,091,386 bytes |
| block / table / cell | 383 / 4 / 2,189 |
| 带坐标 block / cell | 383 / 2,189 |
| 平均 / 最低置信度 | 0.9925 / 0.9341 |
| 结构化差异数 | 2,099 |
| 结论 | `RISK_FOUND` |

结果满足 `mock=false / RULE_BASED / workflow 0.3.0 / rules 0.3.0`。46 页全部存在有效 detail，未出现部分页失败，响应未超过 50 MB，任务总耗时未超过 600 秒。文件元数据与任务结果已持久化，API 重启后仍可查询。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| OCR 变更范围 Ruff | 通过 | 相关 Python 文件 `All checks passed` |
| 全仓 Ruff | 未通过（既有债务） | 44 个历史 E501 行宽问题；本轮唯一新增问题已修复 |
| Docker 测试镜像全量 pytest | 通过 | `55 passed, 1 warning`，约 3.70 秒 |
| OCR Client/mapper/Router/Workflow 边界 | 通过 | 覆盖旋转、低置信文字与数值、合并单元格、超时、502/503/504、响应过大、部分页失败、无 detail 和本地优先路由 |
| Vue typecheck | 通过 | `vue-tsc --noEmit` |
| Vue production build | 通过 | Vite 构建成功；主 JS 约 988 KB，有 chunk size warning |
| `docker compose config --quiet` | 通过 | Compose 配置有效 |
| runtime/test 镜像构建 | 通过 | `contract-review-agent:dev` 与 `:test` 构建成功 |
| Alembic check | 通过 | `No new upgrade operations detected` |
| `/health`、`/ready`、`/docs`、`/console/` | 通过 | 均为 HTTP 200 |
| 真实 OCR 日志脱敏检查 | 通过 | 未命中 Key、OCR Base URL、fixture 完整 URL、合同文件名或正文标记 |
| 临时目录清理 | 通过 | 任务结束后临时目录 0 条目 |
| API 重启持久化 | 通过 | 真实任务仍为 `SUCCEEDED / COMPLETED / 100` |
| PostgreSQL 命名卷 | 通过 | `contract-review-postgres-data` 保留，可 inspect |
| OCR 原始资料 SHA-256 | 通过 | HTML `8C6E5C...615A3`、OpenAPI JSON `EA29FFD...CEA06`，与任务前记录一致 |
| Git staged/working tree 敏感值扫描 | 通过 | 未发现 OCR Key 或内网地址进入业务代码、示例、脚本或文档 |
| 浏览器视觉验收 | 待人工 | 当前自动化会话无可连接浏览器；未用 HTTP 200 冒充视觉通过 |

唯一 pytest warning 为 LangGraph 上游 `allowed_objects` 默认值未来变化提示，不影响本轮运行。

## Docker 与运行状态

- API：`contract-review-api-1`，使用最新 `contract-review-agent:dev`，healthy，映射 `127.0.0.1:8000`。
- Worker：`contract-review-worker-1`，与 API 使用同一最新镜像，running。
- PostgreSQL：`contract-review-postgres-1`，healthy，继续使用命名卷 `contract-review-postgres-data`。
- 控制台：`http://127.0.0.1:8000/console/` 返回 200，最新前端已打入镜像。
- 最终是否保持运行：是。
- 未执行 `docker compose down -v` 或任何 volume 删除。

## 重要决策

- 本轮不进行甲方 Docker 网络环境验证；宿主机真实闭环作为当前验收依据，部署环境仍需单独验证。
- 文本 PDF 保持 `pdfplumber` 优先，只有低文本层扫描 PDF 才调用 OCR，避免不必要成本和数据暴露。
- 46 页数据暂不支持立即切换异步 OCR：当前单任务在 600 秒内有充分余量，响应远低于 50 MB；是否切换应结合约 200 页实测和队列等待数据决定。
- 低置信关键数值不会静默忽略，部分页失败或结构不完整不会放宽校验生成虚假成功。

## 已知问题与风险

- 2,099 个差异显示文本 PDF 与 OCR 的分段/表格结构差异会带来较高噪声；必须建立人工标注黄金集校准对齐和 warning 去重，不能把数量直接解释为 2,099 个合同风险。
- 当前只有一组 46 页样本。按本次大小和耗时线性外推，约 184–200 页可能仍在 600 秒与 50 MB 内，但这是推断，不是验收结果。
- 同步 OCR 会占用唯一 Worker 约一分钟；队列并发和大文件峰值尚未测量。
- 自动浏览器不可用，中文控制台的可视化点击检查仍需人工完成。
- 前端单包约 988 KB；不影响当前测试控制台，但后续可按路由和 Element Plus 组件做拆包。
- 全仓仍有 44 个既有 Ruff E501 行宽问题，后续应单独清理，避免与业务分支混杂。
- 甲方最终 Worker 容器网络链路未在本轮验证，且按用户要求不作为当前阻塞。

## 近 200 页与异步 OCR 建议

1. 暂不立即改为异步 OCR；46 页服务耗时 42.98 秒、响应 5.34 MB，当前同步实现有明确余量。
2. 下一次真实调用优先选一份约 184–200 页脱敏扫描合同，单并发执行并记录上传大小、服务耗时、响应大小、Worker 占用和内存。
3. 若接近 600 秒、接近 50 MB、出现网关同步超时，或单 Worker 队列等待不可接受，再设计供应商任务 ID 持久化和异步轮询迁移。
4. 在约 200 页调用前先用 46 页样本建立差异黄金集，降低 OCR 分段导致的噪声，否则更大样本只会放大不可解释差异。

## 下一步建议

1. 人工浏览器打开 46 页任务，检查中文状态、OCR 标签、引擎、置信度、warning、双方位置、坐标、差异项和原始 JSON。
2. 对 46 页结果做小规模人工标注，优化段落/表格对齐及重复 warning 汇总。
3. 在获得大文件调用窗口后执行一次约 184–200 页单并发验收，再决定同步/异步 OCR。
4. 在甲方最终部署环境补 Worker 容器到 OCR 的单页扫描 PDF 网络验收。
5. 下一里程碑启动 DRAFT_REVIEW 真实纵向切片：模板差异、辅助资料事实矩阵和跨文件校验；LLM 最后接入。

## 下一会话首先阅读

- `README.md`
- `docs/plans/20260820_ocr-document-parser-integration.md`
- `docs/progress/20260820-141928_current-progress-review.md`
- `docs/progress/20260820-145214_ocr-branch-acceptance.md`
- `app/adapters/document_parser/textin_client.py`
- `app/adapters/document_parser/textin_mapper.py`
- `app/documents/router.py`
- `app/workflows/final_compare.py`
- `scripts/e2e_ocr_acceptance.py`

## 交接摘要

FINAL_COMPARE 扫描 PDF OCR 回退已收口，workflow/rules 为 0.3.0。
46 页真实任务 52.391 秒完成，46/46 页成功，结构和坐标元数据完整。
Docker 全量测试 55 passed，迁移、前端构建、日志脱敏和临时清理均通过。
最新 API/Worker/PostgreSQL 保持运行，命名卷和真实任务在重启后保留。
PR #1 已建立，未合并；本轮不做甲方 Docker 网络验证。
自动浏览器不可用，视觉验收明确待人工完成。
下一步先校准 OCR 差异噪声，再做约 200 页单并发测试，随后推进 DRAFT_REVIEW。
