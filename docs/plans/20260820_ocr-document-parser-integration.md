# 外部 OCR / 文档解析服务接入计划

> 计划日期：2026-08-20
> 计划状态：READY
> 目标：让扫描型 PDF 通过甲方文档解析服务生成可追溯 `ParsedDocument`，继续复用现有 FINAL_COMPARE 确定性比对链路

## 1. 结论

下一里程碑优先实现 **FINAL_COMPARE 扫描 PDF 真实解析纵向切片**，不同时展开真实 LLM、DRAFT_REVIEW 全量业务规则或旧版 DOC 支持。

甲方提供的不是简单“每页 OCR 文本”接口，而是一套文档解析服务，能够返回页、段落、表格、单元格、坐标、旋转角度、OCR 置信度、目录和 Markdown。现有 `OcrResult(pages)` 协议无法承载这些信息，应将其抽象为外部文档解析器，再将结果统一转换为项目已有 `ParsedDocument`。

首版建议使用同步解析接口。项目外部 API 和 Worker 已经异步化，同步调用能最快完成纵向闭环，同时避免马上增加第三方任务 ID 持久化、轮询恢复和数据库迁移。客户端接口仍应隔离同步/异步实现，待真实大文档压测后再决定是否切换甲方异步任务接口。

## 2. 文档已确认能力

- OpenAPI：3.0.3；产品标识为 TextIn Document Master v3.0.0。
- 同步入口：`POST /api/contracts/v3/parser/external/engine`，请求体为 `application/octet-stream`。
- 异步入口：任务创建、列表、结果、重试和删除接口；创建请求为 `multipart/form-data`，可以包含多个文件。
- 异步状态：等待、解析中、成功、额度不足、失败。
- 结果层级：文档、页、文字行、段落块、表格、单元格、图像块、目录、Markdown。
- 定位信息：页码、段落 ID、表格行列、四点坐标、页面宽高、DPI 和旋转角度。
- 置信信息：文字行分数；可选字符分数、字符位置和候选字符。
- 表格信息：行列数、跨行跨列、单元格文本和坐标。
- 文档说明给出的 PDF 页数上限为 1000 页，覆盖当前业务中最多约 200 页的范围，但仍需真实环境确认请求体和超时限制。
- 支持综合解析和纯扫描识别模式；支持段落/表格合并、目录、去水印、切边、图表和图片输出等可选能力。
- 文件类型枚举包含 DOC、DOCX、PDF、图片及部分表格/文本格式，但各格式的真实解析质量尚未联调。

## 3. 文档中仍存在的接口风险

1. 鉴权说明要求 `x-api-key`，OpenAPI security schemes 又列出多个不同 header，具体 parser path 没有绑定 security；必须由甲方确认真实部署使用的 header。
2. server 模板列出多个主机、端口和 basePath，不能据此猜测生产地址；需要取得 Worker 容器可访问的实际 Base URL。
3. 文档包含默认管理凭证说明，不得将该凭证复制到代码、`.env.example`、进度文件或 Git；应要求甲方确认已修改默认凭证。
4. OpenAPI 的部分 schema required 字段与 properties 不一致，客户端不能依赖自动生成代码直接无校验使用。
5. 没有明确最大文件字节数、QPS、并发、同步请求推荐超时、任务保存时间和服务端重试建议。
6. 没有确认上传文档、解析文本和页面图片是否被服务端持久化、保留多久以及谁可以访问。
7. 错误码给出了处理中、任务失败、额度不足和部分失败，但“哪些错误可安全重试”仍需联调确认。

## 4. 推荐架构

```text
URL 文件
  → 现有受控下载器
  → 本地 ParserRegistry
      ├─ DOCX：python-docx
      ├─ 文本型 PDF：pdfplumber
      └─ 扫描/低文本 PDF：ExternalDocumentParserClient
             → 甲方文档解析 API
             → ExternalParseResultMapper
  → ParsedDocument
  → 现有文字/数值/表格比对
  → 结构化 JSON / 控制台
```

原则：

