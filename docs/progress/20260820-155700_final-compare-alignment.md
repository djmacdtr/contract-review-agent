# 任务进度：FINAL_COMPARE 0.4.0 一致解析与可靠对齐

## 基本信息

- 时间：2026-08-20 15:57:00 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / FIX / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/final-compare-alignment`
- 当前提交：`1d15f4a`（第三组控制台/文档提交尚未生成）
- 工作树状态：dirty；仅包含本阶段控制台、验收脚本、README 和三份待纳入进度记录

## 用户目标

将 FINAL_COMPARE 升级到 0.4.0：统一 PDF/PDF 解析路径，引入解析器无关的可比较单元、N:M 对齐、表格兼容门控和差异爆炸保护，修复相同内容产生 2,099 项风险差异的问题，并完成一次 46 页真实验收。

## 本次完成

- 合并已 Ready 的 PR #1，保留三个原始提交和 `feat/final-compare-ocr` 分支，从最新 `main` 创建 `feat/final-compare-alignment`。
- 完成任务级解析计划：DOCX/DOCX 均走 `python-docx`；PDF/PDF 均走 external `auto`；混合格式中 PDF 走 external `scan`；正式路径不再静默回退 `pdfplumber`。
- OCR Client 支持调用方传入 `auto/scan`，结果保存实际 `parse_mode`，并返回聚合 `PDF_EXTERNAL_PARSE_USED` warning。
- 新增 `ComparableDocument/ComparableUnit`、唯一条款和文本锚点、局部 1–4 对 1–4 动态规划、页面文本 fallback、表格兼容门控及原始位置回映。
- 增加中文空格、软换行、零宽字符、标点和外部解析器 HTML/Markdown/LaTeX 表达噪声规范化；数值 token 保持敏感。
- 增加覆盖率、未匹配比例、全局相似度、候选/最终差异数、兼容表格数、fallback 和可靠性原因等诊断指标；不可靠候选不会升级为 HIGH/MEDIUM 风险。
- `DiffSide.locations` 返回 N:M 全部位置，保留原 `location`；workflow/rules 升至 `0.4.0`，API 路由及 `schema_version=1.0` 不变。
- 控制台增加可靠性、覆盖率、候选/最终差异、fallback、聚合 warning、解析模式、多位置和每页 20 项分页。
- 完成唯一一次 46 页 PDF/PDF external `auto` 真实任务。该任务暴露 16 项残余解析表达差异，随后以安全聚合诊断修复；未重复调用外部 OCR。
- 原有两份未跟踪诊断记录内容保持不变，SHA-256 分别仍为 `643C501B...9E999` 和 `020A4767...8AFA1`。

## 修改文件

- `app/adapters/document_parser/base.py`、`textin_client.py`、`textin_parser.py`：解析模式契约、请求和元数据。
- `app/documents/router.py`：任务级格式配对路由。
- `app/comparison/models.py`、`reliable.py`、`engine.py`：统一比较模型、N:M 对齐、可靠性诊断和 OCR 复核分级。
- `app/workflows/final_compare.py`：0.4.0 版本、配对解析及结果扩展。
- `frontend/src/api/types.ts`、`utils/labels.ts`、`views/TaskDetailView.vue`：可靠性展示与分页。
- `scripts/e2e_ocr_acceptance.py`：0.4.0 双 external `auto` 硬门槛验收。
- `tests/`：解析路由、正负样本、结构噪声、表格门控、fallback、可靠性与 Worker 回归。
- `README.md`：0.4.0 路由、诊断、验收现状和能力边界。
- `docs/progress/20260820-150430_diff-explosion-diagnosis.md`、`20260820-151201_document-parser-routing-decision.md`：按原内容纳入版本控制。

## 接口、数据和配置变化

- API：无新增路由或请求字段；结果仅向后兼容增加 `DiffSide.locations` 和 `metadata.comparison_diagnostics`。
- 数据库/迁移：无模型或迁移变化；`alembic check` 无缺失迁移。
- 配置：无新增环境变量；external `parse_mode` 由任务级路由传入。
- 兼容性：DRAFT_REVIEW 继续 Mock；DOCX/DOCX 真实比对不依赖 OCR；正式 PDF 未配置 external parser 时返回 `OCR_NOT_CONFIGURED`。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 变更前 Docker 测试 | 通过 | `55 passed, 1 warning` |
| 路由定向测试 | 通过 | `26 passed` |
| 对齐与 workflow 定向测试 | 通过 | `26 passed, 1 warning` |
| Docker PostgreSQL 全量测试 | 通过 | `74 passed, 1 warning`；唯一 warning 为 LangGraph 上游未来弃用提示 |
| 变更范围 Ruff | 通过 | 本分支及未提交 Python 文件全部通过；全仓库仍有 43 个既有 E501，不属于本阶段引入 |
| Vue typecheck/build | 通过 | `vue-tsc --noEmit` 和 Vite production build；仅有既有大 chunk 提示 |
| `docker compose config --quiet` | 通过 | 默认服务仍为 API、Worker、PostgreSQL |
| Docker test/runtime build | 通过 | 测试和常驻运行镜像均从当前源码重建 |
| `alembic check` | 通过 | `No new upgrade operations detected` |
| 健康冒烟 | 通过 | `/health`、`/ready`、`/docs`、`/console/` 均为 HTTP 200 |
| API 重启持久化 | 通过 | 真实任务重启前后均为 `SUCCEEDED` |
| 命名卷检查 | 通过 | `contract-review-postgres-data` 可 inspect，未删除 volume |
| 浏览器视觉验收 | 待完成 | 当前没有可连接浏览器；未用 HTTP 200 冒充视觉通过 |

## 46 页真实任务与安全回放

- 任务：`tsk_01M0F259V1903F3Y8G7AZ5RMH6`
- 总耗时：88.3 秒；任务达到 `SUCCEEDED / COMPLETED / 100`。
- baseline：46/46 页、external `auto`、服务耗时 42,024 ms、响应 5,250,319 字节、373 blocks、6 tables、2,186 cells、平均/最低置信度 0.9968/0.9856。
- target：46/46 页、external `auto`、服务耗时 36,381 ms、响应 5,280,081 字节、383 blocks、9 tables、2,197 cells、平均/最低置信度 0.9932/0.9531。
- 对齐：双侧字符加权覆盖率均为 1.0，全局相似度 0.9905，候选/最终差异 16/16，结果 JSON 86,286 字节。
- 与旧 2,099 项相比减少 99.24%，但初次结果仍为 `RISK_FOUND`（3 HIGH、13 MEDIUM），未满足最终验收门槛。
- 安全诊断显示残余项主要来自 `<br>`、Markdown/LaTeX、表格阅读顺序和 1–2 字符 OCR 微小差异；不涉及数值 token 变化。
- 修正后对该任务已持久化差异片段进行离线安全回放，预计保留 3 项 LOW、0 HIGH、0 MEDIUM，结论应为 `REVIEW_REQUIRED`；没有输出合同文本，也没有再次调用外部 OCR。
- 临时工作目录为 0 条目；日志扫描确认 OCR Key、OCR 地址和合同正文标记均未出现。

## Docker 与运行状态

- API：运行且 healthy，映射 `127.0.0.1:8000`。
- Worker：运行，使用最新本阶段镜像。
- PostgreSQL：运行且 healthy，继续使用命名卷。
- 控制台：`http://localhost:8000/console/` 可访问。
- 最终是否保持运行：是。

