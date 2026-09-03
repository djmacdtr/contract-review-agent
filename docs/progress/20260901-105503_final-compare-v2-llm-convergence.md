# FINAL_COMPARE V2 重复差异收敛实施记录

## 状态

离线实现和只读 dry-run 已完成；外部 Canary 与正式任务验收因候选簇门禁未满足而停止。本轮未调用 OCR/LLM，未创建或修改业务任务。

## 范围与保护

- 仅启用 `FINAL_LOGICAL_V2` 的疑似重复簇和受控 LLM 裁决路径。
- DRAFT_REVIEW、FINAL_COMPARE LEGACY、公开 API、数据库 Schema 和历史结果未修改。
- 保留现有工作区修改、`.real-diagnostic-temp/`、备份和缓存；未执行 reset、clean、push 或 commit。
- Docker API、Worker、PostgreSQL 状态未改变，当前仍保持运行。

## 实现摘要

- 增加保守的重复簇构造器：按文件、匹配表格、双侧标准化文本、类型、页码和相邻区域组成 2–16 项簇；不接受数值或实际文字变化作为重复。
- 增加 `validate_final_compare_duplicate_clusters` 内部 LLM 协议，使用严格 JSON Schema、GLM-5.3-Flash、关闭思考模式、4096 token；应用层要求完整候选覆盖、置信度至少 0.95 和全部原始位置保留。
- V2 工作流仅调用重复簇协调器；模型失败、不确定或非法引用保留候选并标记待复核，不阻断确定性结果。
- 增加安全候选/裁决统计和 cache-only dry-run、Canary 脚本；不保存合同正文、完整响应、URL 或凭据。
- 三组公共验收脚本启用 V2，并在首个配对缓存失败或任务失败时停止后续创建。

## 离线验证

| 检查 | 结果 |
| --- | --- |
| V2 与 LLM 定向测试 | `64 passed` |
| FINAL_COMPARE/比较/结果/Advice 相关测试 | `149 passed` |
| 变更范围 Ruff | 通过 |
| 相关 Python compileall | 通过 |
| 前端 format/typecheck/build | 通过；仅有既有 bundle size warning |
| `git diff --check` | 通过；仅有既有换行转换提示 |
| Compose 全量测试 | `493 passed, 12 failed`；失败集中在既有 `tests/unit/test_structured_extraction_v2.py` 的抽取快照/文本 mock/安全上下文兼容断言，不属于本轮 FINAL_COMPARE V2 变更，未修改该业务链路 |

## 真实只读 dry-run

使用本地 DOCX 和数据库持久化 PDF OCR/page cache 重新构造 `FINAL_LOGICAL_V2` 比较：

- 旧报告差异：189
- V2 原始候选：186
- V2 规则后候选：186
- 逻辑单元统计：258
- 疑似重复簇：0
- 疑似候选：0
- PDF OCR cache：HIT
- DOCX page sidecar：HIT
- OCR 调用：0
- LLM 调用：0
- 数据库写入：0

历史报告安全结构核对显示，旧报告中的 10/16/5 组目标侧文本为空；最新持久化 OCR cache 的对应目标单元含实际文本或数值，不能按旧报告文本强行组成“双方标准化文本完全一致”的簇。为避免误删真实变化，当前 Canary 继续安全阻塞，未发送模型请求。

## 外部验收状态

- Canary：未执行，前置簇选择门禁未满足。
- 正式 FINAL_COMPARE：未创建。
- 三组串行任务：未创建。
- OCR/LLM 外部调用：0。
- 控制台报告：无新增地址；历史报告未修改。

## 未完成项

1. 需要业务确认当前最新 OCR cache 是否应作为正式 V2 输入；若要复现 10/16/5 组，必须先提供能证明同一逻辑区域且双侧文本一致的结构/跨度数据，不能从旧空文本结果推断。
2. Compose 全量测试仍有 12 个既有抽取链路失败；本轮按范围未修改 DRAFT_REVIEW/抽取兼容逻辑。
3. 在上述输入和全量回归门禁明确后，才能执行一次三簇 Canary及三组串行正式验收。
