# 起草检查抽取恢复重复失败根因

## 结论

任务 `tsk_01M10NSNATMYNP4KZPFX6QE1ER` 的 `source_task_id` 和来源文件 ID 映射均正确，但来源任务的成功抽取结果没有被完整复用。任务重新调用文本抽取后，一个目标合同文本单元返回 `finish_reason=length`，形成 `LLM_OUTPUT_TRUNCATED`；当前单结构恢复规则不能继续拆分该错误，任务因此在映射前失败。

这不是 OCR、LLM 网络、`json_schema` 配置或映射逻辑导致的本次失败。

## 数据库只读证据

- 来源任务：`tsk_01M10HF8CBD9HSJXYNCAZ7989E`。
- 恢复任务：`tsk_01M10NSNATMYNP4KZPFX6QE1ER`。
- 来源任务 checkpoint：`profile-v2=3`、`numeric-v2=21`、`text-v4=43`。
- 恢复任务失败时：`profile-v2=3`、`numeric-v2=22`、`text-v4=18`。
- 来源与恢复任务的三个 profile `batch_id` 相同但 `payload_digest` 均不相同。
- 辅助资料 text-v4 的 30 个来源批次只有 8 个相同 `batch_id`，其中仅 5 个摘要相同。
- 目标合同 numeric-v2 的 20 个来源批次没有相同 `batch_id`；目标 text-v4 的 13 个来源批次有 10 个相同 `batch_id`，但摘要匹配为 0。
- 失败文件为目标合同，安全详情为 `chain=text`、`batch_depth=0`、`unit_count=1`、`failure_code=LLM_OUTPUT_TRUNCATED`。

## 实现原因

1. 来源任务保存的是经过失败恢复拆分后的叶子 checkpoint；新任务重新从初始规划开始。现有预恢复只识别一部分表格子单元，无法重建普通二分和全部历史恢复树，因此“来源有 67 条成功记录”不等于新规划可以完整命中。
2. checkpoint 摘要仍包含会随解析/展示变化的位置数据，例如物理页码；页码不属于事实抽取语义，却会使相同内容的摘要变化。
3. 单结构恢复只对部分 `FACT_*` 错误尝试表格/子单元拆分，没有包含 `LLM_OUTPUT_TRUNCATED`。后续可恢复错误集合虽然包含截断，但因为没有生成 `child_groups`，最终仍直接失败。
4. 当前只保存分片结果，不保存每份文档 Reduce 完成后的最终抽取结果。任务在抽取完成、映射失败后，下一次 retry 仍要重新规划全部分片。

## 交付优先修复

1. 新增每文档 Reduce 结果 checkpoint。抽取完整成功后，持久化已合并并通过证据校验的 `DocumentFactExtraction`；retry 优先读取、重新绑定当前文件 ID 并复核证据，然后直接跳过分片规划。
2. 文档级 checkpoint 身份只使用文件 SHA、解析器/抽取版本及影响抽取语义的规范化内容；排除任务文件 ID、物理页码和展示字段。
3. 对首次没有文档级 checkpoint 的任务，允许单结构 `LLM_OUTPUT_TRUNCATED` 进入确定性缩小处理；若结构不可再拆分，则限制该单元返回的高价值事实数量，禁止原样无限重试。
4. 映射或建议失败不得破坏已经成功保存的文档级抽取 checkpoint。
5. 完成定向离线验证后只运行一次新任务。验收指标是三份文档抽取成功并生成文档级 checkpoint；若后续映射失败，再次 retry 的抽取 LLM 调用必须为 0。

## 暂停范围

- 不再追求历史所有分片 checkpoint 布局完全兼容。
- 不修改页码、印章、双模型、语义规划或其他非阻塞功能。
- 不通过提高全局调用上限或重复真实任务掩盖恢复缺陷。
