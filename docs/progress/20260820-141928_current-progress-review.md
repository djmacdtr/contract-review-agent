# 任务进度：当前进度复核与下一阶段建议

## 基本信息

- 时间：2026-08-20 14:19:28 +08:00
- 状态：COMPLETED
- 任务类型：REVIEW / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-ocr`
- 当前提交：`c7b7490`
- 工作树状态：dirty；包含 OCR 文档解析纵向切片、FINAL_COMPARE 真实规则比对、控制台中文标签、测试、计划和进度记录等未提交修改

## 用户目标

检查项目当前真实工作进度，以代码、测试和运行状态为准给出下一步方向，并形成可供其他会话读取的交接记录。

## 本次完成

- 读取 README、最近三份进度记录、OCR 集成计划和当前 Git 状态。
- 核对 OCR Client、响应映射、解析路由、FINAL_COMPARE 工作流和控制台中文标签实现。
- 使用当前工作区源码重新构建 Docker 测试镜像并执行完整测试。
- 核对 Alembic 迁移一致性、Docker 服务和本地健康端点。
- 扫描源码与脚本，未发现已知 OCR API Key 或内网地址进入业务代码。
- 形成当前能力边界、交付风险和下一阶段优先级建议。

## 修改文件

- `docs/progress/20260820-141928_current-progress-review.md`：新增本次独立复核记录。
- 本次未修改业务代码、配置、数据库迁移或前端源码。

## 接口、数据和配置变化

- API：本次无变化。
- 数据库/迁移：本次无变化；`alembic check` 未检测到缺失迁移。
- 配置：本次无变化；当前运行环境显示 OCR 和 LLM 均未启用。
- 兼容性：本次无变化。

## 当前能力结论

| 范围 | 当前状态 | 说明 |
|---|---|---|
| FastAPI、PostgreSQL、持久化队列、Worker、Docker | 已完成并运行 | API、PostgreSQL healthy，Worker 正常运行 |
| FINAL_COMPARE：DOCX/文本型 PDF | 已实现 | 受控下载、本地解析、文字/数值/基础表格确定性差异 |
| FINAL_COMPARE：扫描 PDF OCR | 代码与主机侧纵向闭环已完成 | 本地解析出现 `OCR_REQUIRED` 时回退外部 OCR；保留页码、坐标、置信度和表格位置 |
| DRAFT_REVIEW | 仍为 Mock | 未实现模板、目标合同与辅助材料的真实交叉检查 |
| LLM 网关 | 未接入 | 就绪检查显示 `llm_configured=false` |
| 测试控制台 | 已有；最新中文标签未部署到常驻容器 | 最新源码可构建，但运行中的镜像早于最后一次前端修改 |
| Git 交付状态 | 未完成 | OCR 相关改动未提交、未推送，HEAD 仍为 `c7b7490` |

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `git diff --check` | 通过 | 无空白错误；存在 Windows LF/CRLF 提示 |
| `docker compose --profile tools run --rm --build test` | 通过 | `52 passed, 1 warning`，约 4.04 秒 |
| Docker 前端生产构建 | 通过 | 作为多阶段测试镜像构建的一部分完成 |
| `docker compose --profile tools run --rm test alembic check` | 通过 | `No new upgrade operations detected` |
| `docker compose ps` | 通过 | API、PostgreSQL healthy；Worker running |
| `/health`、`/ready`、`/console/` | 通过 | health=ok，ready=ready，console HTTP 200 |
| 敏感值定向扫描 | 通过 | 未在业务代码、测试、脚本和配置示例中发现已知 Key 或内网地址 |

唯一测试警告为 LangGraph 上游 `allowed_objects` 默认值未来变化提醒，本次未处理。

## Docker 与运行状态

- API：运行中且 healthy，地址为 `http://127.0.0.1:8000`。
- Worker：运行中。
- PostgreSQL：运行中且 healthy。
- 控制台：`http://127.0.0.1:8000/console/` 返回 HTTP 200。
- 最终是否保持运行：是。
- 注意：常驻 API/Worker 镜像约三小时前创建，未包含工作区最后的中文标签修改；本次只重建了 test 镜像，没有替换常驻服务。

