# Numeric 叶子候选恢复诊断记录

## 范围

- 本次仅针对全新三文件任务 `tsk_01M16HJ4ZB4ZXBEG2FKRZF7XSB` 的首个 Numeric 失败批次执行精确 Canary。
- 失败批次：`batch_ffa43f22a1bf7903d86d05dd`。
- 未重跑 OCR、未创建任务、未调用 retry；Docker Worker 在 Canary 前已停止。

## 离线实现与检查

- Numeric 输出预算改为按候选数动态计算，范围为 `2048..8192`，不再受 `4096` 上限限制。
- Numeric 叶子截断按稳定 `candidate_index` 平衡拆分；子批次保持同一结构上下文，Reduce 校验候选全集和去重。
- 单候选叶子截断立即安全失败，不继续拆分或重复发送相同请求。
- 安全失败详情及 Worker 结构化日志补充 `numeric_candidate_count`。
- 定向 Numeric 测试：通过；覆盖预算、空批次过滤、`4 → 2 + 2` 候选拆分、候选索引覆盖、单候选叶子终止。
- Ruff、compileall、`git diff --check`：通过。

## 精确 Canary

- 来源任务：`tsk_01M16HJ4ZB4ZXBEG2FKRZF7XSB`
- 文件 SHA：`730e27c9305053bb047014efb75bb88db3b6ba45aba46f13337c84a25fd0b228`
- 来源成功 checkpoint：`profile-v2=3`、`numeric-v2=49`。
- 规划安全统计：`planned_batch_count=41`、`strict_hit_count=36`、`cache_miss_count=6`、最大结构单元数 `12`。
- Canary 调用数：`1`；请求对象为指定单候选 Numeric 叶子。
- 结果：`FAILED`，`failure_stage=NUMERIC_CANARY`，`failure_code=LLM_OUTPUT_TRUNCATED`，`candidate_count=1`、`unit_count=1`。

## 结论

- 单候选叶子仍发生截断，已达到安全恢复边界；本轮不再调用外部服务。
- 未执行标准 retry，未创建第二任务，未修改原任务或历史报告。
- 正式闭环未完成：需后续针对该单候选叶子的实际输入预算/请求形态另行决策；本轮不引入估算页码、不放宽候选全集校验、不扩大恢复预算。
