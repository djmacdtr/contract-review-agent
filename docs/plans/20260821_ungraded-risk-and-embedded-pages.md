# 无等级风险模型与双嵌入页面规划

## 1. 背景与结论

甲方新增确认：合同比对不需要高、中、低风险评级。只要能够确认存在不合规、缺失、冲突或未经允许的变化，就统一作为风险项；风险内部只按业务类型区分，不按严重程度区分。

甲方提供的 `AI合同智能对比_审查报告样板_合并版_V2.html` 使用 Tab 合并展示起草阶段和放款阶段。实际交付需要两个独立页面，分别嵌入甲方系统的签约前节点和放款前节点。

最终方向：

- 后端采用“风险 / 人工复核 / 通过”三种业务语义，不再使用 HIGH/MEDIUM/LOW/INFO；
- 起草检查和放款比对使用两个独立路由和页面组件，不显示阶段切换 Tab；
- 两个页面继续共用同一 Vue SPA、基础组件和 API Client，避免复制两套工程；
- 测试控制台的任务创建、历史列表和调试详情继续保留，不等于甲方正式嵌入页面；
- 风险 Schema 和页面契约应在真实 LLM 接入前冻结，避免事实矩阵和模型 Prompt 二次返工。

## 2. 原型可复用内容

### 2.1 视觉和信息结构

可直接借鉴：

- 顶部报告名称、合同/业务关联信息和当前阶段；
- 红色“检出风险”与绿色“校验通过”总体状态条；
- 风险总数、删除/缺失、增加/变更、校验通过统计卡；
- 基准文件与当前文件卡片；起草阶段允许展示多份辅助资料；
- 按检查模块组织结果；
- 风险卡片展示标题、类型、文件位置、基准值、当前值、差异高亮和说明；
- 删除内容使用删除线，新增或变更内容使用高亮；
- 处理通过的规则以紧凑绿色行展示；
- 页面底部保留“辅助检查、最终人工判断”的限制说明。

### 2.2 风险分类

原型的“删除项 / 新增项”适合作为用户可理解的一级分类，但程序内部需要保留更精确类型：

```text
DELETION_OR_MISSING
- DELETED
- BLANK_OR_UNFILLED
- REQUIRED_ATTACHMENT_MISSING

ADDITION_OR_CHANGE
- ADDED
- MODIFIED
- NUMERIC_CHANGED
- TABLE_ROW_CHANGED
- TABLE_CELL_CHANGED
- SOURCE_CONFLICT
- RULE_FAILED
```

前端统计可合并为“删除/缺失”和“新增/变更”，详情仍展示精确 `risk_type`。

## 3. 原型中不直接照搬的内容

### 3.1 印章功能

原型包含印章名称、印章叠章和印章区域 OCR。当前已确认项目范围明确排除印章识别或真伪判断，因此：

- 起草页面不展示印章模块；
- 放款页面不展示印章名称或叠章结论；
- 不模拟印章通过或风险结果；
- 除非甲方后续正式变更范围并提供识别能力，否则不进入实现计划。

### 3.2 起草阶段签章空白

业务流程是在起草检查通过后打印并盖章，所以起草阶段签章区为空通常是正常状态。原型把起草阶段甲方签章空白作为风险，与当前流程冲突，不应复制。

### 3.3 固定辅助资料类型

原型按评审意见表、合规报告、方案函写死模块。接口仍接受任意数量和任意用途辅助文件，系统根据正文自动生成开放式 `document_kind`。页面按模型识别结果和实际检查结果动态分组，不要求调用方选择固定类型。

### 3.4 历史版本与印章 Tab

当前起草接口没有“上一版合同”参数，不单独展示历史版本模块。放款接口本身就是基准版与最终版的版本差异页面。

## 4. 新风险语义

### 4.1 三种结果对象

1. `risk_items`：有证据支持的不合规、缺失、冲突、计算失败或未经允许变化；全部都是风险，不分等级。
2. `review_items` 或兼容期 `warnings`：解析质量不足、OCR 低置信度、无法对齐、来源含义不明确等“目前无法确认是否违规”的事项。
3. `passed_checks`：实际执行且通过的规则或一致性检查。

OCR 低置信度不能因为“需要关注”就直接算风险；它属于人工复核。只有进一步确认内容确实变化后，才产生风险项。

