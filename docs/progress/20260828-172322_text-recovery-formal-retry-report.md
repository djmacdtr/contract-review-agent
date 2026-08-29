# Text 生产范围恢复与唯一正式 Retry 报告

## 结果

- 时间：2026-08-28 17:23:22 +08:00
- 来源任务：`tsk_01M13PH5H5EAWJXJRCFKH00PH0`
- 唯一恢复任务：`tsk_01M13TETNBVT4SRQGPN6S7T56T`
- 状态：`FAILED`
- 控制台路径：`/console/#/tasks/tsk_01M13TETNBVT4SRQGPN6S7T56T/report`
- 未调用第二次 retry，未创建第二个恢复任务。

## 本轮修改

- Canary 的 TARGET/REFERENCE 规划继续复用正式候选规划。
- Canary 直接复用生产抽取模块的文本可恢复错误集合，新增多单元 `LLM_INVALID_JSON` 可恢复判定。
- 增加定向回归：多单元非法 JSON 标记为 `RECOVERABLE`，单结构仍不可恢复。
- 未放宽单元、quote、身份、证据或 Schema 校验。

## Canary 结果

- TARGET 正式最大批次：通过，16 个单元，7 条事实。
- REFERENCE 正式最大批次：HTTP 200 但非法 JSON；按生产语义标记为多单元可恢复，未重放该 Canary。
- Canary 外部调用共 2 次；没有重新调用 REFERENCE Canary。

## 正式 Retry 安全摘要

- 来源成功 checkpoint：81
- 新任务成功 checkpoint：46
- 耗时：436.375 秒
- HTTP 调用：98 次，其中 43 次 HTTP 200、55 次 HTTP 500
- 首个安全失败：`failure_stage=FACT_EXTRACTION`、`chain=numeric`、`batch_depth=1`、`unit_count=1`、`batch_id=batch_19682847a9bc4d57f755e490`、`failure_code=LLM_UPSTREAM_ERROR`
- 任务最终状态：`FAILED`，进度 75；未进入报告验收。

## 验证与服务状态

- 定向测试：1 passed。
- `compileall`、Ruff、`git diff --check`：通过。
- Docker API、PostgreSQL 保持健康；正式 Retry 后已恢复 Docker Worker，当前容器为运行状态。
- 保留 `.real-diagnostic-temp/` 和全部既有未提交修改；未执行 reset、clean、commit 或 push。

## 未完成项

- 甲方 GLM 网关在正式抽取期间出现大量 HTTP 500，导致唯一恢复任务失败。
- 39 项差异、4 项通过、页码、局部高亮和建议覆盖率未能在本轮正式任务中验收。
- 按止损规则不再重试或创建新任务；后续若继续，应先处理该首个上游稳定性问题并重新取得正式验收授权。