## 重要决策

- 配对解析一致性优先于单文件文本层判断，PDF/PDF 必须双方 external `auto`。
- `pdfplumber` 只保留诊断用途，external 未配置或调用失败时正式任务安全失败。
- 外部解析器表达噪声可以规范化；双方 OCR 的微小非数值字符或阅读顺序差异仍保留为 LOW 人工复核，不宣称 PASS，也不升级为业务风险。
- 真实调用次数遵守“一次任务、两次 external auto”的约束；修复后不擅自增加真实调用。

## 已知问题与风险

- 修正后的 0.4.0 尚未再次执行 46 页真实 external 任务；离线回放不能替代最终端到端复验，因此 PR 必须保持 Draft。
- 真实任务中 external 返回的表格数量为 6 对 9，虽然门控能安全降级，但复杂表格位置精度仍需人工视觉核对。
- 当前没有可连接浏览器，最新中文控制台的可靠性标签、warning、多位置和分页需人工检查。
- 约 184–200 页性能、异步 OCR 和甲方 Docker 网络仍按范围推迟。
- 全仓库 Ruff 有 43 个既有行长问题；本阶段变更范围 Ruff 已通过。

## 下一步建议

1. 获准后只重跑一次相同 46 页任务，确认 0.4.0 最终结果为 `PASS/REVIEW_REQUIRED`、0 HIGH、0 MEDIUM、覆盖率不低于 0.90、差异不超过 50，再将 PR 标记 Ready。
2. 浏览器人工检查可靠性标签、warning 聚合、OCR 解析模式、坐标、多位置和 20 项分页。
3. 为真实对应 DOCX/扫描 PDF 建立主黄金集；当前 PDF/PDF 负样本只证明一致解析和降噪能力。
4. 0.4.0 合并后再评估约 200 页同步耗时及是否需要异步 OCR，不并行启动 DRAFT_REVIEW 或 LLM。

## 下一会话首先阅读

- `docs/progress/20260820-155700_final-compare-alignment.md`
- `docs/progress/20260820-150430_diff-explosion-diagnosis.md`
- `docs/progress/20260820-151201_document-parser-routing-decision.md`
- `app/documents/router.py`
- `app/comparison/reliable.py`
- `app/workflows/final_compare.py`
- `scripts/e2e_ocr_acceptance.py`

## 交接摘要

FINAL_COMPARE 0.4.0 的一致解析、N:M 对齐、表格门控和可靠性保护已完成，Docker 全量测试 74 项通过。
唯一 46 页任务双方均 external auto、46/46 页、覆盖率 100%，总耗时 88.3 秒，差异由 2,099 降到 16。
首次结果仍误升为 RISK_FOUND；已修复解析器标记噪声和 OCR 微小差异分级，离线回放预计为 3 LOW、0 HIGH/MEDIUM。
因真实调用次数已用尽，修正后的端到端复验未执行，PR 应保持 Draft。
API、Worker、PostgreSQL 最终保持运行，API healthy；命名卷保留。
