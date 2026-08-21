# 任务进度：DRAFT_REVIEW 模板确定性比对与真实基线

## 基本信息

- 时间：2026-08-21 09:56:00 +08:00
- 状态：COMPLETED
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`fd1bdb4`
- 工作树状态：dirty；本阶段模板规则、工作流、控制台、LLM Schema、测试和本文尚未提交

## 用户目标

依据真实文件与 LLM 推进方案，在阶段 A 多文档解析基线上继续开发，先建立目标合同与模板的确定性比对真实基线，并为后续 LLM 单文档抽取准备严格结构化契约。

## 本次完成

- 将阶段 A 以 `fd1bdb4 feat(draft): add real multi-document parsing` 收口为本地提交，未 push。
- 新增模板专用确定性检查：允许填写项过滤、未替换占位符、空白标记和基础表格必填检查。
- 正文复用可靠 N:M 对齐；表格不再直接影响正文可靠性。仅同结构表格逐单元格检查，扩展填充表格返回人工复核 warning。
- 将同段落位置的占位符删除/填写新增安全归并为允许填写项，并保留过滤轨迹。
- DRAFT_REVIEW 升级为 `0.3.0 / RULE_BASED / mock=false`，按真实差异和规则结果生成 PASS、RISK_FOUND 或 REVIEW_REQUIRED。
- 控制台增加模板原始/保留/过滤统计和规则检查页签。
- 新增严格 `DocumentProfile`、`FactCandidate`、`DocumentFactExtraction` Schema；事实缺少文件、位置或证据文本时校验失败，尚未接入真实 LLM。
- 使用固定黄金样本中的 TARGET、TEMPLATE 和两份 DOCX REFERENCE 完成一次 API → Worker → 结果持久化闭环。
- fixture server 已停止并移除；API、Worker、PostgreSQL 已恢复为默认 Compose 服务。

## 修改文件

- `app/draft_review/template_checks.py`：模板确定性差异、填写区过滤和表格规则。
- `app/workflows/draft_review.py`：真实模板比对节点、0.3.0 结果构建和 warning 去重。
- `app/adapters/llm/schemas.py`：后续单文档事实抽取的严格 Pydantic Schema。
- `app/adapters/llm/__init__.py`、`app/adapters/llm/base.py`：Schema 导出和既有 Mock 格式整理。
- `frontend/src/api/types.ts`、`frontend/src/utils/labels.ts`、`frontend/src/views/TaskDetailView.vue`：模板诊断和规则展示。
- `tests/unit/test_draft_template_checks.py`、`tests/unit/test_llm_schemas.py`：模板规则和 LLM Schema 测试。
- `tests/unit/test_draft_review_workflow.py`、`tests/integration/test_worker.py`、`scripts/e2e_smoke.py`：0.3.0 回归断言。
- `README.md`：更新 DRAFT_REVIEW 当前能力与边界。

## 接口、数据和配置变化

- API：路由和请求结构不变；DRAFT_REVIEW 结果由 `PARSER_ONLY` 升级为向后兼容的 `RULE_BASED` 结构。
- 数据库/迁移：无模型或迁移变化；新增真实验收任务 `tsk_01M0H07WR7ZT27589G0Q3WDNJV`。
- 配置：无持久配置变化；真实验收只在 Compose 进程环境临时启用内部 fixture HTTP 和精确 allowlist，OCR/LLM 均关闭。
- 兼容性：`schema_version=1.0` 保持不变；metadata 新增模板诊断字段，现有字段保留。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 相关后端单元测试 | PASSED | 53 passed，1 个 LangGraph 上游 future warning |
| 最终变更定向测试 | PASSED | 13 passed，1 个同上 warning |
| Ruff 变更范围 | PASSED | All checks passed |
| Docker PostgreSQL 全量测试 | PASSED | 122 passed，1 个同上 warning |
| Vue typecheck / build | PASSED | 1459 modules；仅既有约 993.58 KiB chunk warning |
| Docker runtime/test 镜像构建 | PASSED | `contract-review-agent:dev`、`:test` 构建成功 |
| 真实 4 DOCX DRAFT 任务 | PASSED | 5.08 秒；SUCCEEDED / COMPLETED / 100；RULE_BASED 0.3.0 |
| 真实模板对齐 | PASSED | 双侧覆盖率 0.9771；reliable=true |
| 真实模板结果 | BASELINE | raw 48、filtered 22、retained 26、coalesced fill 9、expanded table 3、failed rule 0 |
| 临时目录与日志 | PASSED | 临时目录残留 0；API/Worker 日志未发现 Key、完整 fixture URL或合同文件名 |
| 黄金样本 SHA-256 | PASSED | 5/5 与既定记录一致；原件未修改 |
| API 重建后持久化 | PASSED | 验收任务仍为 SUCCEEDED / 100 |
| 浏览器视觉检查 | 未执行 | 内置浏览器不可用；HTTP 控制台为 200，视觉验收仍待人工确认 |
| `git diff --check` | PASSED | 无空白错误；仅 Windows 行尾提示 |

