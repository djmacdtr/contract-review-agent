# FINAL_COMPARE 逻辑候选校验离线实现记录

## 状态

已完成代码实现与定向离线验证；真实 FINAL_COMPARE 验收待后续明确授权后执行。

## 范围

- 仅增加 opt-in 的 `FINAL_LOGICAL_V2` 比对路径。
- 默认 `FINAL_COMPARE_CANDIDATE_VALIDATION_ENABLED=false`，LEGACY 路径保持原行为和 0.6 版本结果标识。
- 未调用真实 OCR/LLM，未创建或修改业务任务，未修改数据库结构和公开 API。
- 保留已有工作区修改、`backups/`、`tmp/` 和 `.real-diagnostic-temp/`。

## 实现摘要

- 新增逻辑表格视图：按解析器提供的逻辑单元合并 DOCX 合并单元格和 OCR 跨行/跨列单元格，同时保留全部物理位置。
- 新增表头、行键和列键匹配；结构不确定时保留差异并标记 `REVIEW_REQUIRED`，不静默删除。
- 新增证据位置范围内的确定性去重和差异类型优先级，避免全文同文案的全局去重。
- 新增候选 LLM 校验协议：最多 8 项一批、顺序处理；失败/不确定保留差异，只有证据完全相同的重复候选才允许移除。
- 新增安全候选统计和模型运行元数据；候选正文仅作为模型请求输入，不写入日志或诊断摘要。
- 风险结果传递 `validation_status`、`validation_source`、`validation_reason_code`；前端对待复核风险显示“待人工复核”。
- 新增只读 `scripts/final_compare_logical_dry_run.py`，仅读取既有 TaskResult 做规则候选摘要，不调用外部服务、不写数据库。
- 保留既有 DOCX/OCR 逻辑位置元数据改动，未改变 DRAFT_REVIEW 默认业务链路。

## 离线验证

| 检查 | 结果 |
| --- | --- |
| `.venv\\Scripts\\python.exe -m pytest -q tests/unit/test_final_compare_logical_v2.py tests/unit/test_openai_llm_client.py tests/unit/test_comparison.py tests/unit/test_final_compare_workflow.py tests/unit/test_risk_model.py tests/unit/test_advice_batches.py tests/unit/test_result_advice.py` | 139 passed |
| `ruff check` 变更相关 Python 文件 | 通过 |
| `.venv\\Scripts\\python.exe -m compileall -q app scripts/final_compare_logical_dry_run.py` | 通过 |
| `npm run test:format` | 通过 |
| `npm run typecheck` | 通过 |
| `npm run build` | 通过；仅有既有 bundle size warning |
| `git diff --check` | 通过；仅有 CRLF 转换提示 |

只读 dry-run：`scripts/final_compare_logical_dry_run.py --task-id tsk_01M1BBHY5424N69QRDFA8N96VZ` 已读取成功 FINAL_COMPARE 结果并完成规则摘要；结果为 `stored_diff_count=189`、`candidate_count_after_rules=189`、`review_required_count=0`，外部调用和数据库写入均为 0。

## 未完成项

- 尚未启用 V2 配置执行真实任务，未进行模型候选 Canary、OCR 或控制台验收。
- 页码和现有缓存外围逻辑未在本轮通过真实服务重新验收；视觉验收由用户负责。
- 本轮未执行全量 Compose 回归、Docker 冒烟、commit 或 push。

## 后续

真实验收前应由运行管理会话确认服务与配置，再在明确授权下开启 `FINAL_COMPARE_CANDIDATE_VALIDATION_ENABLED`，使用受控样本执行唯一验收任务；失败时保留首个安全阶段和错误码，不连续创建任务。
