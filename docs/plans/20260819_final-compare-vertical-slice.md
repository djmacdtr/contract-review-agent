# FINAL_COMPARE 真实纵向切片实施计划

> 计划日期：2026-08-19  
> 计划状态：READY  
> 目标：尽快通过现有 API 和控制台看到真实、可解释的两个文件比对效果

## 1. 背景

里程碑 0–1 已建立 FastAPI、PostgreSQL 持久化任务队列、Worker、Mock LangGraph、控制台和 Docker Compose，但当前结果仍为 `MOCK`。

下一步不直接引入 OCR、LLM 或完整合同规则，而是优先打通一个可演示的确定性纵向切片：

```text
两个文件 URL
→ 受控下载
→ DOCX/文本型 PDF 解析
→ 可追溯统一文档模型
→ 基础段落/表格对齐
→ 文字和数值差异
→ 结构化 JSON
→ 控制台展示
```

优先选择 `FINAL_COMPARE`，因为它只有 baseline 和 target 两个文件，边界清晰、最容易快速验证真实效果。

## 2. 开始前门槛

1. 阅读 `AGENTS.md`、`README.md`、本计划和最近进度记录。
2. 检查当前 Git 状态。当前仓库创建本计划时仍是 `UNBORN`，工程文件全部未跟踪。
3. 运行当前里程碑 0–1 的关键回归，确认基线仍通过。
4. 建议在用户明确授权后创建里程碑 0–1 基线提交；没有授权不得自动 commit，但必须在进度文件中持续记录工作树状态。
5. 不修改外层需求原件和方案文档。

## 3. 本阶段范围

### 3.1 开发用 fixture 文件服务

在 Docker Compose 中提供仅开发/测试使用的 fixture 文件服务，建议使用 profile：

- 从 `tests/fixtures/files/` 提供脱敏小型 DOCX/PDF；
- Worker 通过 Docker 内部地址访问，例如 `http://fixture-server/...`；
- 只在开发测试环境设置 `ALLOW_HTTP_DOWNLOADS=true`；
- 只允许 `fixture-server` 主机；
- 不改变正式 API 的“只接受 URL”契约；
- 生产默认不启动 fixture 服务。

### 3.2 受控下载器

实现：

- 只支持 `http`/`https`；
- URL 格式和主机 allowlist；
- DNS 解析后的 IP 检查；
- 默认拒绝环回、链路本地、私网、保留地址和云元数据地址；
- 开发 fixture 主机通过显式 allowlist 放行；
- 每次重定向重新校验；
- 重定向次数、连接/读取/总超时；
- Content-Length 预检查和流式大小上限；
- 实际字节数二次限制；
- SHA-256；
- 安全文件名、MIME/魔数基础检测；
- 错误码区分超时、过大、禁止目标、类型不支持和下载失败；
- 日志只记录安全 URL，不记录 query/fragment。

### 3.3 临时文件生命周期

- 每个任务使用独立目录；
- 文件名不直接信任 URL 路径；
- 下载、解析和比对失败都在 `finally` 清理；
- 生产默认立即清理；
- 测试可检查清理结果；
- 禁止写出配置的临时根目录；
- 不把原文件二进制保存到 PostgreSQL。

### 3.4 统一文档模型

实现 `ParsedDocument` 及必要子模型：

- file_id、role、file_name、sha256、page_count；
- blocks；
- block_id、type、order、raw_text、normalized_text；
- page、paragraph_index、table_index、row、column、section、bbox、confidence；
- 表格 rows/cells；
- parser_metadata 和 warnings。

模型必须能由 DOCX 和 PDF 解析器共同产生，后续规则不得直接依赖 `python-docx` 或 PDF 库对象。

### 3.5 DOCX 解析

- 段落顺序和文本；
- 标题/条款线索；
- 表格、行、列和单元格；
- 段落索引、表格索引、行列位置；
- 页眉页脚可保留但默认从主比较文本排除；
- DOCX 无可靠物理页码时明确使用结构位置，不伪造页码。

### 3.6 文本型 PDF 解析

- 使用 pypdf/pdfplumber 中更适合当前结构的实现；
- 逐页提取文本和页码；
- 识别文本层为空或密度过低；
- 扫描件返回明确 `OCR_REQUIRED`/`SCANNED_PDF_NOT_SUPPORTED`，本阶段不调用 OCR；
- 不对复杂 PDF 表格做过度承诺。

### 3.7 基础确定性比对

实现：

- Unicode NFKC；
- 全角/半角、空白和常规换行归一化；
- 保留原文；
- 页眉页脚降权/排除；
- 条款编号和标题优先匹配；
- 完全匹配、相似度候选和顺序约束；
- 新增、删除、修改；
- 数字、金额、百分比、日期和期限变化单独识别；
- 表格基础行/单元格变化；
- 每条差异包含 baseline、target、来源位置和置信度；
- 无法可靠对齐时输出 warning，不强行制造精确对应。

数值只做原文值识别和变化分类；复杂金额计算、租金公式和容差规则留到后续。

### 3.8 工作流与结果

