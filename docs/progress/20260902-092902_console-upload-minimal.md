# 任务进度：控制台文件上传最小改造

## 基本信息

- 时间：2026-09-02 09:29:02 +08:00
- 状态：COMPLETED
- 任务类型：BUILD
- 代码目录：D:\work\contract_review\contract-review-agent
- 当前分支：feat/draft-review-multidoc
- 当前提交：55d356d
- 工作树状态：dirty；保留其他会话的 V2、缓存和验收修改，本记录仅对应控制台上传改造

## 用户目标

为控制台的 DRAFT_REVIEW 和 FINAL_COMPARE 创建表单增加 DOCX/PDF 上传能力，将上传内容安全落到 API 持久化卷并转换为原有任务接口可接受的内部 URL，同时保持公开任务接口、工作流、结果 Schema 和历史任务不变。

## 本次完成

- 新增控制台 multipart 上传和按不可猜测 ID 读取文件的内部入口，上传使用 1MB 分块、SHA-256、签名校验和原子落盘。
- 增加 7 天上传清理、启动清理、大小/空文件/扩展名/路径字符校验及失败临时文件清理。
- 控制台起草检查、放款比对表单改为文件选择、顺序上传、进度/状态展示、移除/替换，并在必需文件未成功上传时禁用创建任务。
- 保留原有 URL 任务创建接口；不修改 OCR、LLM、比对工作流、结果 Schema、数据库表或迁移。

## 修改文件

- `app/services/console_uploads.py`：上传持久化、元数据、签名校验、读取和过期清理。
- `app/api/routes/console_uploads.py`：隐藏于公开 API 文档的上传/下载路由。
- `app/schemas/files.py`：上传响应模型。
- `app/core/config.py`：上传根目录、内部基址和留存天数配置。
- `app/main.py`：挂载路由并在 API 启动时清理过期上传。
- `compose.yaml`、`Dockerfile`、`.env.example`：命名卷、容器路径、API 内部下载主机和运行配置。
- `frontend/src/api/client.ts`、`frontend/src/api/types.ts`：XHR 上传进度和上传结果类型；前端上传队列串行化。
- `frontend/src/components/RemoteFileFields.vue`、`frontend/src/views/DraftReviewView.vue`、`frontend/src/views/FinalCompareView.vue`：控制台上传交互。
- `tests/unit/test_console_uploads.py`：上传、签名、大小、路径、路由读取和清理测试。
- `README.md`：上传部署、反向代理和磁盘要求说明。

## 接口、数据和配置变化

- API：新增内部 `POST /api/v1/console/uploads` 和 `GET /api/v1/console/uploads/{upload_id}`，两条路由均不进入 OpenAPI 文档；原任务接口不变。
- 数据库/迁移：无变化。上传内容和安全元数据保存在 `upload_data` 命名卷。
- 配置：新增 `UPLOAD_ROOT`、`CONSOLE_UPLOAD_BASE_URL`、`CONSOLE_UPLOAD_RETENTION_DAYS`；Compose 将内部 `api` 主机加入下载 allowlist，并保留外部显式主机。
- 兼容性：原有甲方 URL 提交方式保持可用；控制台改为上传方式。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `python -m pytest -q tests/unit/test_console_uploads.py` | 通过 | 10 passed |
| 变更范围 Ruff | 通过 | `app`、上传测试无 lint 错误 |
| 变更范围 `compileall` | 通过 | Python 文件编译成功 |
| `npm run test:format` | 通过 | 现有前端格式测试成功 |
| `npm run typecheck` | 通过 | Vue/TypeScript 类型检查成功 |
| `npm run build` | 通过 | Vite 生产构建成功，仅有既有 chunk 体积提示 |
| `docker compose config --quiet` | 通过 | Compose 配置解析成功，未启动服务 |
| `git diff --check` | 通过 | 无空白错误 |

## Docker 与运行状态

- API：未因本任务启动或重建。
- Worker：未因本任务启动或重建。
- PostgreSQL：未因本任务启动或重建。
- 控制台：已完成静态构建，未执行人工视觉验收。
- 最终是否保持运行：未改变现有服务运行状态。

## 重要决策

- 上传元数据使用命名卷中的 JSON sidecar，避免新增数据库表；文件内容仍由 Worker 通过现有 URL 下载器流式读取。
- 通过客户端上传队列确保多个文件按顺序发送，降低 API 和单机磁盘瞬时压力。
- Compose 只在容器运行环境中追加 `api` allowlist；`.env` 未被改写，正式外部域名仍由部署环境显式配置。

## 已知问题与风险

- 需要部署环境启用 HTTP 内部下载并将 `api` 作为允许主机；生产反向代理仍需配置 200MB 请求体和合理上传超时。
- 尚未执行 Compose 上传冒烟和人工控制台交互验收，待部署/交付阶段完成。
- 工作树仍包含其他会话的未提交修改，未执行提交、清理或服务状态变更。

## 下一步建议

1. 在目标 Compose 环境执行一次五文件上传和一组 DOCX/PDF 上传冒烟。
2. 核实 API/Worker 重启后 `upload_data` 中的文件仍可下载。
3. 交付前由运维设置正式反向代理大小、超时和磁盘告警参数。

## 下一会话首先阅读

- `AGENTS.md`
- `app/services/console_uploads.py`
- `app/api/routes/console_uploads.py`
- `frontend/src/components/RemoteFileFields.vue`

## 交接摘要

上传功能已在代码层完成并通过 10 个后端定向测试、前端格式/typecheck/build、Ruff、compileall、Compose 配置解析和 diff 检查。新增内部上传路由不改变原任务 API，上传文件使用命名卷和内部 URL。未启动 Docker 服务，未执行 Compose 上传冒烟或人工视觉验收；工作树保留其他会话修改且未提交。