- 受控 URL 下载继续由本项目完成，甲方解析服务只接收本地临时文件字节，不让其二次访问用户 URL。
- 文本 PDF 继续本地解析，只有 `OCR_REQUIRED` 才调用外部服务，减少成本、延迟和数据暴露。
- DOCX 继续使用当前本地解析器，首版不因外部服务存在而替换已验证路径。
- 外部结果必须映射为统一 `ParsedDocument`；比对规则不能直接依赖供应商 JSON。
- 甲方原始响应不进入日志；正式结果只保存必要证据、位置、置信摘要和安全元数据。

## 5. 代码边界

建议新增：

- `app/adapters/document_parser/base.py`：供应商无关的外部解析协议和 DTO。
- `app/adapters/document_parser/textin_client.py`：HTTP、认证、超时、错误映射。
- `app/adapters/document_parser/textin_models.py`：只声明项目实际读取的响应字段，允许未知字段。
- `app/adapters/document_parser/textin_mapper.py`：供应商响应到 `ParsedDocument` 的纯函数映射。
- `app/documents/router.py`：本地解析优先、扫描 PDF 外部回退策略。
- `tests/fixtures/ocr/`：脱敏、裁剪过的响应 JSON，不保存真实合同全文。

现有 `app/adapters/ocr/base.py` 可保留兼容层，但不应继续把 `pages: tuple[str, ...]` 作为真实集成的数据模型。

## 6. 首版同步调用参数

建议初始配置：

- `parse_mode=scan`：仅用于本地文本层不足的扫描 PDF。
- `page_details=1`：保留逐页结构和状态。
- `markdown_details=1`：获取段落、表格和位置摘要。
- `table_flavor=html`：开启表格识别，但映射时优先使用结构化 cells，不解析 HTML 作为真值。
- `get_image=none`：当前不需要页面或对象图片，避免巨大响应和持久化风险。
- `get_excel=0`：不生成无用的 Base64 Excel。
- `raw_ocr=1`：保留文字行分数，支持低置信度警告。
- `char_details=0`：首版控制 200 页响应体积；后续可对疑似数字页增加可配置精细模式。
- `apply_document_tree=1`、`apply_merge=1`：保留标题结构并合并跨页段落/表格。
- `remove_watermark=0`、`crop_dewarp=0`：默认不做可能改变证据图像的处理，待样本评测后开启。

首版不支持向 OCR 服务传 PDF 密码。加密 PDF 返回稳定错误码，避免密码进入任务 JSON 或日志。

## 7. 统一模型映射

| 甲方结果 | 项目模型 |
|---|---|
| `result.detail[].page_id` | `DocumentLocation.page` |
| `paragraph_id` | `DocumentLocation.paragraph_index` |
| `position` | `DocumentLocation.bbox` |
| paragraph / title | `DocumentBlock(type="PARAGRAPH")` |
| header / footer | `DocumentBlock(type="HEADER"/"FOOTER")` |
| table + cells | `DocumentBlock(type="TABLE")` + `ParsedTable` |
| cell row/col/span/text | `TableRow` / `TableCell`；span 进入 parser metadata 或 warning |
| OCR line score | block/page 置信摘要和低置信 warning |
| page angle | parser metadata；非 0 页增加旋转 warning |
| engine version / duration | `ParsedDocument.parser_metadata` |

实现时应将当前 `DocumentLocation.bbox: list[str]` 修正为数字坐标类型。跨页连续段落和表格先保留页面证据并合并逻辑文本；无法可靠合并时输出 warning，不猜测。

## 8. OCR 感知差异策略

- OCR 文本仍参与确定性比对，但差异项必须携带 OCR 来源和人工复核标记。
- 低置信度文本上的普通文字差异降级为解析警告或 `REVIEW_REQUIRED`。
- 数字、金额、日期、比例即使来自低置信 OCR 也不能静默忽略；应输出差异，同时明确“可能由 OCR 造成”。
- 字符候选未开启时不能自行猜测正确字符。
- 某页解析失败、部分任务失败或表格结构不完整时，首版任务失败，不返回看似完整的 PASS。
- 最终 `RULE_BASED_LIMITATION` 文案需要改为“包含 OCR、未包含 LLM 或法律判断”。

## 9. 错误映射建议

