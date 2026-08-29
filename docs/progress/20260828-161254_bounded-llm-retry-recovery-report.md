# 有限 LLM HTTP 重试与唯一 checkpoint 恢复记录

日期：2026-08-28

## 实现与门禁

- 对 HTTP `429/500/502/503/504` 增加最多 4 次重试，退避基线为 1、2、4 秒并加入随机抖动。
- Schema、证据校验、鉴权、端点和其他业务错误不进入该 HTTP 重试路径。
- 超时保持原有 2 次总尝试行为。
- 宿主机恢复脚本的 LLM 与抽取任务并发固定为 1；分片参数未改变。
- LLM Client 重试定向测试：10 passed。
- 抽取/数值安全诊断定向测试：2 passed。
- `compileall`、变更相关 Ruff、`git diff --check`：通过。
- 未运行全量回归，未调整 batch ID、业务规则或结果 Schema。

## 唯一恢复任务

- 来源任务：`tsk_01M13NE8NXXFAGC1XFQVQC3303`
- 来源成功 checkpoint：78 条。
- 唯一恢复任务：`tsk_01M13PH5H5EAWJXJRCFKH00PH0`
- 新任务成功 checkpoint：81 条。
- 任务结果：`FAILED / FACT_EXTRACTION / 75`。
- 首个安全失败：`chain=numeric`、`batch_depth=1`、`unit_count=1`、`batch_id=batch_79e5768df5a7920335c383b2`、`failure_code=LLM_UPSTREAM_ERROR`。
- LLM HTTP 调用：115 次，其中 5 次返回 200、110 次返回 500。
- 有限重试已耗尽；按止损规则未再次 retry、未创建第二个恢复任务。
- 安全输出：`.real-diagnostic-temp/http-retry-recovery-20260828.json`。

## 当前结论

分片参数和 checkpoint 复用路径保持不变；有限 HTTP 重试已生效，但甲方网关在该恢复期间持续返回 500，正式报告仍未闭环。Docker Worker 已恢复，API、PostgreSQL 和 Worker 均处于运行状态。工作区及 `.real-diagnostic-temp/` 保留，未执行 commit、push、reset 或 clean。
