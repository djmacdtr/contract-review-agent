# 任务进度：DOCX/PDF 文档解析路由建议

## 基本信息

- 时间：2026-08-20 15:12:01 +08:00
- 状态：COMPLETED
- 任务类型：REVIEW / DIAGNOSE / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-ocr`
- 当前提交：`43c2c82`
- 工作树状态：dirty；开始时已有未提交的差异爆炸诊断记录，本次仅新增本记录

## 用户目标

评估是否应放弃 `pdfplumber` 并统一使用甲方 OCR，结合真实主场景 DOCX 与盖章扫描 PDF 的比对，以及起草阶段 DOCX 和辅助 PDF 的处理，确定合理的解析路由。

## 本次完成

- 核对甲方文档解析 OpenAPI 对 PDF 解析模式、结构返回和同步接口文件类型的说明。
- 结合 46 页 PDF 差异爆炸诊断，评估同一外部解析器对 PDF/PDF 的降噪收益。
- 对本地脱敏 DOCX 样本做只读结构计数，不输出正文。
- 形成“所有 PDF 统一甲方文档解析、DOCX 保持原生解析、统一中间表示”的建议。

## 修改文件

- `docs/progress/20260820-151201_document-parser-routing-decision.md`：新增解析路由决策记录。
- 本次未修改业务代码、接口、数据库、配置或 Docker 服务。

## 文档依据

- 甲方同步文档解析接口提供 PDF `parse_mode=auto/scan`：
  - `auto`：综合文字层解析和 OCR；
  - `scan`：仅按文字识别方式处理。
- 返回结构统一包含段落、表格、页码、坐标和可选 OCR 明细。
- 同步接口的说明、分页参数及结果 `document_type` 明确围绕 PDF/图片；虽然完整 OpenAPI 的其他业务 Schema 出现 `docx/doc`，但不能据此确认当前同步解析端点可直接处理 DOCX，需另行实测或向甲方确认。

## 样本结构对照

| 输入 | 解析方式 | 结构规模 |
|---|---|---:|
| 46 页原版 PDF | `pdfplumber` 逐换行 | 1,875 个短行块，无结构化表格 |
| 46 页扫描 PDF | 甲方解析/OCR | 383 个语义块、4 张表、2,189 个单元格 |
| 本地融资租赁 DOCX 样本 | `python-docx` | 272 个非空段落、4 张表、2,682 个单元格 |

DOCX 原生结构与 OCR 输出在“段落 + 4 张表”的形态上明显比 `pdfplumber` 物理短行更接近，因此真实的 DOCX→扫描 PDF 场景预计不会复现同等规模的 1,688 个短行删除，但合并单元格、段落切分和 OCR 文本差异仍需统一表示与 N:M 对齐。

## 结论

### 可以放弃 `pdfplumber` 作为正式 PDF 比对解析器

- 对 PDF/PDF 场景，把双方都送入甲方文档解析服务，使用同一参数和同一输出 mapper，能显著减少本次由异构结构造成的差异爆炸。
- 对普通文本 PDF 可使用 `auto`，对扫描 PDF 使用 `scan`；若 PDF/PDF 严格比对追求最大结构一致性，可对一组文件采用相同模式做实验后固定策略。
- `pdfplumber` 可保留为开发诊断、文本密度探测或甲方服务不可用时的受限 fallback，但 fallback 结果不得与 OCR 结构直接进行逐块精细比较。

### 不建议所有格式统一强制 OCR

- DOCX 原文已经包含段落、标题、表格、合并单元格和占位符等高质量结构，转图片再 OCR 会丢失确定性信息并引入识别错误。
- 甲方同步解析端点能否直接接受 DOCX 尚未被当前文档明确证明；即使支持，也应先比较结构质量、耗时和错误率。
- “统一”应落实为统一的 `ComparableDocument/ComparableUnit` 中间模型和统一对齐算法，而不是所有文件必须经过同一个物理解析器。

## 推荐路由矩阵

| 业务场景 | 文件 | 推荐解析 |
|---|---|---|
| 放款阶段 | 原合同 DOCX | `python-docx` 原生段落/表格解析 |
| 放款阶段 | 盖章扫描 PDF | 甲方文档解析 `scan` |
| 放款阶段偶发 PDF/PDF | 两个 PDF | 两侧都走甲方文档解析；优先验证统一 `auto`，必要时统一 `scan` |
| 起草阶段 | 模板 DOCX、待审 DOCX | `python-docx` 原生解析 |
| 起草阶段 | 辅助 PDF | 甲方文档解析；文本/扫描均可优先 `auto`，以事实抽取为主 |
| 旧 DOC | DOC | 先做受控格式转换或确认甲方端点能力，不直接套用 DOCX 解析器 |

## 仍然必须实现的统一层

- 比较专用文本规范化：空格、换行、全半角、页眉页脚和 OCR 标点噪声。
- DOCX 段落与 OCR 段落的 N:M 对齐，不能继续依赖完整块精确相等。
- 表格合并单元格与空单元格的规范化；只有双方结构兼容时才做 row/cell 级比较。
- 差异爆炸保护与 `ALIGNMENT_UNRELIABLE` 降级。
- 金额、日期、比例、主体和条款删除的独立确定性校验。

## 验证建议

1. 用当前 46 页 PDF 负样本做“甲方解析 auto→scan / scan→scan”小型对照实验，只统计结构和最终差异，不输出正文。
2. 用真实对应的 DOCX 与扫描 PDF 建立主黄金集；当前 DOCX 样本结构规模虽接近，但必须确认文件确为扫描件的源版本。
3. 验收不只看差异数量，还要加入受控金额、日期、主体和条款修改正样本，避免降噪后漏检。

## Docker 与运行状态

- 本次未重建、重启或停止 Docker 服务。
- 服务保持上一任务状态运行。

## 已知问题与风险

- 当前甲方 Adapter 硬编码 `parse_mode=scan`，尚未支持按文件或任务策略选择 `auto/scan`。
- 两个 PDF 都走 OCR 会增加调用耗时；46 页单文件约 43 秒，当前 Worker 串行处理两份可能接近翻倍。
- 同一供应商并不保证两个输入绝对相同分段，仍需要统一中间表示和鲁棒对齐。
- DOCX 的 2,682 个单元格与 OCR 的 2,189 个单元格仍有差距，说明合并单元格和空白单元格规范化是必要工作。

## 下一步建议

1. 将正式 PDF 路由切换为甲方文档解析服务，并引入可配置 `auto/scan` 策略；保留 `pdfplumber` 仅作受限 fallback/诊断。
2. 优先以真实 DOCX→扫描 PDF 建立黄金集，实施统一中间表示、N:M 段落对齐和表格兼容门控。
3. 在决定 PDF/PDF 的最终模式前，对当前 46 页负样本做同解析器对照试验。

## 下一会话首先阅读

- `docs/progress/20260820-150430_diff-explosion-diagnosis.md`
- `docs/progress/20260820-151201_document-parser-routing-decision.md`
- `D:\work\contract_review\OCR文档\openapi.json`
- `app/documents/parsers.py`
- `app/documents/router.py`
- `app/adapters/document_parser/textin_client.py`
- `app/comparison/engine.py`

## 交接摘要

统一使用甲方解析服务能明显改善 PDF/PDF 的结构错位，但不应把 DOCX 转图片强制 OCR。
推荐所有正式 PDF 走甲方文档解析，DOCX 保持 `python-docx` 原生解析，二者统一到可比较中间模型。
真实主场景应是 DOCX 段落/表格对扫描 PDF OCR 段落/表格，并配合 N:M 对齐和表格门控。
本地 DOCX 样本为 272 个非空段落、4 张表，结构上比 1,875 个 PDF 物理短行更接近 OCR 的 383 个块、4 张表。
甲方同步端点是否直接支持 DOCX 尚需确认，当前文档只明确 PDF/图片。