| 场景 | 项目错误 | 是否重试 |
|---|---|---|
| OCR 未启用/未配置 | `OCR_NOT_CONFIGURED` | 否 |
| 401/403 | `OCR_AUTH_FAILED` | 否 |
| 参数错误 | `OCR_REQUEST_INVALID` | 否 |
| 额度不足 | `OCR_QUOTA_EXCEEDED` | 否 |
| 连接失败/超时/5xx | `OCR_SERVICE_UNAVAILABLE` | 是，有限退避 |
| 响应 JSON/schema 不符合约定 | `OCR_RESPONSE_INVALID` | 首版否 |
| 部分页失败 | `OCR_PARTIAL_FAILURE` | 按整体失败处理 |
| 文档无法识别 | `OCR_PARSE_FAILED` | 视联调错误原因决定 |

HTTP 重试和 Worker 整体任务重试要避免重复放大。同步接口可在 Adapter 内仅对连接失败、502/503/504做少量退避；业务错误不重试。

## 10. 测试顺序

### 10.1 不依赖甲方环境

1. 根据 OpenAPI 建立最小请求/响应 DTO。
2. 使用 `httpx.MockTransport` 测试 header、参数、二进制请求、超时和错误映射。
3. 使用脱敏响应 fixture 测试段落、页码、坐标、表格单元格和置信度映射。
4. 测试文本 PDF 不调用 OCR、扫描 PDF 调用 OCR、OCR disabled 保持 `OCR_REQUIRED`。
5. 测试低置信数字、旋转页、跨页表格、部分页失败、无 detail、超大响应和临时目录清理。
6. 回归当前全部测试以及 FINAL_COMPARE DOCX 闭环。

### 10.2 甲方环境联调

1. 从 Worker 容器验证 DNS、端口、TLS/证书和鉴权，不只验证宿主机。
2. 依次使用 1 页、20 页、200 页脱敏扫描 PDF。
3. 记录请求字节、响应字节、P50/P95、服务端 duration、超时和峰值内存。
4. 对同一文件重复请求，确认幂等、计费和限流行为。
5. 验证表格、旋转、低清晰度、水印、手写和部分页失败。
6. 根据结果决定继续同步模式，还是切换异步任务创建/结果轮询。

## 11. 同步切换异步的判定

满足任一条件时进入第二阶段异步 Provider 实现：

- 50～200 页文件经常超过网关或反向代理同步超时；
- 服务端明确要求大文件使用异步任务；
- 同步调用失败后无法确认任务是否已经执行/计费；
- 需要 Worker 重启后继续同一 OCR 任务而不是重新上传；
- OCR 单任务长时间占用唯一 Worker 并明显阻塞队列。

异步实现需要持久化供应商 task ID、状态和提交参数。届时再设计数据库迁移，避免首版提前增加不确定字段。

## 12. 实现前必须向甲方确认

1. 实际 Base URL、端口、basePath 和 Worker 容器网络策略。
2. Parser API 的真实鉴权 header、API Key 申请/轮换方式。
3. 文档中默认管理凭证是否已修改；项目不得使用默认管理账号调用业务接口。
4. 同步接口对 200 页 PDF 的最大字节、推荐超时、并发和 QPS。
5. 异步任务保存时间、轮询频率、结果删除责任和重复提交计费规则。
6. 上传原文、解析文本、页面图片是否保存，保留周期和访问控制。
7. 生产部署的引擎/API 版本是否与这份 v3.0.0 OpenAPI 一致。
8. 典型成功、低置信、部分失败、额度不足的真实脱敏响应样例。
9. DOC/DOCX 是否建议使用该服务，以及旧版 DOC 的真实解析质量。
10. HTTPS 内部证书、CA 注入、代理和零信任要求。

## 13. 验收标准

- OCR disabled 时，扫描 PDF 继续明确失败为 `OCR_REQUIRED`，兼容现状。
- OCR enabled 且配置正确时，扫描 PDF 能完成真实 FINAL_COMPARE。
- 文本 PDF 和 DOCX 不产生不必要 OCR 调用。
- 结果包含页码、段落/表格位置、解析器/引擎版本和 OCR warning。
- 表格单元格能进入现有基础表格比较。
- 低置信数字不会被静默当成可靠业务变化或可靠一致。
- 认证、额度、超时、5xx、非法响应和部分失败具有稳定错误码。
- API Key、完整 OCR 响应、合同全文和页面图片不进入日志。
- 临时文件在成功、失败和重试路径清理。
- 现有 29 项测试及新增 OCR 测试全部通过。
- 控制台明确显示解析器来源、OCR warning 和人工复核要求。

