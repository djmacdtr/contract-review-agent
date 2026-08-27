# 最小文本分片定向恢复诊断

日期：2026-08-27

## 结论

本轮唯一正式恢复任务已按计划停止，未调用 retry，也未创建第二个任务。页码探针、OCR/LLM 宿主机网络和页码映射不是本次终止原因。

任务来源：`tsk_01M10C9J6HDXNW429E051KAZ12`

唯一恢复任务：`tsk_01M10EJTDQ8S62YEYHME3T5VBE`

恢复任务最终状态：`FAILED / FACT_EXTRACTION / 75`

安全错误摘要：

```text
code=DYNAMIC_CHECK_INCOMPLETE
failure_stage=FACT_EXTRACTION
chain=text
file=fil_01M10EJTDQ8S62YEYHME3T5VBH
batch_depth=0
unit_count=1
failure_code=DYNAMIC_CHECK_INCOMPLETE
```

错误摘要不含异常文本、合同正文、完整模型响应、文件 URL 或密钥。

## Checkpoint 诊断

来源任务在恢复前已有 43 个成功 checkpoint：

```text
profile-v2: 3
numeric-v2: 23
text-v4: 17
```

恢复任务终止时已保存：

```text
profile-v2: 3
numeric-v2: 23
text-v4: 18
```

只读比较显示，恢复任务的 checkpoint 没有与来源任务同时满足 `file_sha256 + batch_id + extraction_version + payload_digest` 的完整身份匹配；文本链有 10 个相同 `batch_id`，但其 payload digest 不同。也就是说，本轮没有证据证明 43 个来源 checkpoint 被可靠复用，恢复任务实际重新产生了抽取 checkpoint，最终触发控制器级动态抽取终止。

这暴露出后续需要单独处理的跨任务 checkpoint 身份稳定性问题；本轮不修改 checkpoint 校验门，也不再次调用甲方 OCR/LLM。

## 本轮实现与离线验证

- 抽取失败上下文已安全传播：`failure_stage`、`chain`、`file`、`batch_depth`、`unit_count`、`failure_code`。
- Worker retry 已将数据库 `source_task_id` 注入工作流内部 options，公开 retry 接口结构未变。
- 新增错误传播和 Worker retry 传递测试。
- Compose 测试：`353 passed`。
- 变更文件 Ruff、`compileall`、`git diff --check` 通过。
- 宿主机 Worker 和本地文件服务已停止，Docker Worker 已恢复运行；正式环境白名单未被修改。

## 未完成项

- 未达到 `SUCCEEDED / COMPLETED / 100`，因此没有正式结果、39 项差异、4 项通过、页码展示、局部高亮和建议覆盖率可验收结论。
- 未进行控制台正式报告验收；仅保留任务列表路径 `/console/#/tasks` 作为后续验收入口。
- 需要在下一阶段设计兼容现有来源 checkpoint 的跨任务身份映射，并在获得明确授权后重新进行一次恢复验收；本轮不原样重跑。
