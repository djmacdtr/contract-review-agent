# FINAL_COMPARE V2 无重复候选正式验收记录

- 日期：2026-09-01
- 范围：三组 FINAL_COMPARE V2 串行验收
- 比较模式：`FINAL_LOGICAL_V2`
- 真实输入使用最新持久化 OCR/page cache；旧报告仅用于数量对照，不作为合并来源。

## 零调用预检

- 首组 cache-only dry-run：通过
- 旧报告差异数：189
- 当前规则候选数：186
- 疑似重复簇：0
- 重复簇 Canary：`SKIPPED_NO_CANDIDATES`
- 质量审计：完全重复签名 0；同位置跨类型组 1、超额候选 1；表格结构不确定 142；待复核 71；双侧标准化文本不同 112；数值变化 30；金额变化 25；期限变化 9；日期变化 4；编号变化 3
- 公开页码证据：596/596
- OCR 调用：0；LLM 调用：0；数据库写入：0

## 三组正式任务

| 组 | 任务 ID | 状态 | 差异/风险 | 通过项 | LLM HTTP | Advice 模型/回退 | 页码覆盖 | 印章图片 |
|---:|---|---|---:|---:|---:|---:|---|---:|
| 1 | `tsk_01M1DF90EP4SD6K1C8E39T9YQK` | `SUCCEEDED / COMPLETED / 100` | 186 / 186 | 0 | 45（全部 HTTP 200） | 139 / 47 | 594/594 | 22 |
| 2 | `tsk_01M1DFN51DNXE43NVYGRPVA81A` | `SUCCEEDED / COMPLETED / 100` | 41 / 41 | 1 | 56（全部 HTTP 200） | 28 / 13 | 144/144 | 12 |
| 3 | `tsk_01M1DFPWBKDMMPZT48ASZKQ55P` | `SUCCEEDED / COMPLETED / 100` | 26 / 26 | 1 | 5（全部 HTTP 200） | 25 / 1 | 82/82 | 12 |

正式任务均通过公开创建接口创建，文件身份检查通过，`source_task_id` 为空且无私有兼容参数。三组任务均未调用 OCR；页码严格覆盖公开证据，未回退段落、表格或行列位置。

Advice 的 `length` finish reason 由分批请求统计保留在原始安全 JSON 中；不影响结果持久化。Advice 回退数量已如实记录，不将其宣称为全量模型建议通过。

## 服务收口

- 宿主机临时 Worker 和文件服务已停止。
- Docker Worker 已恢复运行。
- API、PostgreSQL、Worker 均健康。
- 控制台报告：
  - `/console/#/tasks/tsk_01M1DF90EP4SD6K1C8E39T9YQK/report`
  - `/console/#/tasks/tsk_01M1DFN51DNXE43NVYGRPVA81A/report`
  - `/console/#/tasks/tsk_01M1DFPWBKDMMPZT48ASZKQ55P/report`

## 未完成项

- 5/10/16 合成重复组仅保留为离线回归样本；当前最新 OCR 输入没有可靠疑似重复簇，因此未执行外部重复簇 Canary。
- 控制台视觉、印章 Tab 切换和建议语气仍以人工页面抽查为准。
