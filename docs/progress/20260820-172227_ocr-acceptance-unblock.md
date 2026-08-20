# 任务进度：FINAL_COMPARE 0.4.1 真实验收解阻与 PR 收口

## 基本信息

- 时间：2026-08-20 17:22:27 +08:00
- 状态：PARTIAL
- 任务类型：FIX / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-alignment`
- 当前提交：`05e3782`（安全诊断代码与测试；本文档提交将在其后）
- 工作树状态：dirty；本会话待提交 `README.md` 和本文档。`AGENTS.md`、`docs/progress/README.md`、`docs/progress/20260820-171531_fast-development-workflow.md` 为并行会话修改，本会话未覆盖、未暂存。

## 用户目标

在不改变 0.4.1 比对算法、不增加迁移且真实上传最多三次的约束下，为外部解析失败增加安全可诊断信息，修复 OCR 探测脚本，使用宿主机 Python 与 Docker PostgreSQL 完成单页预检和唯一一次 46 页 PDF/PDF 真实验收，并据此收口 Draft PR #2；控制台视觉确认前不得将 PR 标为 Ready。

## 本次完成

- `WorkflowError` 支持可选安全详情，Worker 将白名单字段写入现有 `check_task.error_details`，未增加数据库迁移。
- 外部解析客户端对连接、读取、写入、其他网络错误及最终 502/503/504 返回稳定且脱敏的失败分类、尝试次数和耗时。
- `ocr_live_probe.py` 增加 `--mode auto|scan`（默认 `auto`），成功和失败输出均限制为安全指标。
- 46 页验收脚本增加约束：所有保留差异必须为 LOW 且具有 `review_reason`。
- 使用合成单页扫描 PDF 完成一次真实 external `auto` 预检；随后只创建一个 46 页 PDF/PDF 任务，双方各上传一次。三次上传均关闭 HTTP 自动重试，未超预算。
- 唯一 46 页任务 `tsk_01M0F7EP40AEJNRG7CJNET0BS5` 达到 `SUCCEEDED / COMPLETED / 100`，并通过全部硬断言。
- 临时宿主机 API、Worker、fixture server 和临时 PostgreSQL 已停止；默认 Compose API、Worker、PostgreSQL 已恢复，任务在 API 重启后仍可查询。
- 自动浏览器运行时没有可用浏览器，未进行视觉替代或把 HTTP 200 记作视觉通过，因此 PR 保持 Draft。

## 修改文件

- `app/core/errors.py`：为工作流错误增加可选安全详情。
- `app/adapters/document_parser/textin_client.py`：生成网络/超时/上游状态的安全诊断。
- `app/db/repositories/task_repository.py`：失败时持久化安全详情。
- `app/worker/runner.py`：记录白名单安全字段并传给 Repository。
- `scripts/ocr_live_probe.py`：模式参数、安全成功/失败输出。
- `scripts/e2e_ocr_acceptance.py`：强化 LOW 原因码硬门槛。
- `tests/unit/test_textin_client.py`：超时、网络、502/503/504、重试和脱敏测试。
- `tests/unit/test_ocr_live_probe.py`：模式与安全输出测试。
- `tests/integration/test_worker.py`：安全详情持久化、API 返回和日志脱敏测试。
- `README.md`：补充 probe 用法、安全诊断和本次真实验收结果。

## 接口、数据和配置变化

- API：路由、请求结构和公开错误码不变；任务详情的既有错误详情对象可返回安全诊断字段。
- 数据库/迁移：无 schema 变化，复用 `check_task.error_details` JSONB；`alembic check` 无缺失迁移。
- 配置：真实验收进程将 `OCR_HTTP_RETRY_ATTEMPTS=0`，未覆盖 `.env`；示例配置无真实地址或密钥。
- 兼容性：0.4.1 比对、解析路由、结果 schema 和 workflow/rules 版本均未改变。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向 pytest（实现前） | FAILED（预期） | `probe` 尚不存在，形成 TDD 红灯 |
| 定向 pytest（实现后） | PASSED | 25 passed |
| `docker compose --profile tools run --rm test` | PASSED | 最终 103 passed，1 个 LangGraph 上游未来弃用 warning |
| 变更范围 Ruff | PASSED | 无错误 |
| `npm run typecheck` | PASSED | Vue/TypeScript 类型检查通过 |
| `npm run build` | PASSED | 生产构建通过；仅现有大 chunk 提示 |
| `docker compose config --quiet` | PASSED | Compose 配置有效 |
| `docker compose --profile tools build test` | PASSED | 测试镜像由当前源码构建 |
| `docker compose build api worker` | PASSED | API/Worker 共用运行镜像构建通过 |
| `docker compose run --rm api alembic check` | PASSED | `No new upgrade operations detected` |
| 单页真实 probe | PASSED | 1 页、5 blocks、引擎 3.20.11、552 ms、17,097 bytes、平均/最低置信度 0.9664 |
| 46 页真实任务 | PASSED | 84.782 s，external `auto` 双侧 46/46 页，结果 33,260 bytes |
| 比对精度硬门槛 | PASSED | reliable=true；双侧覆盖率 1.0；3 LOW；0 HIGH/0 MEDIUM/0 numeric；较 2,099 项降低 99.8571% |
| 外部响应边界 | PASSED | baseline 5,250,312 bytes / 37,832 ms；target 5,280,075 bytes / 37,218 ms，均低于 50 MiB |
| 日志和临时目录检查 | PASSED | Key、OCR 地址、完整 fixture 文件名、traceback 泄露均为 0；临时工作目录残留 0 |
| `/health`、`/ready`、`/docs`、`/console/` | PASSED | 默认 Compose 恢复后全部 HTTP 200 |
| API 重启持久化 | PASSED | 真实任务重启前后均为 `SUCCEEDED` |
| 命名卷检查 | PASSED | `contract-review-postgres-data` 保留 |
| 自动浏览器视觉验收 | 未执行/待验收 | 浏览器运行时返回空可用列表；未用 HTTP 200 代替视觉结论 |

## Docker 与运行状态

- API：`contract-review-api-1`，healthy，`127.0.0.1:8000`。
- Worker：`contract-review-worker-1`，running。
- PostgreSQL：`contract-review-postgres-1`，healthy；命名卷保留。
- 控制台：`http://localhost:8000/console/` 可访问，视觉清单待人工确认。
- 最终是否保持运行：是，默认 Compose 三个服务保持运行。

