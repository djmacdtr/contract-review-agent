# 任务进度：DRAFT_REVIEW 脱敏真实文件解析基线

## 基本信息

- 时间：2026-08-21 09:25:48 +08:00
- 状态：PARTIAL
- 任务类型：TEST / DIAGNOSE / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`901cbb8`
- 工作树状态：dirty；阶段 A 的业务代码和测试来自另一会话且尚未提交，本次未覆盖这些修改，仅新增计划/进度文档和数据库测试任务

## 用户目标

进入 DRAFT_REVIEW 下一阶段，固定使用用户指定的 5 份脱敏真实文件进行后续业务比对测试，并判断真实文件基线与 LLM 接入的合理先后顺序。

## 本次完成

- 将 1 份目标合同、1 份模板和 3 份辅助资料登记为固定黄金样本集，记录文件大小和 SHA-256。
- 创建完整 5 文件真实 `DRAFT_REVIEW` 解析任务；下载成功后在 PDF 外部解析阶段失败。
- 使用同一 PDF 对外部解析器执行一次 `scan` 模式最小诊断，仍返回 `OCR_SERVICE_UNAVAILABLE`。
- 对甲方 OCR 任务列表接口执行无文件健康探测，返回 HTTP 502，确认当前失败来自上游服务不可用，不能归因于合同文件或 `auto/scan` 模式。
- 没有连续重跑 OCR；改用同一黄金样本中的 4 份 DOCX 完成真实解析基线。
- 4 份 DOCX 均完成解析，结果为 `PARSER_ONLY / REVIEW_REQUIRED`，没有调用 LLM。
- 每次临时测试结束后均恢复下载白名单、OCR 开关和运行服务配置，停止只读 fixture server。

## 修改文件

- `docs/plans/20260821_draft-review-real-files-and-llm.md`：固定样本集与后续阶段顺序。
- `docs/progress/20260821-091834_real-draft-testset-decision.md`：样本和架构决策记录。
- `docs/progress/20260821-092548_real-draft-parse-baseline.md`：本次真实测试与诊断记录。
- 未修改合同原件、业务代码、配置文件、数据库结构或 Docker 定义。

## 接口、数据和配置变化

- API：无公开接口变化；通过既有 `/api/v1/draft-reviews` 创建测试任务。
- 数据库/迁移：无迁移；新增两条测试任务及其事件、文件元数据，其中一条失败、一条成功。
- 配置：只在测试进程中临时覆盖精确下载白名单、OCR 开关和辅助文件上限，测试后恢复。
- 兼容性：无变化。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 5 文件真实解析任务 | FAILED（外部阻塞） | 任务 `tsk_01M0GYKA7YTMBS1FQZDM3ECDSV`；`PARSING / 45%`；`OCR_SERVICE_UNAVAILABLE` |
| 同一 PDF `scan` 模式直接诊断 | FAILED（外部阻塞） | 外部解析服务仍返回 `OCR_SERVICE_UNAVAILABLE` |
| OCR 无文件健康探测 | FAILED（外部阻塞） | HTTP 502，约 5044 ms |
| 4 DOCX 真实解析任务 | PASSED | 任务 `tsk_01M0GYTY1N9C7XPM2QQ8CQE9KE`；4 文件；约 7.2 秒；`PARSER_ONLY / REVIEW_REQUIRED` |
| 合同 TARGET 解析 | PASSED WITH WARNING | 276 blocks、4 tables；`DOCX_MERGED_CELLS_SIMPLIFIED` |
| 合同 TEMPLATE 解析 | PASSED WITH WARNING | 276 blocks、4 tables；3 个 `DOCX_MERGED_CELLS_SIMPLIFIED` |
| 法律合规报告解析 | PASSED | 80 blocks、1 table、0 warning |
| 项目方案确认函解析 | PASSED WITH WARNING | 10 blocks、1 table；`DOCX_MERGED_CELLS_SIMPLIFIED` |
| LLM | 未执行 | 当前阶段不应使用 LLM 掩盖 OCR 或解析问题 |

## Docker 与运行状态

- API：测试后按原配置重建并恢复，最终 healthy。
- Worker：测试后按原配置重建并保持 running。
- PostgreSQL：healthy，命名卷未修改或清空。
- fixture server：已停止。
- 控制台：沿用 API 静态资源，服务保持运行。
- 最终是否保持运行：是。

## 重要决策

- 完整 5 文件基线暂时被甲方 OCR HTTP 502 阻塞，不修改业务代码猜测性绕过，不连续调用。
- 4 DOCX 真实解析已经证明阶段 A 的下载、任务、DOCX 解析和结果持久化闭环可用。
- 模板确定性比对只依赖 TARGET/TEMPLATE DOCX，可以立即进入阶段 B，不需要等待 OCR 或 LLM。
- LLM Client/Schema 可以开始开发，但真实交叉比对验收必须等 PDF 解析恢复；不能把 LLM 当 OCR 降级方案。

## 已知问题与风险

- 甲方 OCR 当前健康探测为 HTTP 502，完整黄金样本尚未成功闭环。
- DOCX 合并单元格当前被简化，阶段 B 实现表格规则时必须评估它是否影响证据定位和必填判断。
- 当前只有解析结果，没有模板差异、占位符、事实抽取、事实矩阵或建议。
- 阶段 A 工作树仍未提交，真实任务可复现性依赖先收口当前修改。

## 下一步建议

1. 收口并提交阶段 A 代码及本次记录。
2. 直接进入阶段 B：使用真实 TARGET/TEMPLATE 完成条款、占位符、空白和表格的确定性基线。
3. 同步定义 LLM Client、`DocumentProfile` 和 `FactCandidate` Schema，但先用 Mock/错误响应测试。
4. OCR 恢复后只重跑一次完整 5 文件任务；成功后再做真实 LLM 单文档抽取和事实矩阵。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/plans/20260821_draft-review-real-files-and-llm.md`
- `docs/progress/20260821-090856_draft-review-parse-slice.md`
- `docs/progress/20260821-092548_real-draft-parse-baseline.md`
- `app/workflows/draft_review.py`
- `app/comparison/engine.py`
- `app/adapters/llm/base.py`

## 交接摘要

固定 5 文件黄金样本已经登记。
完整任务因甲方 OCR HTTP 502 在 PDF 解析阶段失败，没有连续重试。
同一集合中的 4 份 DOCX 已真实闭环：4/4 解析成功，约 7.2 秒。
目标合同与模板均为 276 blocks、4 tables，可立即进入模板确定性比对。
下一阶段先做 TARGET/TEMPLATE 规则基线，同时准备 LLM Schema/Client。
OCR 恢复后再重跑一次完整 5 文件任务，然后进入真实 LLM 事实抽取。
