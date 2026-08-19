# 合同智能检查 Agent

这是方案 v0.2.2 的合同检查工程：FastAPI、PostgreSQL 持久化任务队列、独立 Worker、LangGraph 工作流和 Vue 3 测试控制台。`FINAL_COMPARE` 已支持受控 URL 下载、DOCX/文本型 PDF 解析及确定性文字、数值和基础表格比对；`DRAFT_REVIEW` 仍为 Mock。

> 所有结果都不构成合同审查、法律审核或放款意见。真实版本比对标记为 `RULE_BASED`；本版本不调用 OCR 或 LLM，也不检查印章。

## 入口

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- 就绪检查：http://localhost:8000/ready
- 测试控制台：http://localhost:8000/console/
- Worker：`python -m app.worker`
- FastAPI：`app.main:app`
- 初始迁移：`alembic/versions/0001_initial.py`

## Docker 首次启动

需要 Docker Desktop/Engine 和 Docker Compose，不要求宿主机安装 Python 或 Node.js。

```powershell
Copy-Item .env.example .env
docker compose config
docker compose build
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose up -d api worker
docker compose ps
```

API 与 Worker 使用同一 `contract-review-agent:dev` 镜像，只覆盖启动命令。前端由 Docker 多阶段构建并复制进该镜像，由 FastAPI 在 `/console/` 提供。

数据库迁移必须作为独立命令执行；API 和 Worker 不会自动竞争运行迁移。

## 测试

运行后端单元和 PostgreSQL 集成测试：

```powershell
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose --profile tools run --rm test
```

测试容器会自动创建并迁移独立的 `contract_review_test` 数据库，避免正在运行的 Worker 领取测试任务；不会删除或重建开发数据库。

运行 DRAFT_REVIEW Mock 的 API → Worker → 结果闭环：

```powershell
docker compose up -d api worker
docker compose exec -T api python scripts/e2e_smoke.py
```

## 使用本地脱敏合同运行真实版本比对

合同目录只读挂载给 Compose `fixtures` profile，不进入 Git、应用镜像或 PostgreSQL。PowerShell 示例：

```powershell
$env:LOCAL_FIXTURE_DIR = 'D:/work/contract_review/脱敏真实合同'
$env:ALLOW_HTTP_DOWNLOADS = 'true'
$env:DOWNLOAD_HOST_ALLOWLIST = 'fixture-server'

docker compose --profile fixtures up -d --build api worker fixture-server
docker compose exec -T api python scripts/e2e_final_compare.py
```

脚本使用以下内部 URL，浏览器无需直接访问 fixture-server：

```text
http://fixture-server:8080/保证合同1.docx
http://fixture-server:8080/保证合同3.docx
```

也可以在控制台的“放款阶段比对”页面手工输入上述 URL。fixture-server 不映射宿主机端口，挂载目录为只读。验证结束后如不需要文件服务，可以只停止该 profile 服务：

```powershell
docker compose --profile fixtures stop fixture-server
```

前端类型检查和生产构建在 Dockerfile 的 `frontend-builder` 阶段执行。完整无缓存验证可运行：

```powershell
.\scripts\verify.ps1
```

## Alembic

```powershell
docker compose run --rm api alembic current
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic history
```

从空数据库验证时，应创建临时数据库、将 `DATABASE_URL` 指向它执行 `upgrade head`，检查四张表后删除临时数据库。不要用应用启动时的 `create_all` 代替迁移。

## 任务语义

- 创建接口仅持久化 URL 描述并返回 HTTP 202。
- Worker 使用 `FOR UPDATE SKIP LOCKED` 领取任务，领取事务立即提交。
- 状态为 `PENDING → RUNNING → SUCCEEDED/FAILED`；进度和心跳使用独立短事务。
- Worker 心跳超时且还有尝试次数时重新排队；达到最大次数时以 `WORKER_LOST` 失败。
- 手工重试只接受 `FAILED` 任务，并创建带 `source_task_id` 的新任务，保留原失败记录。
- 数据库保存原 URL 供未来 Worker 下载；API、日志与输入快照只使用删除 query/fragment 的安全 URL。

## 数据卷与停止

PostgreSQL 使用固定命名卷 `contract-review-postgres-data`。普通停止不会删除数据：

```powershell
docker compose down
docker volume inspect contract-review-postgres-data
```

本项目的常规命令不包含 `docker compose down -v`。删除数据库卷属于显式破坏性操作，不应在普通开发或验收流程执行。

## 环境配置

复制 `.env.example` 为 `.env`。示例只包含开发默认值，LLM/OCR Key 为空，`.env` 已被 Git 忽略。

- `LLM_ENABLED=false`，模型配置默认 `GLM-5.2`；当前真实版本比对没有 LLM 调用路径。
- `MOCK_STAGE_DELAY_SECONDS` 控制控制台可见的模拟阶段延时，测试中设为 `0`。
- `WORKER_STALE_AFTER_SECONDS` 和 `TASK_MAX_ATTEMPTS` 控制心跳恢复。
- `ALLOW_HTTP_DOWNLOADS` 默认关闭；开发 fixture 需要显式开启，并将 `DOWNLOAD_HOST_ALLOWLIST` 精确设为 `fixture-server`。
- `MAX_FILE_SIZE_MB`、`DOWNLOAD_TIMEOUT_SECONDS`、`DOWNLOAD_MAX_REDIRECTS` 控制下载边界。
- `PDF_MIN_TEXT_CHARS_PER_PAGE` 控制文本 PDF 最低文本密度；低于阈值返回 `OCR_REQUIRED`。

## 常见问题

### `/ready` 返回 503

确认 PostgreSQL healthcheck 已通过，并已执行 Alembic 迁移：

```powershell
docker compose ps
docker compose logs postgres
docker compose run --rm api alembic upgrade head
```

### 任务一直 PENDING

检查 Worker 是否运行及其安全日志：

```powershell
docker compose ps worker
docker compose logs worker
```

日志只包含任务 ID、阶段和错误类别，不应包含完整签名 URL、合同全文或密钥。

### 控制台刷新后 404

控制台使用 Hash Router；请访问 `/console/`，页面路径位于 `/#/tasks`，不会要求服务端处理 SPA 子路由。

## 当前能力边界

已实现 FINAL_COMPARE 的受控下载、DOCX/文本型 PDF 解析、文字/数值/基础 DOCX 表格差异。下载器执行协议、allowlist、DNS/IP、重定向、超时、大小和内容签名校验，但正式部署仍应使用甲方文件域名 allowlist，并评估 DNS rebinding、出口代理和网络策略。

尚未实现扫描 PDF OCR、旧版 DOC、真实 LLM、Embedding/Rerank、DRAFT_REVIEW 真实模板/跨资料检查、复杂表格、合同数学规则、印章、上传、报告、鉴权和模板库。扫描或低文本密度 PDF 会明确以 `OCR_REQUIRED` 失败，不会假装完成。
