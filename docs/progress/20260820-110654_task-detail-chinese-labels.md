# 任务进度：任务详情中文标签

## 基本信息

- 时间：2026-08-20 11:06:54 +08:00
- 状态：COMPLETED
- 任务类型：FIX / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-ocr`
- 当前提交：`c7b7490`
- 工作树状态：dirty；保留开始时已存在的 OCR 功能、测试、文档和前端修改。本次新增 `frontend/src/utils/labels.ts`，并定点修改 `frontend/src/views/TaskDetailView.vue` 和本进度文件；未 commit 或 push。

## 用户目标

将任务详情页面中涉及的英文状态、阶段、执行模式、文件角色/解析状态、结论、差异类型和风险等级标签统一改为中文展示。

## 本次完成

- 新增集中式 `displayLabel` 映射，覆盖当前后端任务枚举、阶段、结论、执行模式、文件角色、解析状态、差异类型、风险等级和已知解析器名称。
- 任务详情页的任务状态、当前阶段、执行模式、结论、文件角色/解析器/解析状态，以及差异类型/风险等级均改为调用中文显示映射。
- 未改变 API 返回值、数据库、枚举存储值或原始 JSON 标签页；未知值仍显示原始值，避免接口新增枚举时显示为空。

## 修改文件

- `frontend/src/utils/labels.ts`：新增前端显示文案映射与安全回退函数。
- `frontend/src/views/TaskDetailView.vue`：将详情页的枚举标签切换为中文显示文案。
- `docs/progress/20260820-110654_task-detail-chinese-labels.md`：新增本次交接记录。

## 接口、数据和配置变化

- API：无变化；仍使用既有英文枚举协议值。
- 数据库/迁移：无变化。
- 配置：无变化。
- 兼容性：未知标签回退显示原值；`原始 JSON` 保留原始协议内容，便于联调。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 前置 TDD 编译检查：`tsc src/utils/labels.ts --noEmit ...` | 通过（预期失败） | 实现前确认标签映射模块不存在，报 `TS6053`。 |
| 映射断言：`esbuild ... labels.ts ... | node -e ...` | 通过 | 9 组：任务状态、阶段、执行模式、文件角色、差异类型、等级、结论、解析器和未知值回退。 |
| `npm run typecheck` | 通过 | `vue-tsc --noEmit` 成功。 |
| `npm run build` | 通过 | 1459 modules；存在既有约 988 KB 主包体积提示，未阻塞。 |
| `git diff --check` | 通过 | 无空白错误。 |
| 浏览器页面检查 | 未执行 | 当前环境没有可用浏览器连接；为保护运行中的并行会话服务，未重建或重启 API 镜像。 |

## Docker 与运行状态

- API：`contract-review-api-1`，healthy，映射 `127.0.0.1:8000`。
- Worker：`contract-review-worker-1`，运行中。
- PostgreSQL：`contract-review-postgres-1`，healthy，继续使用命名卷 `contract-review-postgres-data`。
- 控制台：运行中的容器未重建；本次本地前端生产构建已通过。
- 最终是否保持运行：是；未停止、重启或删除服务/volume。

## 重要决策

- 仅翻译 UI 显示值，保持后端英文枚举作为稳定 API 契约。
- 映射集中在独立模块，避免不同页面翻译不一致；未知值采用原值回退，方便发现后端新增枚举。
- 不覆盖 `TaskDetailView.vue` 中本次开始前已经存在的 OCR 展示修改。

## 已知问题与风险

- 运行中 API 镜像尚未包含本地前端构建产物；需要由运行管理会话在合适时机重建/发布后，进行一次浏览器视觉验收。
- 当前没有可连接的浏览器实例，未完成截图级 UI 验证。

## 下一步建议

1. 在合适的发布窗口重建 API 镜像并对任务详情页进行浏览器视觉验收，重点检查截图中的成功、已完成、删除内容和高风险标签。
2. 后续如任务列表也需要统一中文显示，可复用 `displayLabel` 映射替换其中的直接枚举输出。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260820-110654_task-detail-chinese-labels.md`
- `frontend/src/utils/labels.ts`
- `frontend/src/views/TaskDetailView.vue`
- `docs/progress/20260820-103505_ocr-parser-slice.md`

## 交接摘要

任务详情页的英文枚举标签已统一由中文映射显示，接口与数据库值不变。
映射覆盖截图所示的 `SUCCEEDED`、`COMPLETED`、`DELETED`、`HIGH`，并扩展到相关详情标签。
映射断言 9 组、Vue 类型检查和生产构建均通过；`git diff --check` 无错误。
现有 OCR 并行修改均已保留，未 commit、未重启服务、未删除 Docker volume。
当前 API、Worker、PostgreSQL 继续运行健康；运行镜像未重建，因此仍需发布后的视觉验收。
