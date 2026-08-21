# 合同智能检查 Agent

这是方案 v0.2.2 的合同检查工程：FastAPI、PostgreSQL 持久化任务队列、独立 Worker、LangGraph 工作流和 Vue 3 测试控制台。`FINAL_COMPARE 0.4.2` 已支持受控 URL 下载、配对一致的文档解析、可追溯 N:M 条款对齐、可靠性保护和 OCR 人工复核；`DRAFT_REVIEW 0.3.1` 已支持真实多文档解析、模板固定内容比对、允许填写项过滤、未填占位符和基础表格必填检查。结果使用 Schema 2.0 的“风险 / 人工复核 / 通过”语义，不设置风险等级；事实矩阵和 LLM 尚未启用。

> 所有结果都不构成合同审查、法律审核或放款意见。真实版本比对标记为 `RULE_BASED`；OCR 只负责文档结构解析，本版本不调用 LLM，也不检查印章。

## 入口

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- 就绪检查：http://localhost:8000/ready
- 测试控制台：http://localhost:8000/console/
- 起草检查报告页：`http://localhost:8000/console/#/reports/draft/{task_id}`
- 放款比对报告页：`http://localhost:8000/console/#/reports/final/{task_id}`

两个业务报告页已纳入控制台统一导航。创建任务后默认进入对应报告并轮询进度；任务中心以“查看报告”为主入口，以“调试详情”为次入口。业务报告不显示原始 JSON 或文件 URL，调试详情仍保留解析诊断、重试和原始结果。URL 只携带不可猜测的任务 ID，不携带文件地址。
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

本机 Python 开发建议使用独立 Miniconda 环境，避免修改其他项目共用的环境：

```powershell
conda create -n contract-review-agent-py312 python=3.12 -y
conda run -n contract-review-agent-py312 python -m pip install -e ".[test]"
conda run -n contract-review-agent-py312 python -m pytest -q
```

运行 DRAFT_REVIEW 合成 DOCX 的 API → Worker → 真实解析结果闭环。冒烟脚本会在 API 容器临时启动只服务合成文件的 HTTP 服务，因此 API/Worker 必须显式允许主机 `api`：

```powershell
$env:ALLOW_HTTP_DOWNLOADS = 'true'
$env:DOWNLOAD_HOST_ALLOWLIST = 'api'
docker compose up -d api worker
docker compose exec -T api python scripts/e2e_smoke.py
```

脚本结束后会删除合成文件，不调用 OCR 或 LLM。

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

- `LLM_ENABLED=false`，模型配置默认 `GLM-5.2`；当前工作流没有真实 LLM 调用路径。
- `MOCK_STAGE_DELAY_SECONDS` 控制控制台可见的模拟阶段延时，测试中设为 `0`。
- `WORKER_STALE_AFTER_SECONDS` 和 `TASK_MAX_ATTEMPTS` 控制心跳恢复。
- `ALLOW_HTTP_DOWNLOADS` 默认关闭；开发 fixture 需要显式开启，并将 `DOWNLOAD_HOST_ALLOWLIST` 精确设为 `fixture-server`。
- `MAX_FILE_SIZE_MB`、`DOWNLOAD_TIMEOUT_SECONDS`、`DOWNLOAD_MAX_REDIRECTS` 控制下载边界。
- `MAX_REFERENCE_FILES` 控制 DRAFT_REVIEW 辅助资料数量，代码默认 20，允许在 1–100 范围内配置；超限会明确返回 `INVALID_REQUEST`，不会截断。
- DRAFT_REVIEW 的 `reference_type` 已弃用并被忽略；新请求只传 URL、文件名和可选 MIME/展示名，系统分类将在后续 LLM 阶段作为输出提供。
- DRAFT_REVIEW 逐份真实解析：DOCX 使用 `python-docx`，PDF 使用外部解析器 `auto`；目标合同与模板正文使用可靠对齐，允许填写项保留过滤轨迹，扩展表格转为人工复核提示。结果标记为 `RULE_BASED / mock=false`，不会生成模拟或 LLM 结论。
- `FINAL_COMPARE` 按文件对制定解析计划：DOCX/DOCX 均使用本地 `python-docx`；PDF/PDF 均使用外部解析器 `auto`；混合 DOCX/PDF 中 PDF 使用外部解析器 `scan`。`pdfplumber` 仅保留为诊断工具，不作为正式比对的静默降级路径。
- 新任务结果固定使用 `schema_version=2.0`：确认的不合规、缺失、冲突或未经允许变化进入 `risk_items`；OCR、解析和对齐不确定性进入 `review_items`；实际执行且通过的检查进入 `passed_checks`。旧 1.0 结果读取时转换为无等级统计并标记为 legacy。
- `OCR_ENABLED` 默认关闭；启用时还必须配置 `OCR_BASE_URL`、`OCR_API_KEY` 和 `OCR_AUTH_HEADER`。示例文件不会包含真实地址或密钥。
- `OCR_MAX_RESPONSE_MB` 限制供应商响应大小；`OCR_HTTP_RETRY_ATTEMPTS` 和 `OCR_RETRY_BACKOFF_SECONDS` 只作用于连接错误、超时及 502/503/504；`OCR_LOW_CONFIDENCE_THRESHOLD` 默认 `0.8`。

