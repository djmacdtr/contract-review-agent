# FINAL_COMPARE V2 表格逻辑验收记录

- 时间：2026-09-01 12:53（Asia/Shanghai）
- 范围：仅验证 `FINAL_LOGICAL_V2` 的表格稀疏列、纵向合并和键值行对齐；未修改历史任务或报告。
- 外部创建方式：三组均只通过公开 `POST /api/v1/final-comparisons` 创建一次，未调用 retry。

## 离线门禁

- V2 cache-only dry-run：三组均 `SUCCEEDED`，OCR/LLM/数据库写入均为 `0`。
- 待复核数量：第 1 组 `1`（上限 10）、第 2 组 `0`（上限 5）、第 3 组 `0`。
- 公开页码覆盖：第 1 组 `247/247`、第 2 组 `64/64`、第 3 组 `66/66`。
- 重复簇：三组均 `SKIPPED_NO_CANDIDATES`，LLM 调用 `0`。
- 定向测试：`115 passed`（1 warning）。
- 变更范围 Ruff、compileall 和 `git diff --check`：通过。
- Compose 测试：`503 passed, 12 failed`；失败均位于既有抽取 checkpoint/错误上下文回归，不属于本轮表格比较路径，未改动这些无关模块。

## 正式三组结果

| 组 | 任务 ID | 状态 | 差异/风险 | 通过 | Advice（模型/回退） | 页码 | 印章 |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | `tsk_01M1DMWYQRDA72EE5S8J0K0NF3` | `SUCCEEDED / COMPLETED / 100` | 92/92 | 1 | 84/8 | 247/247 | 22 |
| 2 | `tsk_01M1DMZ8PTKNV070Z85P66BF4J` | `SUCCEEDED / COMPLETED / 100` | 21/21 | 3 | 21/0 | 64/64 | 12 |
| 3 | `tsk_01M1DMZKMER2J6J3M61P5RC5NQ` | `SUCCEEDED / COMPLETED / 100` | 22/22 | 1 | 22/0 | 66/66 | 12 |

- 三组均持久化成功，结果接口和任务接口均返回 HTTP 200，创建接口均返回 HTTP 202。
- OCR 业务调用为 `0`；所有实际 LLM HTTP 响应为 HTTP 200。每组记录的 LLM 请求数为 `14/3/3`，Advice 均在最终差异之后生成。
- 三组任务均为全新任务，`source_task_id=null`，文件 ID 唯一，未设置旧恢复或再生成标记。
- 页码均通过真实 sidecar/OCR 缓存校验；未回退段落、表格行列或估算页码。
- 第一组仍有 8 项 Advice 使用安全 fallback；这不阻断任务，但建议文案覆盖的人工语气仍需用户在控制台抽查。

## 控制台路径

- 第 1 组：`/console/#/tasks/tsk_01M1DMWYQRDA72EE5S8J0K0NF3/report`
- 第 2 组：`/console/#/tasks/tsk_01M1DMZ8PTKNV070Z85P66BF4J/report`
- 第 3 组：`/console/#/tasks/tsk_01M1DMZKMER2J6J3M61P5RC5NQ/report`

## 收尾状态

- 正式验收脚本已停止宿主机临时执行器并恢复 Docker Worker；API、PostgreSQL、Worker 均健康。
- 原有工作区修改、历史报告和 `.real-diagnostic-temp/` 均保留。
- 未执行 reset、clean、commit 或 push。
- 未完成项：Compose 中与本轮无关的 12 个旧抽取测试失败，以及控制台视觉/建议语气的人工抽查。
