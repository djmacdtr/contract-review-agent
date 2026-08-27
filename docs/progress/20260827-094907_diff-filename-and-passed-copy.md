# 任务进度：差异文件名与通过项文案生效

## 基本信息

- 时间：2026-08-27 09:49:07 +08:00
- 状态：COMPLETED
- 任务类型：FIX / BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`23246f2`
- 工作树状态：dirty；保留本任务开始前已有的后端、页码、报告证据及其他未提交修改

## 用户目标

修复差异证据缺失侧不显示文件名的问题，并让指定任务的“校验通过”诊断文案通过实际运行中的控制台生效；不修改后端结果生成、校验算法、API Schema、数据库或任务状态。

## 本次完成

- 新增统一差异位置格式化逻辑：
  - 目标侧缺失时使用 `TARGET` 文件名。
  - 基准侧缺失时优先使用 `BASELINE`，起草检查回退使用 `TEMPLATE`。
  - 保留缺失内容的页码和页区间。
- `DiffEvidence.vue` 与 `TaskDetailView.vue` 统一使用共享位置格式化逻辑。
- 保留通过项展示层的差异化 AI 诊断文案；当前指定任务 API 返回的旧描述未被改写，前端 bundle 负责展示优化。
- 使用 Docker 当前构建上下文重建并仅刷新 API 控制台容器，使指定任务页面加载新版静态资源。

## 修改文件

- `frontend/src/utils/reportEvidence.ts`：新增缺失差异侧文件角色回退和统一位置格式化。
- `frontend/src/components/report/DiffEvidence.vue`：使用统一位置格式化，移除无文件名的固定返回。
- `frontend/src/views/TaskDetailView.vue`：复用统一差异位置格式化。
- `frontend/tests/reportEvidence.test.ts`：新增缺失目标侧、缺失基准侧文件名及页区间测试。
- `docs/progress/20260827-094907_diff-filename-and-passed-copy.md`：记录本轮完成情况。

## 接口、数据和配置变化

- API：无变化；指定任务原始通过项仍返回既有描述。
- 数据库/迁移：无变化。
- 配置：无变化。
- 兼容性：既有 `DiffItem` 和 `ResultFile` 数据结构保持兼容；缺失侧按文件角色补齐展示文件名。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `npm --prefix frontend run test:format` | 通过 | 通过项文案与差异位置测试均通过 |
| `npm --prefix frontend run typecheck` | 通过 | `vue-tsc --noEmit` 无错误 |
| `npm --prefix frontend run build` | 通过 | Vite 转换 1494 modules；仅有既有大 chunk warning |
| `git diff --check` | 通过 | 无空白错误；仅有 Windows LF/CRLF 提示 |
| `docker compose up -d --build --no-deps api` | 通过 | API 镜像重建并仅重新创建 `contract-review-api-1` |
| `GET /health` | 通过 | HTTP 200 |
| `GET /console/` 与静态 bundle 核对 | 通过 | 新资源包含模板完整性、租赁期间、首期利率、租金期数新版文案 |
| 指定任务结果核对 | 通过 | `tsk_01M0Z48FK9QFS0J83HV14GMNP0` 仍为 `SUCCEEDED`，原始 4 条通过项数据未改变 |

## Docker 与运行状态

- API：`contract-review-agent:dev`，running，已刷新为包含最新前端资源的容器。
- Worker：running，未重启。
- PostgreSQL：running，未重启、未修改数据。
- Fixture server：running，未重启。
- 控制台：`http://127.0.0.1:8000/console/` 返回 HTTP 200；新版 bundle 已生效。
- 最终是否保持运行：是；仅 API 被重建并保持运行。

## 重要决策

- 通过项继续采用展示层格式化，因此历史任务无需重新执行即可显示新版文案。
- 文件名回退只依赖已有文件角色，不伪造文件 ID 或合同正文。
- API 原始结果保持旧描述，避免扩大到后端生成逻辑。

## 已知问题与风险

- 浏览器截图和视觉验收未由 Agent 执行；用户需刷新报告页确认页面视觉效果。
- 工作树仍包含本任务开始前的其他未提交修改，未整理、覆盖或清理。

## 下一步建议

1. 刷新指定报告页，确认四条通过项分别显示新版诊断文案，并确认差异项缺失侧显示文件名。

## 下一会话首先阅读

- `AGENTS.md`
- `frontend/src/utils/reportEvidence.ts`
- `frontend/src/components/report/DiffEvidence.vue`
- `frontend/src/utils/passedCheckDescription.ts`
- `docs/progress/20260827-094907_diff-filename-and-passed-copy.md`

## 交接摘要

缺失差异侧现在按报告类型补齐目标或基准/模板文件名。
报告页和任务详情页已共用统一差异位置格式化逻辑。
指定任务 API 数据未被修改，新 API bundle 已包含新版通过项文案。
前端测试、typecheck、build、diff check 和 API 健康检查均通过。
Worker、PostgreSQL 和 fixture-server 未重启，未执行 commit、push、reset 或清理。