### 本机真实 OCR 验证

当开发机 Docker 网络无法访问甲方 OCR，但宿主机可以访问时，可让 PostgreSQL 继续运行在 Docker 中，让 API 与 Worker 使用上述 Miniconda 环境在宿主机运行。仅对这些临时进程设置 `OCR_ENABLED=true`，并将 `DATABASE_URL` 指向映射到 `127.0.0.1` 的 PostgreSQL 端口。可用以下脚本验证，但输入只能使用脱敏或完全合成的扫描 PDF：

```powershell
conda run -n contract-review-agent-py312 python scripts/ocr_live_probe.py --mode auto <synthetic-scan.pdf>
conda run -n contract-review-agent-py312 python scripts/e2e_ocr_local.py
# 46 页验收（文件名、API/fixture 地址和期望页数均可用环境变量覆盖）
conda run -n contract-review-agent-py312 python scripts/e2e_ocr_acceptance.py
```

`ocr_live_probe.py` 的 `--mode` 支持 `auto` 或 `scan`，默认 `auto`。真实 OCR 地址、鉴权头和值只从被 Git 忽略的 `.env` 或进程环境读取。探测脚本成功时只打印页数、结构块/表格/单元格数、解析模式、耗时、响应大小和置信度摘要；失败时只打印稳定错误码及安全诊断，不打印异常链、全文、服务地址或密钥。安全诊断仅包含组件、失败类型、尝试次数和耗时，并通过任务现有 `error_details` 字段持久化。宿主机成功不能替代最终甲方内网中 Worker 容器的单页扫描 PDF 验收。

2026-08-20 已完成一组 0.3.0 的 46 页宿主机真实闭环：基准文件由 `pdfplumber` 解析，扫描目标文件回退 OCR，46 页全部成功；任务总耗时约 52.4 秒，OCR 服务耗时约 43.0 秒，响应约 5.1 MiB。0.4.0 已将 PDF/PDF 改为双方统一 external `auto`，并在结果 `metadata.comparison_diagnostics` 返回双侧覆盖率、全局相似度、候选/最终差异数、fallback 和可靠性原因。若可靠性门槛未通过，候选差异不会升级为 HIGH/MEDIUM 业务风险，结论固定为 `REVIEW_REQUIRED`。

0.4.0 首次 46 页双方 external `auto` 诊断任务已确认 46/46 页、双侧覆盖率 100%、全局相似度 99.05%，差异由旧流程的 2,099 项降至 16 项。0.4.1 进一步定点合并 OCR 表格中“空主键、仅名称/描述类文本列非空”的相邻续行，并以 `OCR_SINGLE_CHAR_VARIANCE`、`OCR_PLACEHOLDER_VARIANCE`、`OCR_READING_ORDER_VARIANCE` 等原因码保留 LOW 人工复核；金额、日期、比例、主体、条款和表格真实变化由正样本保护，Docker 全量测试为 91 项通过。

0.4.1 首次获准的 46 页复验曾在 `PARSING / 35%` 以 `OCR_SERVICE_UNAVAILABLE` 安全失败，未自动重跑。增加安全诊断和单页预检后，2026-08-20 在宿主机链路完成了严格限额复验：单页预检上传 1 次、唯一 46 页任务双方各上传 1 次，HTTP 自动重试关闭。任务 `tsk_01M0F7EP40AEJNRG7CJNET0BS5` 在 84.782 秒内成功，双方 external `auto` 均为 46/46 页，双侧覆盖率 100%，最终只有 3 项带原因码的 LOW 人工复核项，HIGH、MEDIUM 和 `NUMERIC_CHANGED` 均为 0，结论为 `REVIEW_REQUIRED`。后端 Docker 全量测试为 103 项通过。自动浏览器在本次环境中不可用，控制台视觉检查仍需按进度记录中的人工清单完成，因此 PR 暂时保持 Draft。

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

已实现 FINAL_COMPARE 的受控下载、任务级一致解析计划、同步外部 PDF 解析，以及文字/数值/基础表格差异。DRAFT_REVIEW 已复用可靠正文对齐并增加模板填写区过滤、未填标记和保守的表格检查。对齐引擎会处理中文空格、软换行、零宽字符和已确认的解析器标记噪声，支持 1–4 对 1–4 条款合并/拆分、严格的 OCR 表格续行合并、表格兼容门控及页面文本 fallback；不可靠对齐不会直接生成业务风险。下载器执行协议、allowlist、DNS/IP、重定向、超时、大小和内容签名校验，但正式部署仍应使用甲方文件域名 allowlist，并评估 DNS rebinding、出口代理和网络策略。外部解析响应会校验业务码、有效页数、页面状态、段落和表格单元格完整性；不完整结果不会生成 `PASS`。

尚未实现旧版 DOC、异步 OCR、真实 LLM、Embedding/Rerank、DRAFT_REVIEW 事实抽取/跨资料矩阵、复杂模板表格语义、合同数学规则、印章、上传、报告文件导出、鉴权和模板库。正式 PDF 解析未配置外部解析器时会明确以 `OCR_NOT_CONFIGURED` 安全失败，不会用本地文本抽取假装完成。
