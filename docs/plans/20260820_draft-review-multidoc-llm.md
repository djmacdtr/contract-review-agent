# DRAFT_REVIEW 多文档智能检查与 LLM 接入计划

## 1. 结论

下一主里程碑转向合同起草检查 `DRAFT_REVIEW`。

它不是对目标合同、模板和全部辅助资料做两两全文 diff。正确处理方式是：

```text
目标合同 vs 合同模板
  → 确定性条款/占位符/表格差异

目标合同、模板、N 份辅助资料
  → 每份文件独立识别与事实抽取
  → 字段语义映射与规范化
  → 跨文件事实矩阵
  → 冲突、缺失和不确定性

确定性差异 + 事实冲突 + 规则结果
  → LLM 生成有证据的解释和建议
```

LLM 是任意辅助资料智能理解的必要组成部分，但不能替代文字 diff、金额/日期比较、占位符检查和数学规则。

## 2. 接口决策

### 2.1 移除调用方辅助资料类型

当前 `reference_type` 枚举不符合真实业务：甲方可能传入任意文件，调用方无法稳定判断类型，也不能预先穷举所有资料种类。

新请求只包含文件信息：

```json
{
  "target_file": {
    "url": "https://files.example.com/draft.docx",
    "file_name": "待检查合同.docx"
  },
  "template_file": {
    "url": "https://files.example.com/template.docx",
    "file_name": "合同模板.docx"
  },
  "reference_files": [
    {
      "url": "https://files.example.com/reference-1.pdf",
      "file_name": "辅助资料一.pdf"
    },
    {
      "url": "https://files.example.com/reference-2.docx",
      "file_name": "辅助资料二.docx"
    }
  ]
}
```

调整方式：

- 控制台删除辅助资料类型下拉框；
- OpenAPI 示例不再出现 `reference_type`；
- 新业务逻辑完全忽略调用方类型；
- 为避免当前数据库迁移，既有 nullable `reference_type` 字段可暂时保留为历史兼容字段，不参与判断；
- 如果需要兼容旧联调客户端，可以短期接受该字段但标记 deprecated/ignored；正式接口冻结前再彻底移除。

### 2.2 辅助资料数量

- 业务上支持 1 到 N 份辅助资料，不绑定固定资料组合；
- “数量未知”不等于无限制，服务仍需配置安全上限；
- 建议新增 `MAX_REFERENCE_FILES`，开发默认 20，甲方根据任务量和硬件调整；
- 超限返回明确的输入错误，不截断文件列表。

### 2.3 系统自动识别结果

文件类型识别改为输出能力，而不是请求参数：

```json
{
  "file_id": "fil_xxx",
  "document_profile": {
    "document_kind": "项目评审意见",
    "title": "...",
    "confidence": 0.91,
    "generated_by": "LLM",
    "evidence_locations": []
  }
}
```

`document_kind` 使用开放字符串，不设置阻塞性枚举；无法判断时返回 `UNKNOWN`，任务继续执行并提示人工复核。

## 3. 工作流设计

### 3.1 LangGraph 节点

```text
download_files
  → parse_documents
  → normalize_documents
  → compare_template
  → detect_blank_fields
  → classify_documents
  → extract_facts_per_document
  → normalize_fact_candidates
  → build_fact_matrix
  → detect_conflicts
  → run_deterministic_rules
  → generate_advice
  → build_result
  → persist_result
```

### 3.2 解析路由

- 目标合同/模板 DOCX：`python-docx` 原生段落和表格；
- PDF：甲方文档解析服务，文本与扫描件统一输出段落、表格、页码和坐标；
- 辅助 DOCX：原生解析；
- 旧 DOC：在转换能力确定前明确失败或要求甲方预转换；
- 任一文件都保留 `raw_text`、规范化文本、文件 ID 和位置证据。

### 3.3 模板与目标合同

这部分优先复用 FINAL_COMPARE 的可靠 N:M 对齐，但增加起草特有规则：

- 固定条款新增、删除和修改；
- `##{字段}`、`手动补充`、空白线和空表；
- 允许填写区域与模板固定文本分离；
- 表格列、行、合并关系和必填内容；
- 明确文字和数字变化由程序判断，不调用 LLM 决定是否相等。

## 4. LLM 职责

### 4.1 必须使用 LLM 的部分

- 根据实际正文识别未知辅助资料的用途和标题；
- 从无固定格式的文档中抽取主体、金额、期限、利率、保证人、合同编号等事实；
- 将不同资料的同义字段映射到统一 `field_key`；
- 对程序已经发现的来源冲突给出解释；
- 基于已有证据生成审核建议。

### 4.2 禁止交给 LLM 的部分

- 金额、比例、日期和期限的精确比较；
- 租金、利息和合计计算；
- 明确占位符和空白检测；
- 原文、页码、坐标和来源的创造或修正；
- 在多个冲突来源中擅自选择“正确答案”；
- 将所有原文一次性发送给模型并要求直接给最终结论。

## 5. LLM Adapter 首版

### 5.1 网关

- 使用集团 OpenAI 兼容 `POST /v1/chat/completions`；
- 主模型暂定 `GLM-5.2`，以 `/v1/models` 实际结果为准；
- `stream=false`，初始并发 1；
- LangGraph 只依赖 `ContractLlmClient` Protocol，不直接依赖 SDK 响应类型。

### 5.2 结构化输出

官方 OpenAI 模型支持 JSON Schema Structured Outputs，但甲方网关指南没有确认该能力，因此首版不能假设可用。

