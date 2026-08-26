# DRAFT_REVIEW 事实消费边界收口记录

## 结论

本阶段完成了事实评审、跨文档映射和正式结果消费边界的统一实现与离线验证，但真实三文件任务未达到正式结果发布门，已按安全规则止损。首个新失败原因为映射模型与映射复核模型的实际身份相同，触发独立模型门；没有放宽该门，也没有发布半成品结果。

## 实际架构与配置

- `qualified_fact_refs` 统一计算映射和正式结果可消费的事实集合：事实与评审决定一一覆盖、决定为 `ACCEPT`、事实/决定/整体评审置信度达到现有阈值、证据完整、证据位置可回查，并满足独立模型门。
- `FACT_MAPPING` 只接收目标合同和辅助资料中通过上述门的事实；没有合格事实时生成结构合法的空映射，不回退到未评审事实。
- 映射提案和映射复核使用稳定组合键，严格校验提案、缺失要求与复核决定的一对一覆盖、文件身份和事实来源。
- `_build_result` 再次使用同一 helper 和同一映射复核覆盖校验；只有 `MATCH + ACCEPT` 且双方及整体复核达到阈值、证据完整的映射才进入事实矩阵。拒绝或不确定项不形成通过结论。
- 事实链版本保持 `profile-v2`、`numeric-v2`、`text-v4`；真实任务沿用既有 PostgreSQL checkpoint，当前三文件任务成功复用 92 个 checkpoint。
- `SEMANTIC_PLAN` 保持旁路；默认调用次数为 0。没有修改公开 FastAPI 接口、请求参数、公开结果 Schema 或 `FINAL_COMPARE` 确定性差异逻辑。
- 诊断器只记录事实、决定、提案、复核和正式结果的数量/布尔门控状态；不记录合同正文、证据文本、模型响应、Key、授权头、任务 ID 或签名 URL。

## 网关结构化输出结论

本阶段没有重复执行既有的 12 次复杂网关探测，直接使用已验证的 `json_schema` 配置。LLM Client 继续使用 `trust_env=False`。本次真实阻塞不是 JSON 或 Schema 失败：映射和复核均返回合法 JSON，逐项复核覆盖完整，整体置信度和证据完整性均通过。

新增安全计数显示，映射与映射复核的实际模型身份相同（`independent_models=0`）。因此严格消费门拒绝全部 3 条映射，避免把同模型重复判断当成独立共识。映射阶段现已将该情况分类为 `MAPPING_MODEL_NOT_INDEPENDENT`；此前同一原因在正式汇总中表现为 `FACT_CONSENSUS_INCOMPLETE`。

## 离线测试

执行结果：

```text
.venv\Scripts\python.exe -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py -q
130 passed, 1 warning

ruff check app/draft_review/facts.py app/workflows/draft_review.py scripts/draft_review_real_diagnostic.py tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py
All checks passed

.venv\Scripts\python.exe -m compileall -q app scripts
通过

git diff --check
通过
```

新增/复核内容包括：被拒绝或未覆盖事实不进入映射、映射提案和缺失要求的完整复核、额外/重复/遗漏决定、错误文件或未知事实引用、同模型映射复核阻断，以及安全聚合指标不包含事实文本。

## 最新真实三文件任务

任务使用固定的目标合同、模板和项目方案确认函，创建了新的唯一锁和 JSONL；旧 `.real-diagnostic-temp` 内容未删除或覆盖。任务沿 `real_667625e6654d8f3e` 复用 checkpoint，没有执行 OCR、网关 probe、事实抽取或 `SEMANTIC_PLAN`。

| 项目 | 结果 |
|---|---:|
| 新增 HTTP 调用 | 2 |
| 新增逻辑调用 | 2 |
| checkpoint 复用 | 92 |
| checkpoint 写入当前任务 | 92 |
| 运行耗时 | 约 22 秒 |
| `FACT_MAPPING` | 1 次，3 条提案 |
| `FACT_MAPPING_REVIEW` | 1 次，3/3 覆盖、3/3 接受 |
| `independent_models` | 0 |
| `SEMANTIC_PLAN` | 0 |
| 正式差异/风险/通过项 | 0，未发布结果 |
| AI 建议 | 0，未进入建议阶段 |

抽取批次复用统计如下；`recovery` 为本次任务新增恢复数：

| 文档 | profile | 数值/文本抽取批次 | recovery |
|---|---:|---:|---:|
| 融资租赁合同（回租）.docx | 1 | 30 | 0 |
| 融资租赁合同（回租）模版.docx | 1 | 0 | 0 |
| 项目方案确认函.docx | 1 | 31 | 0 |

安全聚合结果：目标合同抽取事实哈希计数 311，事实评审覆盖 298（接受 281、拒绝 17、未覆盖 13）；辅助资料抽取和评审均为 18，全部接受并覆盖。映射输入为目标 281、辅助 18；模型返回 3 条 `MATCH`，复核 3 条 `ACCEPT`，但因实际模型身份不独立，正式消费数为 0。

工作流完成到 `FACT_MAPPING_REVIEW`。`NUMERIC_VALIDATION_AND_FORMAL_DIFF` 入口被正式事实消费门阻断，因此没有进入正式结果、风险建议或持久化结果阶段。

## 未完成项与下一步

- 需要甲方网关提供并正确返回两个不同的、可归因的模型身份，且映射模型与映射复核模型均通过现有结构化输出和业务质量门；不能仅修改配置字符串而忽略实际返回模型。
- 外部模型组合就绪后，使用新任务继续复用现有 92 个成功 checkpoint，只重做映射/映射复核及后续正式阶段，不原样重跑已失败任务。
- 本阶段未执行全仓 pytest、前端构建、Alembic 完整验证、Docker 构建/Compose 冒烟、API/Worker/控制台验收、OCR、五文件或 200 页任务；这些留待三文件正式结果成功后执行。

