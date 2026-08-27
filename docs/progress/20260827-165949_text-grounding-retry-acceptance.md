# Text 证据过滤与唯一 Retry 验收

日期：2026-08-27

## 实施与离线验证

- 新增 `filter_text_fact_evidence`，对 Schema 通过的非数值候选逐项执行原文回查、位置回查和身份校验。
- 有依据候选保留；无依据、未知单元、重复或身份冲突候选只记录内部安全计数并丢弃。
- 全部候选被丢弃时，结构单元返回空事实；Schema 错误和批次饱和仍保持严格失败。
- 新增 `scripts/text_grounding_diagnostic.py`，只重建指定 text `batch_id`，关闭 OCR/页码和结构重试，最多一次 LLM 调用，并支持严格 checkpoint 写入。
- 定向证据/诊断测试：`6 passed`；变更文件 Ruff、compileall、`git diff --check` 均通过。
- 追加覆盖“同一模型身份先出现坏候选、后出现好候选”的过滤回归后，定向测试仍为 `6 passed`。
- 未运行 Compose 全量测试。

## 精确批次诊断

- 来源任务：`tsk_01M113Z6XJAAF7AFE41HPV8YQF`
- 文件：`fil_01M113Z6XJAAF7AFE41HPV8YQJ`
- 批次：`batch_f194945649fc1f6e0613557c`
- 结构单元数：`1`
- LLM 调用：`1`
- 结果：`SUCCEEDED`
- 接受事实：`1`
- 丢弃事实：`0`
- 已写入来源任务严格匹配的 `text-v4` checkpoint。

诊断期间未下载文件、未调用 OCR、未启用页码映射，也未创建任务。

## 唯一正式 Retry

仅调用一次 retry：

- 来源任务：`tsk_01M113Z6XJAAF7AFE41HPV8YQF`
- 新任务：`tsk_01M116EKZRT49MHDZ4B9PP24KF`
- 结果：`FAILED / FACT_EXTRACTION / 75%`
- 首个安全错误：

```text
failure_stage=FACT_EXTRACTION
chain=text
file_id=fil_01M116EKZSAEYXJMQ0V05XB9B3
batch_depth=0
unit_count=1
batch_id=batch_34cf6515a30f3658e0e580c2
failure_code=LLM_INVALID_JSON
```

该错误发生在另一个初始 text 批次，不是已诊断的 `batch_f194...`。本轮没有第二次模型调用来处理该新批次，也没有再次 retry。

失败时新任务已保存 `3` 条 profile、`96` 条 numeric、`24` 条 text checkpoint；来源任务在单批诊断后共有 `3` 条 profile、`97` 条 numeric、`48` 条 text checkpoint，其中精确批次已确认存在 1 条成功记录。

## 收尾状态

- 宿主机 Worker 和临时本地文件服务已停止。
- Docker Worker 已恢复运行；API、PostgreSQL、Worker 均健康，Docker Worker 启动日志确认 `json_schema` 和原生结构化输出开启。
- 未调用第二次 retry，未创建第二个正式任务；未修改公开 API、差异算法、页码或 checkpoint 表结构。
- 39 项差异、4 项通过、页码、高亮、建议和控制台成功展示尚未完成，不能发布正式成功结论。
- 未执行 commit、push、reset、clean，也未清理 `.real-diagnostic-temp/`。