## 重要决策

- 下一步不应立即扩展新的大功能，先将 OCR 分支变成可审查、可回滚、可推送的稳定增量。
- 真实 LLM 接入应排在 OCR 规模和部署网络验收之后，避免解析链路与模型链路同时引入不可定位变量。
- DRAFT_REVIEW 建议沿用现有下载、解析和证据定位底座，先做确定性模板/事实核对，再叠加 LLM 结构化抽取和建议。

## 已知问题与风险

- 当前工作区有较大范围未提交修改，且远端尚无 OCR 纵向切片提交；多窗口继续开发容易产生覆盖、丢失和审查困难。
- 本机 Docker 容器到甲方 OCR 服务曾返回 502；目前真实闭环只在宿主机进程完成。最终部署网络必须再次验证。
- OCR 实测样本仅为单页合成扫描件，尚未覆盖真实 20 页、200 页合同、旋转页、低置信度、复杂表格和部分失败。
- 当前为同步 OCR 请求。200 页文件是否会超时、超响应上限或长时间占用 Worker 尚无数据。
- 最新控制台中文标签尚未在浏览器完成视觉验收，也未部署到当前常驻 API 镜像。
- `FINAL_COMPARE_WORKFLOW_VERSION` 和规则版本仍为 `0.2.0`；提交前应确认本次 OCR 能力是否需要版本提升。
- DRAFT_REVIEW、真实 LLM、复杂合同规则和法律建议仍未实现，当前结果不能代表完整业务需求已交付。

## 下一步建议

1. **收口 OCR 分支**：做一次针对当前 diff 的代码审查，确认版本号和文档，然后提交并推送 `feat/final-compare-ocr`，通过 PR 保留审查边界。
2. **完成 OCR 验收基线**：补真实多页样本与边界用例，至少覆盖 20 页、接近 200 页、旋转、低置信度、复杂表格、超时、响应过大和部分页面失败；记录耗时、响应大小和 Worker 占用。
3. **验证部署网络**：在甲方最终 Docker 网络环境执行 API→Worker→OCR 闭环；不要把本机 Docker 的 502 直接推断为生产不可用。
4. **部署并视觉验收控制台**：重建常驻 API/Worker 镜像，浏览器检查中文标签、OCR 页码/坐标/置信度和差异列表。
5. **再启动 DRAFT_REVIEW 真实纵向切片**：复用下载和解析路由，先实现目标合同与模板的确定性差异，再加入辅助文件事实矩阵。
6. **最后接入 LLM 网关**：围绕结构化事实抽取、风险解释和建议定义严格 JSON Schema、重试/降级、证据引用和模型调用审计；不要让 LLM 替代数值和文字确定性比对。

## 下一会话首先阅读

- `README.md`
- `docs/plans/20260820_ocr-document-parser-integration.md`
- `docs/progress/20260820-103505_ocr-parser-slice.md`
- `docs/progress/20260820-110654_task-detail-chinese-labels.md`
- `docs/progress/20260820-141928_current-progress-review.md`
- `app/adapters/document_parser/textin_client.py`
- `app/adapters/document_parser/textin_mapper.py`
- `app/documents/router.py`
- `app/workflows/final_compare.py`

## 交接摘要

基础平台和 FINAL_COMPARE 的真实确定性比对已完成。
扫描 PDF OCR Adapter 已实现，并在宿主机完成单页真实纵向闭环。
当前源码独立复测为 52 passed，迁移一致，敏感值扫描无命中。
API、Worker、PostgreSQL 和控制台保持运行，但常驻镜像不是最新工作区版本。
DRAFT_REVIEW 仍为 Mock，LLM 未接入。
当前最大风险是 OCR 大批改动未提交、未推送，以及多页性能和 Docker 部署网络尚未验收。
下一步应先审查、提交 OCR 分支，再做多页/网络/控制台验收，随后开发 DRAFT_REVIEW。
