# 任务进度：FINAL_COMPARE 真实纵向切片

## 基本信息

- 时间：2026-08-19 16:18:49 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`main`
- 当前提交：`UNBORN`
- 工作树状态：dirty；仓库尚无首个提交，现有工程文件均未跟踪。本会话未自动提交，也未覆盖或删除其他会话文件。

## 用户目标

仅将 `FINAL_COMPARE` 落成真实纵向切片：从现有 URL API 受控下载 DOCX/文本型 PDF，解析为统一模型，执行确定性文字、数值和基础表格比对，持久化可追溯 JSON 并在控制台展示；`DRAFT_REVIEW` 保持 Mock，OCR、真实 LLM 和复杂合同规则不进入本次范围。

## 本次完成

- 实现受控流式下载：协议/主机/IP 校验、逐跳重定向复核、禁用环境代理、超时和双重大小限制、SHA-256、DOCX/PDF 魔数校验及稳定错误码。
- 实现任务独立临时工作目录，在成功、失败和重试路径统一清理；二进制和合同全文不进入 PostgreSQL。
- 实现统一文档模型与解析器：DOCX 按正文 XML 顺序保留段落/表格和结构位置；文本 PDF 保留页码，低文本量明确返回 `OCR_REQUIRED`。
- 实现 NFKC/空白归一化、段落锚点和顺序对齐、字符级差异片段、数值类原文识别、基础表格行/单元格比对，以及固定严重度规则。
- 新增真实 `FINAL_COMPARE` LangGraph 与工作流路由；`DRAFT_REVIEW` 继续使用现有 Mock Graph。
- Worker 将数据库中的原始 URL 仅在内部传递；成功结果、任务终态和文件 SHA-256/大小/MIME/页数/解析器/警告在同一事务更新。
- 真实结果保持顶层 schema `1.0`，返回 `mock=false`、`execution_mode=RULE_BASED`、工作流/规则版本 `0.2.0`、`primary_model=null` 和空 `model_runs`。
- 控制台增加真实模式、文件解析状态和 warning、差异类型/严重度、前后文本、双方位置、人工复核标记展示；DRAFT 页面仍明确为 Mock。
- Compose 新增 `fixtures` profile：用应用镜像提供仅容器网络可见的 fixture server，只读挂载本机脱敏目录且不映射宿主机端口。
- 增加下载、解析、比对、Graph 和 Worker 测试；增加真实 FINAL_COMPARE 冒烟脚本。
- 生成 `frontend/package-lock.json`，Docker 前端阶段改用 `npm ci`。
- 更新 README、环境示例和忽略规则；未修改 `.env`，未新增数据库迁移。

## 修改文件

- `app/services/downloader.py`、`app/services/temp_files.py`：安全下载和临时目录生命周期。
- `app/documents/`：统一模型、归一化、DOCX/PDF 解析器。
- `app/comparison/`：差异类型、定位模型和确定性比对引擎。
- `app/workflows/final_compare.py`、`app/workflows/router.py`、`app/workflows/types.py`：真实工作流及路由。
- `app/worker/runner.py`、`app/db/repositories/task_repository.py`：工作流调用、安全失败与文件元数据原子持久化。
- `app/schemas/results.py`、`app/schemas/common.py`、`app/core/config.py`、`app/core/errors.py`、`app/main.py`：结果契约、配置、错误与 OpenAPI 调整。
- `frontend/src/`、`frontend/package-lock.json`：控制台真实结果展示和依赖锁定。
- `compose.yaml`、`Dockerfile`、`.env.example`、`.dockerignore`、`.gitignore`：容器、fixture profile 和配置。
- `tests/unit/`、`tests/integration/test_worker.py`：下载、解析、比较、Graph/Worker 测试。
- `scripts/e2e_final_compare.py`、`scripts/e2e_smoke.py`：真实 FINAL 和 Mock DRAFT 冒烟。
- `README.md`：真实能力、fixture 启动和安全边界说明。

## 接口、数据和配置变化

