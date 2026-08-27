# Numeric 调用预算门禁与唯一 Retry 验收

日期：2026-08-27

## 实施与离线门禁

- 总逻辑调用硬上限已调整为 `256`；numeric 规划仍为最多 `6` 个结构单元，恢复路径未改变。
- 增加零调用预检，并将预算耗尽内部子码固定为 `EXTRACTION_CALL_BUDGET_EXHAUSTED`。
- 配置相关定向测试：`3 passed`；未运行全量测试或 Canary。
- 宿主机 Worker 实际启动配置：`json_schema`、原生结构化输出开启、numeric 单批最多 `6`、总上限 `256`、单文档上限 `128`。

来源任务 `tsk_01M1127FS0ACRW0A3T0EBCMR63` 的只读预检结果：

- profile 严格命中：`3`
- numeric 严格命中：`50`
- text 严格命中：`0`
- numeric cache miss：`46`
- text cache miss：`39`
- 实际待调用 cache miss 合计：`88`
- 最大单文档 miss：`54`，低于单文档上限 `128`
- 预检 LLM 调用：`0`

预检未发现需要提高单文档上限至 `192` 的情况。

## 唯一正式 Retry

按计划仅调用一次 retry：

- 来源任务：`tsk_01M1127FS0ACRW0A3T0EBCMR63`
- 新任务：`tsk_01M113Z6XJAAF7AFE41HPV8YQF`
- 新任务状态：`FAILED`
- 失败阶段：`FACT_EXTRACTION`
- 失败进度：`75`
- 来源 checkpoint 严格预检命中：`53`（`3` profile、`50` numeric）
- 新任务截至失败已持久化：`3` profile、`97` numeric、`25` text checkpoint

Worker 的首个安全失败诊断为：

```text
failure_stage=FACT_EXTRACTION
chain=text
file_id=fil_01M113Z6XJAAF7AFE41HPV8YQJ
batch_depth=1
unit_count=1
batch_id=batch_f194945649fc1f6e0613557c
failure_code=FACT_VALUE_NOT_GROUNDED
```

该失败不是 `EXTRACTION_CALL_BUDGET_EXHAUSTED`，说明本轮预算门禁没有再次误伤；错误发生在一个最小 text 分片的事实值证据校验。错误详情未包含合同正文、完整模型响应、URL 或密钥。

## 收尾状态

- 未调用第二次 retry，未创建第二个正式任务。
- 宿主机 Worker 和临时本地文件服务已停止；Docker Worker 已恢复并健康运行。
- Docker Worker 启动日志确认 `json_schema=true` 对应的响应模式和原生结构化输出均已生效。
- 未修改持久化 `.env` 的敏感配置，未清理 `.real-diagnostic-temp/`。
- 因任务失败，39 项差异、4 项通过、页码/高亮/建议的最终正式验收未完成；控制台仅可确认失败任务，不发布成功结论。
- 未执行 commit、push、reset 或 clean。

