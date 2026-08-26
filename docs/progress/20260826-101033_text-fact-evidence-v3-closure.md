# DRAFT_REVIEW 文本事实证据 v3 闭环记录

日期：2026-08-26

## 实际架构与配置选择

- 文档概况、数值候选和文本事实改为三条内部链路，稳定版本分别为 `profile-v2`、`numeric-v2`、`text-v3`；成功 checkpoint 按批次 ID、文件 SHA、payload 摘要和版本独立保存。
- 数值链继续使用 12,000 字符 payload、24 个候选、最大输出 4,096、单任务并发 2；模板只执行一次概况，不进入事实链。
- 文本链使用普通段落、表格行及必要的表格列组作为结构单元；当前最终配置为单批最多 32 个结构单元、12 项响应上界、12,000 字符 payload。32 是在 16 单元批次实测返回 11 项、64 单元批次实测触发饱和之间的止损折中；固定输出上界不再按每个结构单元虚增 token 预算。
- 文本 quote 校验先做原文唯一精确匹配，失败后仅做 NFKC、全半角、空白/换行、`<br>` 和零宽字符等价匹配，并保留规范化字符到原文字符的位置映射；唯一命中才回填原文片段。多命中、无命中和改写均拒绝。
- LangGraph 仍按受控波次使用 `Send`，Reduce 检查批次幂等、结构单元覆盖、来源位置、候选分类和身份冲突。`FACT_BATCH_SATURATED`、`FACT_UNIT_NOT_FOUND`、`FACT_QUOTE_NOT_GROUNDED`、`FACT_IDENTITY_DUPLICATED` 在 outcome 和安全 JSONL 中保留；恢复预算仍为 `max(2, ceil(N×20%))`，全任务硬上限 50。
- `WorkflowRouter` 已接入现有 SQLAlchemy checkpoint Store；未新增公开接口、业务表、Redis、Celery 或微服务。独立链路的内存 Store、幂等写入和 `source_task_id` 恢复已由离线测试覆盖。

## 网关结构化输出探测

- 已有部署级合成探测对数值和文本 Schema 分别验证 `json_schema`、`json_object`，两种模式均达到 3/3 合法 JSON、Schema 通过、`finish_reason=stop`，最终选择 `json_schema`。
- 本阶段定向诊断和 Canary 未重复执行 12 次探测，直接使用已验证的 `json_schema`；诊断器只有显式传入 `--probe` 才重新探测。
- HTTP 客户端继续 `trust_env=False`。安全指标只记录状态码、finish reason、内容/推理字符数、空内容、代码围栏、JSON 边界和错误摘要，不记录 Key、授权头、合同正文、quote 或完整模型响应。

## 离线验证

执行命令：

```text
ruff check app/adapters/llm app/core/config.py app/draft_review/facts.py app/draft_review/extraction.py app/workflows/draft_review.py app/workflows/router.py scripts/draft_review_real_diagnostic.py tests/unit/test_structured_extraction_v2.py
.venv\Scripts\python.exe -m compileall -q app scripts
.venv\Scripts\python.exe -m pytest tests/unit/test_structured_extraction_v2.py tests/unit/test_openai_llm_client.py tests/unit/test_draft_review_workflow.py tests/unit/test_draft_review_real_diagnostic.py tests/unit/test_draft_review_checkpoints.py -q
git diff --check
```

结果：`73 passed`；Ruff、compileall 和 `git diff --check` 通过。覆盖了饱和、错误 unit、精确 quote、空白/换行/`<br>`、全角、零宽、重复命中、模型改写、独立 checkpoint 复用和安全日志。

## 真实定向诊断与 Canary

- 定向诊断锁：`.real-diagnostic-temp/text-diagnosis-v3-20260826-100135.lock`；指标：`.real-diagnostic-temp/text-diagnosis-v3-20260826-100135.jsonl`。
- 定向诊断实际为 1 次 HTTP、1 次逻辑调用；16 单元文本批次返回 11 项，JSON、Schema、证据和唯一回查均通过。
- Canary 锁：`.real-diagnostic-temp/text-canary-v3-20260826-101500.lock`；指标：`.real-diagnostic-temp/text-canary-v3-20260826-101500.jsonl`。
- Canary 为 5 次业务调用，5/5 通过，均为 `finish_reason=stop`，未发生结构纠错或模糊证据匹配。

## 新三文件任务结果

- 新锁：`.real-diagnostic-temp/structured-v3-full-20260826-103000.lock`；指标：`.real-diagnostic-temp/structured-v3-full-20260826-103000.jsonl`。
- 任务未重复执行网关探测；实际总计 27 次 HTTP、27 次逻辑调用：3 次文档概况、18 次数值候选、6 次文本事实。目标合同实际初始计划为 17 个数值批次和 10 个文本批次；参考函为 1 个数值批次和 2 个文本批次；模板仅概况。当前配置修正后离线计划为目标 17/17、参考函 1/1，模板 0 个事实批次。
- 概况和数值链均完成；目标合同文本首波 6 个大批次均返回合法 JSON，但均达到 12 项上限并产生 `FACT_BATCH_SATURATED`。该子码被正确保留，未计入格式错误熔断；恢复预算随后止损，任务返回 `DYNAMIC_CHECK_INCOMPLETE`。
- 首个失败阶段为 `TEXT_FACT_EXTRACTION`；未进入文本 Reduce、跨文档映射、语义规划、程序数值校验、正式差异或 AI 建议。正式差异 0，双侧证据 0，AI 建议 0；没有发布半成品结果。
- 本次失败后未原样重跑三文件任务。已只调整文本批次结构单元上限并完成离线复核；`.real-diagnostic-temp/`、旧锁和旧指标全部保留。

## 当前未完成项与下一步

- 新三文件真实闭环仍未达到完成门槛；文本 32 单元配置尚未进行新的完整任务验证，按当前指令不再重跑刚失败的完整任务。
- 真实任务尚未证明 SQL checkpoint 的跨真实任务复用；独立 checkpoint 接口、SQLAlchemy Store 和内存恢复语义已实现，离线测试已证明文本失败时成功数值批次可复用。
- 未执行五文件、OCR、Docker、全仓测试、数据库完整验收或迁移连接验收。
- 后续新任务应使用新的任务身份并沿 `source_task_id` 读取同文件 SHA、同版本、同 payload 摘要的成功 checkpoint；首先单独验证文本 32 单元批次的恢复成功率，再决定是否进入新的三文件闭环。