## 重要决策

- 单页预检成功后只创建一个 46 页任务，严格消耗总计三次上传；没有自动或人工重试真实调用。
- 自动浏览器不可用属于视觉验收阻塞，不影响已经获得的后端真实闭环证据，但按 Ready 门槛保持 PR Draft。
- 不新增 GitHub Actions；继续以本地 Docker 全量测试和真实验收作为 PR 证据。

## 已知问题与风险

- 控制台尚需人工确认，未完成前 PR #2 不能标记 Ready。
- 本轮按范围没有验证甲方 Docker 网络；宿主机成功不能替代部署侧 Worker 容器的单页验证。
- 约 200 页、异步 OCR、DRAFT_REVIEW、LLM 和复杂规则仍未进入本阶段。
- LangGraph 依赖存在一个上游未来弃用 warning，不影响当前执行。

## 控制台人工验收清单

1. 打开任务 `tsk_01M0F7EP40AEJNRG7CJNET0BS5`，确认中文状态为成功、执行模式为规则比对。
2. 确认对齐可靠、双侧覆盖率 100%、候选/最终差异均为 3，warning 为聚合展示。
3. 确认 OCR 人工复核区默认折叠；展开后 3 项均显示中文原因、LOW 和人工复核提示。
4. 确认每项差异至少一侧显示可追溯位置，多位置可展开，表格位置含行列/坐标信息。
5. 确认长文本不会破坏布局；在具有超过 20 项差异的历史任务上确认每页 20 项分页。
6. 确认原始 JSON 可查看，且不展示鉴权信息、OCR 服务地址或合同全文之外的额外敏感运行配置。

## 下一步建议

1. 由人工完成上述控制台清单并把结果补入 PR #2；全部通过后再将 Draft 标记 Ready，不合并。
2. 进入真实 DOCX → 盖章扫描 PDF 主黄金集，建立跨格式的长期回归基准。
3. 在甲方内网部署时补 Worker 容器到 OCR 的单页验证，再评估大文件和异步 OCR。

## 下一会话首先阅读

- `AGENTS.md`
- `README.md`
- `docs/progress/README.md`
- `docs/progress/20260820-172227_ocr-acceptance-unblock.md`
- `docs/plans/20260820_final-compare-alignment.md`

## 交接摘要

0.4.1 安全诊断与 probe 修复已实现，Docker 全量 103 项测试通过。
真实调用严格限制为三次且全部成功，没有重试。
唯一 46 页任务 84.782 秒完成，双侧 46/46 页、覆盖率 100%。
最终仅 3 LOW，0 HIGH/0 MEDIUM/0 numeric，结论 REVIEW_REQUIRED。
日志泄露和临时目录残留均为 0，API 重启后任务仍存在。
默认 Compose API、Worker、PostgreSQL 已恢复并保持健康。
自动浏览器不可用，控制台视觉清单待人工完成，PR #2 必须继续保持 Draft。
