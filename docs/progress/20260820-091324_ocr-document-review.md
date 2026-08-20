# 任务进度：甲方 OCR / 文档解析接口审阅

## 基本信息

- 时间：2026-08-20 09:13:24 +08:00
- 状态：COMPLETED
- 任务类型：REVIEW / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`main`
- 当前提交：`c7b7490`
- 工作树状态：dirty；仅新增 OCR 接入计划和本进度记录，业务代码未修改

## 用户目标

阅读 `D:\work\contract_review\OCR文档` 中甲方提供的 OCR 文档，结合现有代码、方案和进度记录，明确下一阶段实施方向。

## 本次完成

- 完整定位并解析甲方 `openapi.json` 的 12 个文档解析 API path。
- 对照同步解析、异步任务、结果、重试、删除、图片和导出接口。
- 核对页、段落、表格、单元格、坐标、旋转角度、OCR 行/字符置信度和错误码模型。
- 对照现有 `OcrAdapter`、`ParsedDocument`、ParserRegistry、FINAL_COMPARE Graph、Worker 和 task_file 元数据。
- 确认现有 `OcrResult(pages)` 过窄，建议改为供应商无关外部文档解析 Adapter，并统一映射到 `ParsedDocument`。
- 确定下一里程碑为 FINAL_COMPARE 扫描 PDF OCR 真实纵向切片；首版建议同步引擎、本地解析优先、仅在 `OCR_REQUIRED` 时回退。
- 形成详细接入计划、测试矩阵、错误映射、联调确认项和下一会话启动语。

## 修改文件

- `docs/plans/20260820_ocr-document-parser-integration.md`：新增 OCR/外部文档解析接入方案。
- `docs/progress/20260820-091324_ocr-document-review.md`：新增本次审阅交接记录。

## 接口、数据和配置变化

- API：无项目 API 变化。
- 数据库/迁移：无。
- 配置：无实际配置变化；计划建议后续增加可配置鉴权 header、Base URL、同步参数和重试边界。
- 兼容性：无运行时变化；当前扫描 PDF 仍返回 `OCR_REQUIRED`。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `rg --files | rg 'OCR|ocr'` | 通过 | 找到 HTML、OpenAPI JSON 和现有 OCR Adapter |
| OpenAPI JSON 解析 | 通过 | 3.0.3，12 个 parser path；因 schema 中存在大小写重复 key，使用 Hashtable 解析 |
| 关键 schema 审阅 | 通过 | ParseResult、pages、detail、textline、raw OCR、table/cells、status 和 error code |
| 当前代码边界对照 | 通过 | 确认 ParserRegistry 仅支持 DOCX/文本 PDF，OCR stage 已预留但未接入 |
| OCR 原始资料 SHA-256 | 通过 | `openapi.json` 为 `EA29FFD...CEA06`；HTML 为 `8C6E5C...615A3`，原件未修改 |
| Git 状态 | 已检查 | 开始时 clean；结束时只有两份新增 Markdown |
| 自动化测试 | 未运行 | 本任务只新增方案/进度 Markdown，没有修改运行代码 |

## Docker 与运行状态

- API：本任务未检查、未停止、未重启。
- Worker：本任务未检查、未停止、未重启。
- PostgreSQL：本任务未改变。
- 控制台：本任务未改变。
- 最终是否保持运行：本任务没有执行任何 Docker 状态变更。

## 重要决策

- 把甲方服务视为“外部文档解析器”，不把供应商响应泄漏到业务比对层。
- 首版使用同步 engine；外层任务已经异步，可避免立即增加第三方任务 ID 持久化和恢复状态机。
- 文本 PDF 继续本地解析，扫描/低文本 PDF 才调用外部服务。
- DOCX 继续使用 python-docx；旧版 DOC 是否使用甲方服务需真实联调后决定。
- 先完成 OCR，再推进 DRAFT_REVIEW 真实工作流和 LLM 事实抽取。

## 已知问题与风险

- 文档鉴权说明与 OpenAPI security schemes 不一致，真实 header 未确认。
- 实际 Base URL、请求大小、QPS、同步超时、数据留存和可重试错误未确认。
- 文档中包含默认管理凭证说明，严禁复制进仓库；应要求甲方确认已修改。
- OpenAPI 部分 required 字段与 properties 不一致，不能直接无审查生成强类型客户端。
- 对 200 页扫描 PDF 使用同步接口是否稳定，需要 Worker 容器内真实压测。
- 当前 `DocumentLocation.bbox` 为字符串数组，与 OCR 数字坐标不一致，实现时需要修正。

## 下一步建议

1. 向甲方确认实际 Base URL、鉴权 header/API Key、200 页限制、并发/QPS、数据留存和真实脱敏响应。
2. 不等待真实凭证，先按计划用 MockTransport 和脱敏响应 fixture 实现客户端、DTO、映射和 OCR_REQUIRED 回退。
3. 从 Worker 容器做 1/20/200 页联调，根据耗时和失败语义决定是否切换供应商异步 API。
4. 建立扫描 PDF + OCR 差异黄金集，再推进 DRAFT_REVIEW 和 LLM。

## 下一会话首先阅读

- `AGENTS.md`
- `README.md`
- `docs/plans/20260820_ocr-document-parser-integration.md`
- `docs/progress/20260820-091324_ocr-document-review.md`
- `docs/progress/20260819-161849_final-compare-slice.md`
- `app/documents/models.py`
- `app/documents/parsers.py`
- `app/workflows/final_compare.py`
- `app/adapters/ocr/base.py`

## 交接摘要

甲方文档解析 API 能返回页、段落、表格、坐标和 OCR 置信度，不是简单文字接口。
现有 OcrResult(pages) 需要升级为外部文档解析 Adapter，并映射到 ParsedDocument。
下一里程碑建议只做 FINAL_COMPARE 扫描 PDF 真实 OCR 纵向切片。
首版使用同步 engine，本地 DOCX/文本 PDF 路径不变，仅 OCR_REQUIRED 时回退。
真实联调前仍需甲方确认 Base URL、鉴权、限流、数据留存和 200 页稳定性。
本任务未修改业务代码、数据库或 Docker 状态，OCR 原始资料未修改。
