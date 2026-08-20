# FINAL_COMPARE 0.4.1 精度收口计划

## 1. 目标

在不降低真实合同变更召回率的前提下，完成 FINAL_COMPARE 0.4.1 精度收口：

- 将当前 46 页相同内容负样本从历史任务的 16 项，稳定收敛为最多 2–3 项 LOW 人工复核；
- HIGH=0、MEDIUM=0、数值差异=0，结论为 `REVIEW_REQUIRED` 而不是 `RISK_FOUND`；
- 定点消除表格设备名称跨行/阅读顺序误报；
- 保留法律条款单字和金额占位符单字符差异，避免为追求 0 差异而漏检真实修改；
- 用正样本证明金额、日期、比例、主体、条款和表格真实变化仍能被识别。

## 2. 当前基线

- 分支：`feat/final-compare-alignment`
- 当前提交：`f5a475e`
- workflow/rules：`0.4.0`
- 真实任务：`tsk_01M0F259V1903F3Y8G7AZ5RMH6`
- 双侧 external parser `auto`，46/46 页，对齐覆盖率 1.0，全局相似度 0.9905。
- 历史持久化结果：16 项、3 HIGH、13 MEDIUM。
- 当前代码离线复核：13 项规范化后相等，预计只剩 3 LOW。
- 三项残余：法律条款单汉字、表格设备名称跨行顺序、金额空白占位符单字符。

## 3. 范围

### 3.1 本阶段实施

1. 表格续行/跨行阅读顺序定点修复。
2. OCR 单字符复核原因编码与前端分区展示。
3. 负样本和受控正样本回归测试。
4. 只执行一次 46 页真实 external 端到端复验。
5. Docker、API、Worker、控制台、迁移和文档验收。
6. PR 从 Draft 收口到 Ready；不自动合并。

### 3.2 本阶段不实施

- 不追求无条件 0 差异；
- 不将单字 OCR 差异全局忽略；
- 不接入 LLM、Embedding 或 Rerank；
- 不启动 DRAFT_REVIEW 真实实现；
- 不做约 200 页性能测试或异步 OCR；
- 不修改数据库 Schema；
- 不做印章、报告、上传和鉴权；
- 不执行甲方最终 Docker 网络验收。

## 4. 实施步骤

### 步骤 0：冻结验收基线

- 保留历史任务及诊断数据，不覆盖或重写已有结果。
- 将 16 项安全分类固化为测试说明，不保存合同全文或完整 OCR 响应。
- 确认当前工作树中的进度文件属于已有会话产出，实施时一并保护。

完成标准：能够从测试和进度记录复述 13 项应抑制、3 项应保留的原因。

### 步骤 1：先补失败测试

新增最小脱敏 fixture，覆盖：

1. 同一表格单元格文字被拆到相邻续行，其他列为空；应合并且不产生差异。
2. 相邻两行具有不同序号、金额、日期或设备编号；不得合并。
3. 字符集合相同但真实词序不同；不得因 `Counter` 相同而静默忽略。
4. 法律条款单字增删；必须保留 LOW 或更高等级。
5. 金额、日期、比例变化；必须保持 HIGH。
6. Markdown、`<br>`、LaTeX 表达噪声；必须产生 0 项差异。

完成标准：实现前至少一个表格续行测试按预期失败，现有关键变化测试继续通过。

### 步骤 2：实现严格的表格续行合并

建议在 `app/comparison/reliable.py` 的表格可比较单元构建层实现，不修改供应商原始响应：

- 只处理同一已匹配表格内相邻行；
- 续行的主键/序号列为空，关键数值列为空；
- 非空内容集中在同一文本列；
- bbox、row/column 或页面顺序能够证明为相邻续行；
- 合并后双方数字 token、设备编号和其他列一致；
- 不满足任一条件时保持原差异并人工复核。

禁止采用以下宽松规则：

- 全局字符 Counter 相同即认为一致；
- 忽略所有表格行顺序；
- 删除短文本或空值较多的行；
- 仅凭相似度高就吸收数值变化。

完成标准：历史 `diff_000008` 类型的阅读顺序误报被消除，受控真实词序/数值变化不被吸收。

### 步骤 3：区分业务风险与 OCR 复核项

在不破坏现有 schema 的前提下，优先增加可选原因字段，例如：

```json
{
  "severity": "LOW",
  "requires_manual_review": true,
  "review_reason": "OCR_SINGLE_CHAR_VARIANCE"
}
```

建议原因码：

- `OCR_SINGLE_CHAR_VARIANCE`
- `OCR_PLACEHOLDER_VARIANCE`
- `OCR_READING_ORDER_VARIANCE`

规则：

- 只有两侧均来自 OCR、无数值 token 变化且差异很小时才允许降为 LOW；
- 法律条款单字差异仍保留在结果中；
- 空白金额占位符差异只标记疑似噪声，不做全局字符等价替换；
- 前端将 LOW OCR 复核项与 HIGH/MEDIUM 业务风险分区或默认折叠，但仍可展开查看证据。

完成标准：剩余两项在控制台显示为“需要人工复核/疑似 OCR 单字符”，不再显示为高风险。

### 步骤 4：建立最小正负黄金集

负样本：

- Markdown、HTML、LaTeX、换行、段落 N:M、表格续行、重复页眉页脚。