### 4.2 总体结论

```text
RISK_FOUND
  存在至少 1 个 risk_item

REVIEW_REQUIRED
  没有已确认风险，但存在未完成、低可靠性或需要人工判断的 review_item

PASS
  risk_items=0，review_items=0，且所有必要检查完成并可靠
```

### 4.3 统计结构

建议结果 Schema 改为：

```json
{
  "statistics": {
    "risk_count": 11,
    "deletion_or_missing_count": 4,
    "addition_or_change_count": 7,
    "review_count": 2,
    "passed_check_count": 6
  }
}
```

不再输出 `high`、`medium`、`low`、`info`。

### 4.4 风险项结构

```json
{
  "risk_id": "risk_xxx",
  "module_code": "TEMPLATE_INTEGRITY",
  "risk_type": "DELETION_OR_MISSING",
  "change_type": "DELETED",
  "title": "固定条款缺失",
  "description": "...",
  "source_evidence": [],
  "related_diff_ids": ["diff_xxx"],
  "related_rule_ids": [],
  "requires_manual_action": true
}
```

不包含 `severity` 或模型生成的严重程度。前端排序按模块顺序、文件位置和生成顺序，不按等级排序。

### 4.5 技术差异与业务风险

`diff_items` 保留为底层证据，不等于每个差异都是风险：

- 模板允许填写项进入过滤轨迹，不产生风险；
- OCR 疑似差异进入 review/warning，不产生风险；
- 确认的固定条款、数值或表格差异生成对应 risk item，并通过 `related_diff_ids` 关联；
- 事实冲突和规则失败可以不依赖 diff，直接生成 risk item。

## 5. API 与数据库迁移

移除 `severity` 是破坏性 Schema 变化，建议将结果 `schema_version` 升级为 `2.0`，不要继续伪装为兼容扩展。

### 5.1 API

- `DiffItem` 删除 `severity`；
- `RuleCheck` 删除 `severity`；
- 新增明确的 `RiskItem`、`ReviewItem` 和新统计结构；
- 任务列表由四个等级计数改为 `risk_count` 和 `review_count`；
- AI 建议删除“优先处理 HIGH”之类文案，改为“建议处理事项”；
- `review_reason` 继续保留并扩展，用于解释为什么不能确认风险。

### 5.2 数据库

建议新增 Alembic 迁移：

- `check_task.risk_count`；
- `check_task.review_count`；
- 既有 `high_risk_count`、`medium_risk_count`、`low_risk_count`、`info_count` 暂时保留为 deprecated，只用于读取旧任务；
- 新任务只写新字段；
- 旧测试任务可按 `high + medium` 回填 risk、`low + info` 回填 review，但页面明确标识为 legacy 统计；
- 稳定一个发布周期后再决定是否删除旧列，避免本阶段做不可逆清理。

## 6. 两个独立嵌入页面

### 6.1 路由

建议同一 SPA 增加两个报告路由：

```text
/console/#/reports/draft/:taskId
/console/#/reports/final/:taskId
```

- 起草页面只接受 `DRAFT_REVIEW` 任务；
- 放款页面只接受 `FINAL_COMPARE` 任务；
- 任务类型不匹配时显示明确错误，不自动跳到另一页面；
- 页面只通过 `task_id` 查询状态和结果，不在 URL 中携带合同下载地址；
- 页面不显示控制台顶部导航、任务创建入口或阶段切换 Tab。

如果甲方要求非 Hash URL，再为 FastAPI 静态资源增加 history fallback；初版继续使用 Hash Router 最稳妥。

### 6.2 起草检查页面

建议模块：

1. 任务状态与解析进度；
2. 总体结论和无等级统计；
3. 目标合同、模板和实际辅助资料；
4. 模板完整性和未填写检查；
5. 跨文档事实一致性；
6. 表格和数学规则；
7. 风险项与证据；
8. 人工复核/处理警告；
9. AI 建议和能力限制。

模块根据结果是否存在动态显示，不根据固定辅助资料类型硬编码。

### 6.3 放款比对页面

建议模块：

