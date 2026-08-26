# DRAFT_REVIEW 文本候选收缩与三文件闭环记录

## 状态

`PARTIAL_BLOCKED_AT_CHECKPOINT_INFRASTRUCTURE`

本阶段完成了目标合同文本候选收缩、`text-v4` 独立分片和候选 Canary；唯一一次三文件任务在 FACT_EXTRACTION 入口因 PostgreSQL checkpoint 主机 DNS 失败止损。该任务未发出真实模型 HTTP/逻辑调用，未原样重跑。

## 实际架构与配置

- 模板比较结果驱动目标合同文本链：消费 `template_review.diff_items` 和 `diagnostics.filtered_diff_items`，排除无目标侧内容及 `DELETED`，优先使用 `INSERT` 片段，按位置和规范化文本去重。
- 候选保留目标段落或表格单元的位置；最近所属块、前置段落和表头作为 `readonly_context`，模型只能引用候选 `unit_id`，程序回填正式文件身份、证据和位置。
- 目标合同文本链仅处理模板差异候选；本次真实预检为 276 个结构单元、80 个候选、56 个表格位置、12 个文本批次。单批最多 8 个候选，最大 payload 为 10,852 字符。
- 辅助资料仍按全文动态规划，文本结构单元上限调整为 16；数值链保持 12,000 字符和有效 24 个候选上限。文档概况、数值、文本版本分别为 `profile-v2`、`numeric-v2`、`text-v4`。
- 生效安全参数：文本候选 8、辅助文本结构单元 16、简化输出预算 2,000 tokens、LLM `max_output_tokens=4096`、抽取并发 2、波次 6、最大拆分深度 8、诊断任务逻辑调用硬上限 40；旧生产全局默认值未扩大。
- 文本恢复继续使用严格 JSON/Schema/证据验证；饱和、错误 `unit_id`、quote 不唯一或不匹配保留为独立子码并只拆分当前批次。无候选时规划结果为 0 批次，不回退全文扫描。
- 真实诊断器使用新唯一锁和 JSONL，保留 `.real-diagnostic-temp/` 既有内容；真实任务接入现有 SQLAlchemy checkpoint Store，成功读取才允许复用，未引入新公开接口或新服务。

## 网关结构化输出

普通合同任务跳过部署级探测，直接使用此前验证通过的 `json_schema` 配置；本阶段未重复执行 12 次探测。候选 Canary 的 3 个真实请求均返回 `finish_reason=stop`，JSON、Schema 和证据校验全部通过，没有结构纠错和饱和。

## 离线验证

执行：

```text
.venv\Scripts\python.exe -m pytest tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py tests/unit/test_structured_extraction_v2.py -q
ruff check app/adapters/llm app/core/config.py app/draft_review/facts.py app/draft_review/extraction.py app/workflows/draft_review.py scripts/draft_review_real_diagnostic.py tests/unit/test_draft_facts.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_llm_model_eval.py tests/unit/test_structured_extraction_v2.py
.venv\Scripts\python.exe -m compileall -q app scripts
git diff --check
```

结果：`118 passed`；Ruff、compileall、`git diff --check` 通过。新增覆盖候选去重、删除排除、表格位置、只读上下文、`text-v4`、空候选不回退和严格证据回查。

## 真实验证

### 候选 Canary

- 指标文件：`.real-diagnostic-temp/text-candidate-v4-canary-20260826-120000.jsonl`。
- 目标候选总数 80，计划 12 批；执行 3 批，每批 8 个候选。
- 结果：3/3 成功；新增业务调用 3 次，未发生恢复、结构纠错或格式/证据失败。旧锁、旧指标未修改。

### 唯一一次三文件任务

- 指标文件：`.real-diagnostic-temp/text-candidate-v4-full-20260826-120500.jsonl`。
- 三文件已完成下载、解析和模板比较；进入 `FACT_EXTRACTION` 后，SQL checkpoint Store 连接 PostgreSQL 时出现 DNS `gaierror`。
- 指标统计：HTTP 0、逻辑调用 0、checkpoint 复用 0、checkpoint 保存 0；首个失败阶段 `FACT_EXTRACTION`。该次原始内部异常为 DNS `gaierror`；随后已将诊断器映射为不暴露主机细节的 `CHECKPOINT_UNAVAILABLE` 类别，未再次运行任务。
- 因未产生成功模型调用或 checkpoint，不执行原样重跑；正式差异数量、双侧证据数量、AI 建议数量均为 0，映射、语义规划、数值校验、正式结果和建议阶段未完成。

## 当前未完成项与下一步

1. 恢复现有 PostgreSQL checkpoint 数据库的 DNS/可用性，并确认 `extraction_checkpoint` 已完成现有迁移；不得通过禁用 checkpoint 绕过本门槛。
2. 使用下一次新任务沿同一文件 SHA、payload 摘要和 `profile-v2`/`numeric-v2` 读取成功 checkpoint；本次没有可复用记录，因此不能声称已完成复用验收。
3. 在 checkpoint 基础设施恢复后，按既定关卡先验证复用读取，再执行一次新的三文件任务；若出现业务失败，只定位首个新失败阶段，不重跑整条任务。
4. 三文件成功后再统计正式差异、证据对、AI 建议及各工作流阶段；本阶段不启动五文件、OCR、Docker 或全仓测试。