正样本：

- 金额修改；
- 日期修改；
- 比例/利率修改；
- 主体名称单字修改；
- 完整条款删除/新增；
- 表格设备编号、数量、金额和单元格修改；
- 法律条款单字增删。

每个正样本必须断言：差异类型、等级、前后位置和关键 token。不能只断言“差异数大于 0”。

完成标准：关键数值/条款正样本召回率 100%，负样本无 HIGH/MEDIUM 误报。

### 步骤 5：代码和 Docker 验证

按顺序执行：

1. 新增单元测试定向运行；
2. comparison/workflow 定向测试；
3. Docker PostgreSQL 全量 pytest；
4. 变更范围 Ruff；
5. Vue typecheck 和 production build；
6. `docker compose config --quiet`；
7. runtime/test 镜像从当前源码构建；
8. `alembic check`；
9. `/health`、`/ready`、`/docs`、`/console/` 冒烟；
10. 日志和 Git 变更敏感值扫描。

完成标准：所有新增测试通过，不降低当前 74 项基线；无新迁移、无敏感信息、服务可恢复运行。

### 步骤 6：只重跑一次 46 页真实任务

所有本地测试通过后，才允许再次调用甲方 external parser。复用相同只读负样本，创建新任务，不覆盖旧任务。

硬门槛：

| 指标 | 验收门槛 |
|---|---:|
| 任务状态 | `SUCCEEDED / COMPLETED / 100` |
| 解析页数 | 双侧 46/46 |
| 对齐可靠性 | `reliable=true` |
| 双侧覆盖率 | ≥ 0.90，目标保持接近 1.0 |
| HIGH | 0 |
| MEDIUM | 0 |
| 数值差异 | 0 |
| LOW | ≤ 3，目标 2 |
| 结论 | `REVIEW_REQUIRED`；若最终 0 项且无警告才可 `PASS` |
| 日志泄露 | 0 |
| 临时目录残留 | 0 |

若 external 本次产生新的 OCR 随机字符差异，先归因并记录，不为了满足数量门槛增加宽松忽略规则。

### 步骤 7：控制台人工验收

- 查看任务状态、解析模式、可靠性和覆盖率；
- 验证 HIGH/MEDIUM 业务风险与 LOW OCR 复核项的展示层级；
- 验证双方页码、多个位置、表格行列和分页；
- 验证原始 JSON 仍保留稳定英文协议值；
- 检查长文本不会破坏布局。

完成标准：浏览器人工确认，不以 HTTP 200 替代视觉验收。

### 步骤 8：PR 收口

- workflow/rules 建议升级为 `0.4.1`；API 版本和 `schema_version=1.0` 保持不变；
- 更新 README、测试数字、真实任务 ID、耗时和限制；
- 新增进度记录，注明真实调用次数和最终服务状态；
- 将 `feat/final-compare-alignment` PR 从 Draft 标记 Ready；
- 未获用户明确授权不合并 PR。

完成标准：分支 clean、远端同步、PR 检查通过、交接文件完整。

## 5. 推荐提交拆分

1. `fix(final-compare): normalize table continuation rows`
2. `feat(console): separate OCR review-only differences`
3. `test(docs): close final compare precision acceptance`

提交和推送必须由执行会话在用户授权范围内完成；本计划本身不授权自动合并。

## 6. 完成定义

FINAL_COMPARE 0.4.1 只有同时满足以下条件才算完成：

- 46 页真实负样本最终 0 HIGH、0 MEDIUM、0 数值变化、最多 3 LOW；
- 表格续行误报被定点消除；
- 两项单字符 OCR 差异保留为可解释 LOW 复核；
- 关键正样本召回率 100%；
- 全量测试、Docker 构建、迁移、健康检查、日志脱敏和浏览器验收通过；
- PR Ready，文档与进度记录完整；
- 没有开始 DRAFT_REVIEW、LLM、200 页或异步 OCR 的范围外工作。

## 7. 0.4.1 之后

完成本计划后，下一主里程碑应使用真实对应的 DOCX 与盖章扫描 PDF 建立 FINAL_COMPARE 主黄金集：

1. 验证 `python-docx` 与 external `scan` 的 N:M 对齐；
2. 验证 DOCX 表格与 OCR 表格的合并单元格和空单元格；
3. 制作受控金额、日期、条款和表格修改版本；
4. 通过后再决定先推进 DRAFT_REVIEW 还是约 200 页性能基线。

## 8. 2026-08-20 执行结果

- 已从失败测试开始完成严格表格续行合并、OCR 复核原因码、控制台复核分区及正负黄金集。
- Ruff、Vue typecheck/build、Docker runtime/test 构建、Docker PostgreSQL 全量 91 项测试、Compose、Alembic 和 HTTP 冒烟均通过。
- 唯一一次 46 页真实任务为 `tsk_01M0F5C0FB1SRY05XQP6AGKPW0`；任务在 `PARSING / 35%` 以 `OCR_SERVICE_UNAVAILABLE` 安全失败，未生成结果且未重跑。
- 当前会话没有可连接的浏览器实例，控制台视觉验收未完成；HTTP `/console/` 为 200 不能替代该项。
- 因真实精度门槛尚无新的端到端证据，完成定义未满足，PR 保持 Draft。