1. 任务状态与 OCR/解析进度；
2. 总体结论和无等级统计；
3. 原文件与需要比对文件；
4. 新增、删除、修改、数字和表格风险；
5. 左右原文与差异片段；
6. OCR/对齐人工复核事项；
7. AI 摘要和能力限制。

不包含印章模块。

### 6.4 控制台与正式嵌入页边界

现有控制台继续保留：

- `/tasks` 历史任务；
- `/tasks/new/draft` 起草任务入口；
- `/tasks/new/final` 放款任务入口；
- `/tasks/:taskId` 调试详情和原始 JSON。

正式嵌入页是只读业务报告，不提供文件 URL 编辑、任务重试、原始 JSON 或开发诊断信息。

### 6.5 嵌入安全与联调待确认

甲方需要确认实际嵌入方式。若采用 iframe：

- 提供可配置 `Content-Security-Policy: frame-ancestors` 白名单；
- 不使用允许任意来源嵌入的通配配置；
- 甲方系统通过后端先创建任务，再把不可猜测 `task_id` 传给 iframe 页面；
- 如需自适应高度，可定义最小 `postMessage` 协议，并严格校验父页面 origin；
- 本服务仍不实现登录鉴权，部署网络和甲方网关负责访问控制。

## 7. 共享前端组件

建议拆分：

```text
components/report/
├─ ReportHeader.vue
├─ ConclusionBanner.vue
├─ ResultStatistics.vue
├─ FileSummary.vue
├─ CheckModule.vue
├─ RiskItemCard.vue
├─ DiffEvidence.vue
├─ ReviewItemCard.vue
├─ PassedCheckList.vue
└─ CapabilityLimitations.vue

views/reports/
├─ DraftReportView.vue
└─ FinalReportView.vue
```

两个页面共享视觉基础，但各自负责业务模块编排，不使用一个巨型组件根据任务类型写大量条件分支。

## 8. 推荐实施顺序

### 里程碑 0.3.1：产品契约调整

1. 先提交当前阶段 B 的确定性模板基线；
2. 建立结果 Schema 2.0 和数据库兼容迁移；
3. 删除等级计算、等级统计和 HIGH/MEDIUM/LOW 展示；
4. 明确 risk、review、pass 三类输出；
5. 更新 API、OpenAPI、测试、README 和旧任务兼容读取。

### 里程碑 0.3.2：黄金标注和规则收敛

1. 使用固定真实文件标注 26 个差异与 3 个扩展表格复核点；
2. 标签简化为“风险 / 允许填写 / 对齐误报 / 人工复核”；
3. 真实风险 100% 保留，允许填写和对齐误报不产生 risk item；
4. 所有 review item 必须有非空原因码。

### 里程碑 0.3.3：双页面壳与静态数据

1. 创建两个独立嵌入路由；
2. 使用现有真实任务结果 fixture 完成组件和响应式布局；
3. 去除 Tab、等级徽标和开发入口；
4. 保留测试控制台原路由；
5. 人工完成桌面宽度和甲方 iframe 目标尺寸视觉验收。

### 里程碑 0.4.0：LLM 单文档纵向切片

1. 实现 OpenAI 兼容 Client 的离线错误、重试和严格结构校验；
2. 使用 `项目方案确认函.docx` 完成首次真实单文档抽取；
3. 通过后抽取目标合同并建立最小事实矩阵；
4. 风险输出直接遵循 Schema 2.0，不再包含等级。

### 后续

- OCR 恢复后重跑完整 5 文件黄金任务；
- 完成全量事实矩阵和跨资料风险；
- 将真实 API 结果接入两个嵌入页；
- 确认 iframe 域名和尺寸后完成嵌入联调。

## 9. 验收标准

- 页面和 API 不出现高、中、低或 INFO 风险等级；
- 任一确认不合规项计入 `risk_count`；
- OCR/解析不确定性不会冒充风险，统一进入人工复核；
- 风险总数与删除/缺失、增加/变更等分类可解释地对应；
- 起草和放款页面拥有不同 URL，不显示 Tab，刷新后可以恢复任务状态；
- 两个页面共享组件但模块编排独立；
- 正式嵌入页不暴露文件签名 URL、原始 JSON、开发入口或敏感日志；
- 原型中的印章功能不出现在正式结果中；
- 旧任务仍可查看，不因 Schema 2.0 迁移导致页面崩溃。
