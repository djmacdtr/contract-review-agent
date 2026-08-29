# 数值候选动态 Schema Canary 记录

日期：2026-08-28

## 本轮范围

- 仅为数值候选响应增加按本批候选数量生成的 `minItems`/`maxItems`。
- 数值 payload 增加 `required_decision_count`。
- 保留程序侧候选索引全集校验，不将漏项默认为 `IGNORE`。
- 诊断入口增加显式 `--batch-id` 单批模式；无法与来源任务失败详情唯一对应时安全停止。

## 离线门禁

- `.venv` 定向动态 Schema 测试：2 passed。
- 严格漏项回归测试：1 passed。
- `compileall`：通过。
- 变更相关 Ruff：通过。
- `git diff --check`：通过。
- 未运行 Compose 全量测试，未调用 OCR。

## 唯一真实 Canary

- 来源失败任务：`tsk_01M13MBF329VRRKTKA4DRR0K04`
- 失败批次：`batch_82f33fc688268e0620568ec0`
- 目标文件 SHA-256：`730e27c9305053bb047014efb75bb88db3b6ba45aba46f13337c84a25fd0b228`
- 重建批次：6 个结构单元、1 个数值候选。
- 诊断安全统计：91 个严格缓存未命中、3 个 profile checkpoint、5 个 numeric checkpoint，目标 profile 严格命中。
- LLM 调用：1 次；未执行结构纠错重试。
- 结果：失败。
- 首个安全错误：`failure_stage=NUMERIC_CANARY`，`failure_code=NUMERIC_CANDIDATE_UNCLASSIFIED`。
- 安全输出：`.real-diagnostic-temp/numeric-batch-canary-20260828.json`。

## 结论与未完成项

本次真实响应仍未通过候选索引全集校验。按唯一 Canary 门禁停止：未调用 retry、未创建恢复任务、未再次调用外部服务。尚不能宣称动态 Schema 修复已通过真实验收；后续需在不重放本批次的前提下检查实际请求/响应协议或服务端对动态 Schema 的支持情况。
