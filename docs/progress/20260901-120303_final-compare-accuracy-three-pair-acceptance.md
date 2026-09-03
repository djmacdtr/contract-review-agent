# FINAL_COMPARE V2 准确率收敛三组验收记录

## 执行范围

- 使用最新持久化 OCR/page cache，以公开 `POST /api/v1/final-comparisons` 串行创建三组全新任务。
- 未调用 retry、未修改历史报告；临时宿主机 Worker 和文件服务在任务结束后已停止，Docker Worker 已恢复。
- 本次不执行重复簇 LLM Canary：首组真实 cache-only 输入的 `suspected_cluster_count=0`，状态为 `SKIPPED_NO_CANDIDATES`。

## 离线门禁

- 首组 V2 cache-only dry-run：`SUCCEEDED`。
- 首组嵌入式质量审计：候选 `137`，确认变化 `68`，待复核 `69`，编号联动吸收 `42`，连续删除聚合 `14`；对齐 `reliable=true`。
- 公开证据页码：`298/298`，缺失 `0`。
- OCR、LLM、数据库写入：均为 `0`。
- 定向 pytest：`108 passed`；变更范围 Ruff、compileall、`git diff --check`：通过。
- 保留 5/10/16 合成重复簇作为离线回归样本；未把它们硬编码为真实输入门禁。

## 正式任务结果

| 组 | 任务 ID | 状态 | diff/risk | review | 通过 | Advice 模型/回退 | 页码覆盖 | 印章 |
|---:|---|---|---:|---:|---:|---:|---|---:|
| 1 | `tsk_01M1DJ164WN825WNCFNN362ZWV` | `SUCCEEDED / COMPLETED / 100` | 68/68 | 69 | 2 | 64/4 | 298/298 | 22 |
| 2 | `tsk_01M1DJ34FAHC3WATPK64ABGPQV` | `SUCCEEDED / COMPLETED / 100` | 20/20 | 23 | 3 | 20/0 | 104/104 | 12 |
| 3 | `tsk_01M1DJ3Q9JE6QKWKQNVMGHM6SD` | `SUCCEEDED / COMPLETED / 100` | 22/22 | 0 | 1 | 22/0 | 66/66 | 12 |

- 三组任务均为全新任务，`source_task_id=null`，无私有兼容参数。
- 三组 OCR 调用均为 `0`，全部命中持久化解析/page cache；正式 Advice 的 HTTP 调用均为 HTTP 200。
- 所有公开差异/风险/复核证据的页码覆盖完整，没有回退到段落、表格或行列编号。
- V2 不确定结构候选已通过 `review_items` 保留证据；确认候选才进入正式风险/差异/通过项。

## 控制台路径

- [第 1 组报告](/console/#/tasks/tsk_01M1DJ164WN825WNCFNN362ZWV/report)
- [第 2 组报告](/console/#/tasks/tsk_01M1DJ34FAHC3WATPK64ABGPQV/report)
- [第 3 组报告](/console/#/tasks/tsk_01M1DJ3Q9JE6QKWKQNVMGHM6SD/report)

## 未完成项

- 5/10/16 真实历史组未在最新 OCR 输入中出现，未强行制造或调用模型合并。
- 控制台视觉、建议语气和印章 Tab 切换仍以业务方人工抽查为准；自动验收已完成状态、Schema、证据、页码和安全统计校验。