- `FINAL_COMPARE` 使用真实下载、解析和比对节点；
- `DRAFT_REVIEW` 继续保持 Mock，不在本阶段扩展；
- 结果明确区分 `MOCK` 与确定性真实执行，建议使用 `execution_mode="RULE_BASED"`；
- 没有调用 LLM 时 `actual_model=null`，`advice` 明确不可用或来自固定规则摘要；
- 进度阶段反映真实下载、解析、对齐、差异计算和结果保存；
- 失败错误不泄露完整 URL、文件内容或堆栈。

### 3.9 控制台

- 在现有 FINAL_COMPARE 表单中使用 fixture URL 进行测试；
- 展示真实差异类型、严重度、前后文本、来源位置和警告；
- 明确显示 `RULE_BASED`，不标成 AI 或法律审查；
- 保持原始 JSON 查看；
- 不增加文件上传。

## 4. 明确不做

- 扫描 PDF OCR；
- 旧版 `.doc`；
- 真实 LLM、Embedding、Rerank；
- DRAFT_REVIEW 真实模板检查；
- 复杂表格合并单元格恢复；
- 租金数学规则；
- 印章；
- 文件上传；
- 报告文件；
- 鉴权和模板库。

## 5. 建议实施顺序

1. 建立脱敏 fixtures 和开发 fixture-server。
2. 完成下载安全策略、临时目录和下载测试。
3. 定义统一文档模型。
4. 实现 DOCX 解析及回归 fixture。
5. 实现文本 PDF 解析及扫描件拒绝路径。
6. 实现文本归一化、段落对齐和差异分类。
7. 实现基础表格差异。
8. 将真实节点接入 FINAL_COMPARE Graph。
9. 调整结果 Schema 和控制台展示。
10. 完成 Docker 集成与端到端验证。

避免同时修改下载、解析、比对、工作流和前端后才第一次运行测试。每一阶段先建立单元/契约测试再向下集成。

## 6. 最低测试矩阵

| 场景 | 预期 |
|---|---|
| 相同 DOCX | 无业务差异 |
| 段落新增/删除/修改 | 类型和前后文本正确 |
| 金额/比例/日期变化 | 独立数值变化类型 |
| DOCX 表格单元格变化 | 表格差异及行列位置 |
| 相同文本 PDF | 无业务差异 |
| 文本 PDF 段落变化 | 页码位置正确 |
| 扫描 PDF | 明确要求 OCR，不假装成功 |
| 文件过大 | 流式中止并清理临时文件 |
| 重定向到禁止地址 | 拒绝 |
| allowlist 外主机 | 拒绝 |
| 下载中断/超时 | 可解释失败且不残留文件 |
| Worker 重试 | 不复用损坏临时文件，不覆盖旧任务 |

## 7. 验收标准

- 现有里程碑 0–1 测试继续通过；
- `POST /api/v1/final-comparisons` 接受两个 fixture URL 并返回 HTTP 202；
- Worker 完成真实下载、解析和比较；
- 任务到达 `SUCCEEDED / COMPLETED / 100`；
- 结果包含真实、可追溯的 `diff_items`；
- 控制台能展示段落、数值和基础表格变化；
- `execution_mode` 不再是 `MOCK`，且不会误标成 LLM；
- 扫描 PDF 返回明确不支持/OCR 要求；
- SSRF、大小、超时、重定向和清理测试通过；
- Docker Compose 完整闭环通过；
- 日志敏感信息扫描无泄露；
- 外层需求资料哈希不变；
- 任务结束新增 `docs/progress/` 记录。

## 8. 主要风险

- 当前仓库尚无首次提交，跨会话变更难以区分；建议尽快在用户授权后保存已验证基线。
- SSRF 防护与开发 fixture 内网访问存在策略冲突，必须通过显式主机 allowlist 解决，不能全局允许私网。
- DOCX 没有稳定物理页码，结果位置必须使用段落/表格结构索引。
- PDF 文本抽取顺序可能与视觉顺序不一致，需要 fixture 验证和 warning。
- 本阶段不应为了“看起来像 AI”而调用 LLM；先保证确定性证据链正确。

## 9. 下一会话启动语

```text
请在 D:\work\contract_review\contract-review-agent 继续项目开发。先完整阅读仓库根目录 AGENTS.md、README.md、docs/progress/README.md、docs/plans/20260819_final-compare-vertical-slice.md，以及 docs/progress 下最近 3 份记录；再检查 Git 工作树和当前 Docker 状态。

本次目标是按照计划实现 FINAL_COMPARE 的真实纵向切片，让两个文件 URL 经过受控下载、DOCX/文本型 PDF 解析、确定性文字/数值/基础表格比对后，返回可追溯结构化 JSON 并在控制台展示。DRAFT_REVIEW、OCR、真实 LLM 和复杂合同规则不在本次范围。

先给出简短实施计划，然后直接按小步测试驱动方式实施。保护所有现有和并行会话修改，不自动 Git commit，不删除 Docker volume。任务结束时按 docs/progress/README.md 新增进度文件并在最终回复给出路径。
```