## Docker 与运行状态

- API：最新本地 runtime 镜像，healthy，`127.0.0.1:8000`。
- Worker：最新本地 runtime 镜像，running。
- PostgreSQL：healthy；命名卷保留，未删除或清空。
- 控制台：`/console/` HTTP 200；自动视觉检查未完成。
- 最终是否保持运行：是。

## 重要决策

- 模板填充表格与正式版本表格比对语义不同，不能让扩展表格拖低正文对齐可靠性。
- 只有行列结构完全一致的表格执行逐单元格固定内容比较；结构扩展时不猜测行列映射，统一提示人工复核。
- 只在同段落位置且模板骨架可匹配时归并 ADDED/DELETED 占位符填写，避免把真实固定条款变化误过滤。
- LLM 事实必须带来源文件、证据文本和位置；自由文本或缺证据结果不能进入正式事实矩阵。

## 已知问题与风险

- 真实基线仍保留 26 项差异，需要结合业务人工复核，进一步区分真实固定条款变化与复杂填写区；本次未通过放宽可靠性或批量忽略降低数量。
- 3 个扩展表格暂未做业务语义映射，只提供人工复核提示。
- 固定黄金集中的 PDF REFERENCE 仍受外部 OCR HTTP 502 阻塞，本次没有重复调用。
- LLM 仅完成 Schema 边界；OpenAI 兼容 Client、网关能力探测和真实单文件抽取尚未实现。
- 控制台视觉验收待人工完成；前端生产包仍有既有大 chunk 提示。

## 下一步建议

1. 人工抽查 26 项真实模板差异，形成“固定条款 / 允许填写区 / 表格人工复核”的标注快照，再收紧模板规则。
2. 实现 OpenAI 兼容 LLM Client 的超时、有限重试、安全错误映射和严格 Schema 重试测试，不立即发送真实合同。
3. 网关能力确认后，先对一份 DOCX 辅助资料执行一次真实 `DocumentProfile + FactCandidate` 抽取并人工核验。
4. OCR 服务恢复后仅重跑一次完整 5 文件黄金任务，再进入跨文档事实矩阵。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260821_draft-review-real-files-and-llm.md`
- `docs/progress/20260821-095600_draft-template-baseline.md`
- `app/draft_review/template_checks.py`
- `app/workflows/draft_review.py`
- `app/adapters/llm/schemas.py`

## 交接摘要

DRAFT_REVIEW 0.3.0 模板确定性切片已实现并通过真实 4 DOCX 闭环。
真实正文对齐 reliable=true，双侧覆盖率均为 97.71%。
48 项原始差异中过滤 22 项允许填写，保留 26 项复核差异；3 个扩展表格提示人工检查。
全量 Docker 测试 122 passed；前端类型和构建通过。
API、Worker、PostgreSQL 保持运行，验收任务重启后仍存在。
LLM 严格 Schema 已准备，尚未调用真实 LLM；下一步是 Client 离线测试和单文档纵向切片。