## 14. OCR 完成后的顺序

1. 用甲方脱敏扫描样本建立 OCR + diff 黄金集，校准误报和低置信策略。
2. 再实现 DRAFT_REVIEW 的真实模板/辅助资料解析纵向切片。
3. 接入本地 LLM 网关做结构化事实抽取和建议生成。
4. 最后推进复杂租金/清单规则、旧版 DOC 和生产级异步 OCR 恢复。

## 15. 下一会话启动语

```text
请在 D:\work\contract_review\contract-review-agent 继续项目开发。先完整阅读 AGENTS.md、README.md、docs/progress/README.md、docs/plans/20260820_ocr-document-parser-integration.md，以及 docs/progress 下最近 3 份记录，再检查 Git 状态和是否存在并行修改。

本次只实现 FINAL_COMPARE 的扫描 PDF OCR/外部文档解析纵向切片。保留 DOCX 和文本 PDF 本地解析；只有 OCR_REQUIRED 时调用外部解析 Adapter。先使用同步 /api/contracts/v3/parser/external/engine，采用供应商无关 DTO 和 ParsedDocument 映射，所有测试先用 MockTransport 和脱敏响应 fixture，不依赖真实甲方凭证。真实 Base URL、鉴权 header 或 Key 未确认时，不猜测、不写入仓库，并将实时联调标为 BLOCKED/PENDING。

按小步测试驱动实施，不同时接入 LLM，不做 DRAFT_REVIEW 全量规则，不自动 commit/push，不修改或提交 OCR 原始文档。任务结束按 docs/progress/README.md 新增进度文件。
```

## 16. 2026-08-20 首次真实联调结果

已使用完全脱敏、无文本层的 1 页合成扫描 PDF 完成真实联调：

- 宿主机访问甲方内部 Base URL 成功。
- `x-api-key` 鉴权已由只读任务列表接口确认，HTTP 200、业务码 200。
- 同步 engine 接口解析成功，139 KB 请求约 0.8 秒返回；服务端引擎耗时约 0.55 秒。
- 实际部署引擎版本为 3.20.11，与文档产品版本标识需要区分保存。
- 返回 1 个有效页面、9 个段落、1 个表格、12 个单元格和 21 条带分数的文字行。
- 文字行平均分约 0.9987、最低约 0.992；合同号、金额、日期和表格数字识别正确。
- OCR 会归一化文字间空格，因此验收不能用未经归一化的整段字符串做严格相等。
- 请求设置 `raw_ocr=1`，实际 pages 中未返回 `raw_ocr`；文字行及分数位于 `pages[].content`。
- 实际 result 字段与 OpenAPI 示例存在差异，例如返回 `success_count/total_count`，部分文档字段未出现；客户端必须使用宽松读取、项目侧严格映射。

容器网络发现：

- 同一接口从 Windows 宿主机访问正常。
- Docker Desktop 一次性 Worker 容器在 bridge 网络下收到无正文 HTTP 502。
- 显式禁用环境代理后结果不变，排除普通 `HTTP_PROXY` 继承问题。
- Docker host 网络模式下仍为无正文 HTTP 502，说明本地 Docker VM/零信任/内网路由路径尚未打通。
- 在解决容器网络前，可以完成 MockTransport、DTO、映射和本地单元测试，但不能把 Docker 真实端到端标记为通过。

建议网络处理顺序：

1. 请甲方网络/零信任管理员确认 Docker Desktop/WSL2 NAT 网段是否允许访问 OCR 内网地址。
2. 确认是否提供容器可访问的 DNS 名称或 HTTPS 网关，而不是依赖宿主机专属路由。
3. 如只为本地开发，可评估受控的宿主机转发代理；该代理不得进入生产默认架构。
4. 最终交付环境必须从实际 Worker 容器重新验证，不能用宿主机成功代替。