兼容流程：

1. Prompt 要求只返回单个 JSON 对象；
2. 每个节点定义独立 Pydantic Schema；
3. 提取 JSON 并执行严格校验；
4. 只允许去除代码围栏等轻量修复；
5. Schema 失败最多重试两次；
6. 仍失败则返回不确定/节点失败，不把自由文本写入正式结果；
7. 联调确认 `response_format/json_schema` 后通过能力开关启用，Pydantic 校验仍保留。

### 5.3 核心模型

```text
DocumentProfile
- file_id
- document_kind
- title
- confidence
- evidence_locations[]

FactCandidate
- field_key
- display_name
- value_type
- raw_value
- normalized_hint
- source_file_id
- evidence_text
- location
- confidence

FactMatrixItem
- field_key
- display_name
- candidates[]
- status: CONSISTENT / CONFLICT / MISSING / UNCERTAIN
```

所有事实必须有文件 ID 和位置证据；字段不存在时返回缺失，不允许猜测补全。

## 6. 多文档处理策略

- 每份文件单独分类、分块和抽取，结果带稳定 `file_id`；
- 不把全部文件一次性塞入单个 Prompt；
- 同一文件按条款、标题和表格切块；
- 程序先用标签、关键词、条款编号和 RapidFuzz 生成候选；
- LLM 只处理候选块并返回结构化事实；
- 程序负责金额、日期、比例、主体名称等规范化；
- 再按 `field_key` 聚合全部文件，生成事实矩阵；
- embedding/rerank 首版关闭，只有候选召回效果不足时再启用。

## 7. 实施顺序

### 阶段 A：接口与真实解析闭环

1. 删除控制台辅助资料类型下拉框；
2. 请求示例移除 `reference_type`，兼容字段不再参与业务；
3. 将数量上限改为配置；
4. 将 DRAFT_REVIEW 从 Mock 切到真实下载和解析；
5. 输出每份文件的解析状态、页数、warning 和位置。

验收：任意命名、任意顺序、混合 DOCX/PDF 的 1–N 份辅助资料都能创建任务并完成解析。

### 阶段 B：模板确定性检查

1. 模板与目标合同可靠对齐；
2. 固定条款差异；
3. 未替换占位符、疑似空白和空表；
4. 先返回结构化 diff/rule，不依赖 LLM。

验收：模板固定文字、金额和占位符受控正样本召回率 100%。

### 阶段 C：LLM 网关纵向切片

1. `/v1/models` 最小探测；
2. OpenAI 兼容 Client、超时、重试和错误映射；
3. JSON 提取、Pydantic 校验和结构化重试；
4. 单文档 `DocumentProfile + FactCandidate`；
5. Mock、非法 JSON、401/403/404/429/502 和超时测试；
6. 获得 Key 后只做最小脱敏真实联调。

验收：一个未知辅助文件能返回有证据的自动类型和事实列表，模型失败不会生成虚假事实。

### 阶段 D：多文档事实矩阵

1. 对每份辅助资料独立抽取；
2. 字段语义映射；
3. Decimal、日期、期限、比例和主体规范化；
4. 生成跨文件一致、冲突、缺失和不确定状态；
5. 不设置来源优先级，冲突全部进入人工复核。

验收：新增、删除或更换任意辅助文件不需要修改枚举或代码分支。

### 阶段 E：风险和建议

1. 合并模板差异、空白、事实冲突和规则失败；
2. 程序生成风险等级；
3. LLM 只基于输入风险和证据生成建议；
4. 控制台展示自动识别类型、事实矩阵、来源证据和建议限制。

验收：建议中的每个事实都能回溯到结果中已有的文件和位置。

## 8. 测试与验收集

最小样本：

- 无辅助类型、未知文件名；
- 1、3、10 和配置上限数量的辅助文件；
- DOCX/PDF 混合顺序；
- 同一金额不同单位；
- 主体名称存在空格或组织形式差异；
- 金额、期限、利率、保证人和合同编号冲突；
- 文件中不存在目标字段；
- 模型非法 JSON、遗漏字段、无证据补全和诱导内容；
- LLM 不可用时保留模板确定性结果并明确降级边界。

核心指标：

- 关键事实准确率和召回率；
- 证据文件/位置正确率；
- JSON 首次和重试后合规率；
- 无证据新增事实比例必须为 0；
- 确定性金额、日期和占位符测试必须 100% 通过；
- 单任务模型调用数、耗时和失败降级情况。

## 9. 当前依赖与阻塞

开发 Mock、Client、Schema 和工作流不依赖真实 Key，可以立即开始。

真实 LLM 验收前需要甲方提供或确认：

- API Key 和零信任访问；
- `/v1/models` 实际模型列表；
- `response_format/JSON Schema` 支持情况；
- QPS、并发、最大输入/输出和超时；
- 网关是否记录完整合同 Prompt，以及日志保留和访问权限。

## 10. 推荐下一任务

先执行阶段 A，不立即编写完整多文档 LLM 逻辑：

1. 移除 `reference_type` UI/请求依赖；
2. 建立可配置辅助文件数量；
3. 完成 DRAFT_REVIEW 多文件真实下载和解析；
4. 复用 FINAL_COMPARE 完成模板与目标合同的确定性初版比对；
5. 同时定义 LLM 结构化 Schema 和 Adapter 契约，为阶段 C 做准备。

这样可以最快看到真实起草检查任务闭环，同时避免在文档解析、模型输出和跨文件规则三个变量都未知时一次性实现过大范围。
