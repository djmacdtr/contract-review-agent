# 任务进度：DRAFT_REVIEW 下一阶段走向审查

## 基本信息

- 时间：2026-08-21 10:18:21 +08:00
- 状态：COMPLETED
- 任务类型：REVIEW / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`fd1bdb4`
- 工作树状态：dirty；阶段 B 模板规则、LLM Schema、前端、测试和上一份进度尚未提交，本次未修改这些文件，仅新增本记录

## 用户目标

基于阶段 A 已提交、阶段 B 已实现但未提交的完成情况，分析 26 项真实模板差异、LLM Client 和首次真实单文档事实抽取之间的合理推进顺序。

## 本次完成

- 核对阶段 B 交接、当前 Git 状态和真实任务 `tsk_01M0H07WR7ZT27589G0Q3WDNJV` 的非正文结果结构。
- 确认 26 项保留差异分布为：7 `NUMERIC_CHANGED`、5 `MODIFIED`、5 `ADDED`、5 `DELETED`、4 `TABLE_CELL_CHANGED`。
- 确认机器优先级为 15 HIGH、11 MEDIUM，但当前 `risk_items=0`，因此不能将机器优先级解释为已确认合同风险。
- 确认 26 项全部 `requires_manual_review=true` 且 `review_reason=null`，下一规则版本需要补充可解释原因码。
- 确认除 26 项差异外还有 3 个扩展表格人工复核点；黄金标注范围应包含两者。
- 抽查差异结构，发现新增/删除配对、跨行重排和模板占位符填值特征，预计仍存在可通过确定性规则消除的误报；不建议用 LLM 直接过滤这些差异。
- 核对严格 `DocumentProfile`、`FactCandidate`、`DocumentFactExtraction` Schema 已存在，但真实 Client 和工作流接线尚未实现。
- 建议首次真实 LLM 文档优先选择 `项目方案确认函.docx`；它仅 10 个块、1 张表，调用范围小且便于人工核验。调用前必须确认合并单元格简化没有破坏事实关系，否则改用 0 warning 的法律合规报告。

## 修改文件

- `docs/progress/20260821-101821_draft-next-direction-review.md`：新增本次只读审查和下一阶段建议。
- 未修改业务代码、配置、数据库、Docker 服务、合同文件或既有未提交修改。

## 接口、数据和配置变化

- API：无变化。
- 数据库/迁移：无变化。
- 配置：无变化。
- 兼容性：无变化。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| Git 状态与提交核对 | 通过 | `fd1bdb4`；阶段 B dirty、未推送 |
| 阶段 B 交接记录核对 | 通过 | 122 passed；真实任务 26 retained、3 expanded tables |
| 真实任务结果结构查询 | 通过 | 26 diff、0 risk、0 rule check、8 warning |
| 差异类型统计 | 通过 | numeric 7、modified 5、added 5、deleted 5、table 4 |
| 差异解释性核对 | 发现缺口 | 26/26 `review_reason=null` |
| 新测试/真实 LLM | 未执行 | 本次为只读方向审查 |

## Docker 与运行状态

- 本次未改变服务状态。
- 沿用阶段 B 交接：API healthy、Worker running、PostgreSQL healthy、控制台 HTTP 200。
- 最终是否保持运行：是，未执行启动、停止或重建。

## 重要决策

- 下一验收门是“模板差异黄金标注”，不是直接调用 LLM。
- 标注对象应包括 26 项保留差异和 3 个扩展表格复核点。
- LLM 不参与固定条款差异的真伪判定；这一层继续使用人工真值和确定性规则回归。
- OpenAI 兼容 Client 的离线实现可在标注后立即进行，真实调用前先为选定辅助资料建立预期事实清单。
- 首次真实 LLM 只做单文档 `DocumentProfile + FactCandidate`，不生成法律建议、不构建全量事实矩阵。

## 已知问题与风险

- 当前 15 HIGH、11 MEDIUM 是算法优先级，不是已确认风险，控制台文案需要避免误导。
- 26 项均缺少 `review_reason`，当前结果无法说明为何保留人工复核。
- 当前 `ContractLlmClient` 返回非泛型 `dict`，Mock 输出与完整 `DocumentFactExtraction` 契约尚未完全统一；实现真实 Client 前应先收紧类型边界。
- `项目方案确认函.docx` 有合并单元格简化 warning，真实 LLM 调用前必须检查解析块是否仍保留字段和值关系。
- OCR HTTP 502 仍阻塞 PDF 辅助资料和完整 5 文件事实矩阵。

## 下一步建议

1. 将阶段 B 当前通过测试的状态先形成独立本地提交，避免与 LLM Adapter 修改混在同一提交。
2. 导出并人工标注 26 项差异和 3 个扩展表格，标签至少包括：真实固定条款变化、允许填写、对齐误报、表格填写、需人工判断。
3. 把确认后的标注保存为不含合同全文的黄金快照，使用位置、类型、文本摘要哈希和预期保留状态建立回归测试。
4. 根据标注只收紧确定性过滤规则，目标是确认的真实固定条款变化 100% 保留，允许填写/对齐误报不进入风险项。
5. 独立实现 OpenAI 兼容 Client：配置、超时、有限重试、HTTP 错误映射、JSON 提取、严格 Pydantic 校验、结构重试和调用元数据；全部先用 MockTransport 测试。
6. 人工确认 `项目方案确认函.docx` 的解析结构并建立 5–10 个预期事实后，执行一次真实单文档抽取。
7. 单文档达到事实和值准确、证据位置正确、无证据新增事实为 0 后，再抽取目标合同并构建最小两文档事实矩阵。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260821-095600_draft-template-baseline.md`
- `docs/progress/20260821-101821_draft-next-direction-review.md`
- `app/draft_review/template_checks.py`
- `app/adapters/llm/base.py`
- `app/adapters/llm/schemas.py`

## 交接摘要

阶段 B 技术闭环成立，但 26 项是待确认差异，不等于 26 个真实风险。
标注范围还应包含 3 个扩展表格复核点。
新增/删除配对和占位符填写特征表明仍有确定性误报优化空间。
先提交阶段 B，再形成黄金标注和规则回归，之后实现 LLM Client 离线切片。
首次真实 LLM 优先使用小型 `项目方案确认函.docx`，调用前先检查其表格解析关系。
LLM 首次只做有证据的分类和事实抽取，不做建议或全量跨文档判断。