- API：没有新增或改动路由；`GET /tasks/{id}/result` 的 `mock` 改为普通布尔值并显式定义 `diff_items`、文件和 metadata 类型。
- 数据库/迁移：没有 schema 变化；仍为 `0001_initial (head)`。补全既有 `task_file` 元数据字段的写入。
- 配置：新增 `PDF_MIN_TEXT_CHARS_PER_PAGE`；Compose 显式透传下载开关和 host allowlist；新增 `LOCAL_FIXTURE_DIR` 供 fixtures profile 只读挂载。
- 兼容性：既有 DRAFT Mock 结果继续可展示；FINAL 结果从 Mock 切换为规则执行，不发送 OCR/LLM 请求。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 变更前测试基线 | 通过 | `15 passed` |
| 下载器首轮红测 | 预期失败 | 新模块尚未实现时出现 3 个 collection error，随后按 TDD 补齐实现 |
| `docker compose --profile tools run --rm test` | 通过 | `29 passed, 1 warning`；warning 来自 LangGraph 上游待弃用默认值 |
| `ruff check app tests scripts --ignore E501` | 通过 | 无非行宽静态检查问题；既有/新增中文描述保留原可读行宽 |
| `npm run typecheck` | 通过 | Vue/TypeScript 无类型错误 |
| `npm run build` | 通过 | Vite 生产构建完成；存在约 986 kB 单 chunk 性能提示 |
| `npm audit --omit=dev` | 通过 | 生产依赖 `0 vulnerabilities`；开发依赖树仍有 1 个 high 提示，未强制升级 |
| `docker compose --profile fixtures config --quiet` | 通过 | 默认服务仍为 api/worker/postgres，fixture 仅 profile 启用 |
| `docker compose --profile fixtures build api fixture-server` | 通过 | Node 22 + Python 3.12 多阶段运行镜像构建成功 |
| `docker compose exec -T api alembic current` | 通过 | `0001_initial (head)` |
| `docker compose exec -T api alembic check` | 通过 | `No new upgrade operations detected` |
| `/health`、`/ready`、`/docs`、`/console/` | 通过 | 均返回 HTTP 200 |
| `python scripts/e2e_final_compare.py` | 通过 | 任务 `tsk_01M0CHH1FYRQR3A9PKSSM9V2GG` 成功，`RULE_BASED`、31 项 diff |
| 文件元数据核对 | 通过 | baseline/target 均为 python-docx、SUCCEEDED、64 位 SHA-256；大小分别为 63346/63071 字节 |
| API/Worker restart 后查询 | 通过 | 同一任务仍为 SUCCEEDED，结果仍为 RULE_BASED、31 项 diff |
| `python scripts/e2e_smoke.py` | 通过 | DRAFT 任务 `tsk_01M0CHHPSED3Q3WFP7YNYV4TDJ` 成功且仍为 Mock |
| PDF 解析测试 | 通过 | 生成的文本 PDF 保留页码；空文本 PDF 在 Worker 路径失败为 `OCR_REQUIRED` |
| 临时目录检查 | 通过 | Worker `/tmp/contract-review` 子目录数为 0 |
| 日志敏感模式检查 | 通过 | token/signature/API key 模式命中数为 0 |
| 浏览器控制台 QA | 通过 | 31 张差异卡，空位置显示 0，低置信度强配对 0；双方位置和 RULE_BASED 标签可见 |
| 外层资料 SHA-256 复核 | 通过 | 12 份方案 Markdown 和 12 份业务资料与任务开始快照一致；脱敏合同未写入 |
| PostgreSQL volume inspect | 通过 | `contract-review-postgres-data` 存在；未执行任何 volume 删除命令 |

补充：直接运行宿主机 `python scripts/run_tests.py` 因宿主 Python 未安装 `asyncpg` 无法启动，随后按 README 改用交付用 Docker 测试镜像并完整通过。前端首次尝试 `npm ci` 因仓库原先无 lockfile失败，已通过 `npm install` 生成 lockfile，之后本机与 Docker 均改用锁定依赖验证成功。

## Docker 与运行状态

- API：`contract-review-api-1`，healthy，映射 `127.0.0.1:8000`。
- Worker：`contract-review-worker-1`，running。
- PostgreSQL：`contract-review-postgres-1`，healthy；命名卷保留。
- Fixture server：`contract-review-fixture-server-1`，running，仅容器网络端口 8080，无宿主机端口。
- 控制台：`http://localhost:8000/console/` 返回 200。
- 最终是否保持运行：是；api、worker、postgres 和 fixtures profile 均保持运行。

## 重要决策

- 仅精确 allowlist 的测试主机可绕过私网 IP 禁止，以便 fixture server 工作；默认配置继续禁止 HTTP、私网和空 allowlist。
- DOCX 不伪造页码；位置使用段落/表格/行/列。文本 PDF 才提供物理页码。
- `ignore_formatting=false` 只返回能力限制 warning，当前仍仅比较内容。
- 低相似度条款即使编号相同也不强行配对，拆为新增/删除以减少误导。
- 生产依赖锁定并审计通过；不使用 `npm audit fix --force` 引入未经验证的主版本升级。

## 已知问题与风险

- SSRF 防护为当前阶段受控实现，不等同完整生产网关隔离；DNS 解析与实际连接之间仍存在理论上的 rebinding/TOCTOU 风险。
- PDF 仅承诺文本层解析，不恢复复杂表格；扫描件稳定返回 `OCR_REQUIRED`。
- DOCX 合并单元格、复杂浮动对象和修订标记仅给 warning，可能需要人工复核。
- 规则严重度是固定关键词启发式，不是法律审查结论，也不做合同数学计算。
- 前端生产包存在约 986 kB 的单 chunk 性能提示；开发依赖审计有 1 个 high 提示，生产依赖审计为 0。
- LangGraph 依赖发出 1 条 pending deprecation warning，当前不影响执行。

## 下一步建议

1. 先以更多已脱敏 FINAL_COMPARE 样本建立黄金 diff/误报漏报集，校准段落与表格对齐阈值。
2. 增加真实网关级下载隔离、域名解析钉扎/连接 IP 复核、审计记录和更细的 MIME 策略。
3. 在甲方 OCR 服务可用后实现 OCR Adapter，并为扫描 PDF 增加可恢复工作流。
4. 拆分控制台路由和 Element Plus 依赖，降低首屏 chunk。
5. 再单独推进 DRAFT_REVIEW 的真实解析、抽取和规则切片，不与当前确定性对比混合。

## 下一会话首先阅读

- `AGENTS.md`
- `README.md`
- `docs/plans/20260819_final-compare-vertical-slice.md`
- `docs/progress/20260819-161849_final-compare-slice.md`
- `app/workflows/final_compare.py`
- `app/services/downloader.py`
- `app/comparison/engine.py`

## 交接摘要

FINAL_COMPARE 真实纵向切片已完成并在脱敏 DOCX 上实际跑通，输出 31 项可追溯差异。
结果为 `mock=false / RULE_BASED`，DRAFT_REVIEW 保持 Mock。
后端全量测试 29 项通过，前端类型检查与生产构建通过。
Alembic 仍为 `0001_initial (head)` 且无新迁移差异。
API/Worker 重启后任务和结果保持，临时文件无残留，日志敏感模式 0 命中。
api、worker、postgres、fixture-server 当前均运行，API/PostgreSQL healthy。
数据库命名卷保留，未自动 Git commit，外层方案/需求/脱敏合同未修改。
