# 任务进度：标准 Worker 验收配置收口

## 基本信息

- 时间：2026-09-02 10:06:39 +08:00
- 状态：COMPLETED
- 任务类型：FIX
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`55d356d`
- 工作树状态：dirty；保留控制台上传、OCR、页码、起草检查和其他会话的既有未提交修改

## 用户目标

将标准宿主机 Worker 的 OCR/LLM 运行参数与此前成功验收链路对齐，确认配置正确后交由用户再次从控制台创建全新任务。

## 本次完成

- 标准 `DraftReviewWorkflowExecutor` 构造 LLM 客户端时，Text 与 Advice 分别使用可配置的 `json_object` 覆盖；Numeric、Profile 与 Mapping 继续使用全局 `json_schema`。
- 本机 `.env` 已设置 OCR 与 DOCX 页码开启、OCR 重试 0、LLM HTTP 重试 1、LLM 请求并发 2、抽取任务并发 2。
- `.env.example` 已同步验收参数及 Text/Advice 响应格式配置，避免部署继续依赖专用验收脚本注入。
- 未创建、retry 或修改任何业务任务；重启前数据库无 `PENDING/RUNNING` 任务。
- 宿主机 Worker 已重启并加载当前代码与 `.env`，PID 为 `40684`。

## 修改文件

- `app/core/config.py`：增加 Text/Advice 专用响应格式配置。
- `app/workflows/draft_review.py`：标准工作流应用两项响应格式覆盖。
- `.env.example`：同步已验证的部署参数。
- `.env`：同步本机宿主机验收参数；该文件不提交且本文不记录凭据。
- `tests/unit/test_draft_review_workflow.py`：覆盖标准执行器的响应格式路由。

## 接口、数据和配置变化

- API：无 Schema 或路由变化。
- 数据库/迁移：无。
- 配置：新增 `LLM_TEXT_RESPONSE_FORMAT`、`LLM_ADVICE_RESPONSE_FORMAT`；本机取值均为 `json_object`。
- 兼容性：注入自定义 LLM 客户端的测试和运维脚本不受影响；未改变 Numeric/Mapping 的 `json_schema`。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 相关单元测试 | 通过 | `141 passed, 1 warning` |
| Ruff（变更 Python 文件） | 通过 | `All checks passed` |
| compileall（变更 Python 文件） | 通过 | 无编译错误 |
| 本机 Settings 安全核对 | 通过 | OCR/page=true；重试 0/1；并发 2/2；全局 json_schema；Text/Advice json_object |
| `docker compose config --quiet` | 通过 | Compose 配置可解析；仅出现宿主机 Docker config 权限 warning |
| `git diff --check` | 通过 | 无空白错误 |
| 真实 OCR/LLM/业务任务 | 未执行 | 按要求留给用户下一次唯一重试 |

## Docker 与运行状态

- API：HTTP `/health` 200、`/ready` 200。
- Worker：Docker Worker 仍停止；宿主机 Worker PID `40684` 正常运行。
- PostgreSQL：重启前确认无活动任务；未修改数据。
- 控制台：由用户人工上传验收。
- 最终是否保持运行：API、PostgreSQL、宿主机 Worker 保持运行。

## 重要决策

- 只将验收参数写入实际环境与部署模板，不改变通用 `Settings` 的并发/重试默认值，避免未配置环境和既有测试发生非必要行为变化。
- Text/Advice 响应格式必须进入标准工作流构造路径，不再只存在于专用验收脚本。

## 已知问题与风险

- 尚未执行新的真实控制台任务，因此本次只确认配置与离线路由正确，不宣称完整业务闭环已再次通过。
- 宿主机 Docker 客户端后续状态查询出现本地 Docker config/pipe 权限提示，但 API 健康且宿主机 Worker 正常；本次未因此变更 Docker 服务。

## 下一步建议

1. 用户从控制台创建一次全新三文件起草检查任务。
2. 仅观察该任务；若失败，直接依据首个安全错误诊断，不原样连续重试。

## 下一会话首先阅读

- `docs/progress/20260902-095305_ocr-content-length-fix.md`
- `docs/progress/20260902-100639_standard-worker-config-parity.md`
- `app/workflows/draft_review.py`
- `app/core/config.py`

## 交接摘要

标准 Worker 已与成功验收配置对齐：OCR/page 开启，OCR 重试 0，LLM 重试 1，并发 2/2，Text/Advice 使用 json_object，其余使用 json_schema。相关测试 141 项通过，静态检查通过。没有创建或重试业务任务。宿主机 Worker PID 40684 已运行，API health/ready 均为 200，可由用户进行下一次控制台验收。

## 后续修正：宿主机控制台上传地址

- 用户创建的新五文件任务 `tsk_01M1FXZN98KG5657E46C0CFR42` 在下载安全门失败，首错为 `DOWNLOAD_FORBIDDEN_TARGET`；五个任务文件的 URL 主机均为 `127.0.0.1`。
- 根因是 Compose Worker 使用内部 `api` 主机，而当前宿主机 Worker 接收的是 `http://127.0.0.1:8000` 控制台上传地址；此前仅为 Compose 写入了内部主机许可。
- 未把 `127.0.0.1` 加入通用下载 allowlist。下载器只信任与 `CONSOLE_UPLOAD_BASE_URL` 完全匹配的协议、主机、端口及 `/api/v1/console/uploads/upl_*` 路径；同主机其他路径和其他端口仍拒绝，避免放宽公开 URL 接口的 SSRF 边界。
- 本机 `.env` 已设置 `CONSOLE_UPLOAD_BASE_URL=http://127.0.0.1:8000`，不记录或改变原外部域名 allowlist。
- 下载器定向测试：`5 passed`；Ruff、compileall、`git diff --check` 通过。
- 首次组合收集 `test_console_uploads.py` 时，当前 `.venv` 缺少 `python-multipart` 而在 FastAPI 路由导入阶段停止；本轮修改不涉及上传保存/路由，随后只运行直接覆盖下载安全边界的测试。
- 使用失败任务中已保存的首个 URL 做零业务调用校验，结果为 `validation=ok`，主机和端口为 `127.0.0.1:8000`；未下载正文、未调用 OCR/LLM、未 retry 失败任务。
- 重启前活动任务数为 0；宿主机 Worker 已加载修复并运行，PID `55764`。Docker Worker 保持停止。

## 后续真实任务诊断

- 用户随后创建的新任务 `tsk_01M1FYDMQPEVXDGCVBJFW54656` 未再触发下载安全错误，已完成五份文件下载与本地/OCR 解析。
- 任务首错为 `FACT_EXTRACTION / profile / LLM_NETWORK_ERROR`，目标文件结构单元约 275 个；未进入事实分片、映射或 Advice。
- 独立模型列表探针返回 HTTP 200、耗时约 317ms，5 个模型可见，说明网关连通性和模型存在性正常。
- 以该目标文件做精确 Profile 负载的单次诊断未在约 3 分钟内返回；由于此前任务已失败且本轮不再追加外部调用，诊断连接已停止。不能据此宣称模型服务故障，但可以确认真实合同概览请求与合成小请求的处理行为明显不同。
- 当前最可能的剩余差异是 Profile 请求负载/网关处理路径，而非控制台上传、下载 allowlist、OCR 或页码。下一步应零外部调用地限制 Profile 概览单元和字符预算，并显式关闭 Profile 思考模式，再执行一次全新任务。
