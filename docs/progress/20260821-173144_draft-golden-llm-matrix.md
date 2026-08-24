# 任务进度：起草黄金质量门、LLM 纵向切片与事实矩阵

## 基本信息

- 时间：2026-08-21 17:31:44 +08:00
- 状态：PARTIAL
- 任务类型：BUILD / TEST / DOCS
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`94471c03f7d6be0064b7bbbb6e749e9e2eaa945d`
- 工作树状态：dirty；仅包含本次黄金标注、LLM、事实矩阵、报告和测试改动，未提交

## 用户目标

实施既定后续路线：先建立仓库外黄金标注与规则质量门，再完成报告视觉收口、OpenAI 兼容 LLM 单文档抽取和跨文档事实矩阵，同时保持 Schema 2.0 无等级风险语义及安全降级。

## 本次完成

- 实现黄金候选稳定指纹、清单生成/刷新、样本哈希校验、漂移检测和实际规则输出校验；清单不含合同正文。
- 将真实 TARGET/TEMPLATE 基线导出为仓库外 29 个待标注候选，精确对应 26 个差异和 3 个扩展表格点。
- 扩展模板诊断，保留扩展表格索引以支持稳定标注和人工复核。
- 实现 OpenAI 兼容 LLM Client：模型探测、超时、网络/HTTP 重试、安全错误映射、JSON/Schema 重试和调用指标。
- DRAFT_REVIEW 按字符上限逐文件分块抽取；校验事实文件 ID、位置和证据文本确实存在于解析结果中，无效或失败结果安全降级为 review/warning。
- 实现金额、比例、日期、期限及文本/主体的程序规范化，构建 `CONSISTENT / CONFLICT / MISSING / UNCERTAIN` 事实矩阵。
- 来源冲突映射为无等级风险；缺失或单一/低置信来源映射为人工复核；不设置来源优先级。
- 新增事实矩阵报告组件；有效模型结果使用 `HYBRID`，无有效结果保持 `RULE_BASED`。
- 修复完整验收脚本：Compose 命令失败会传播，并在 API 冒烟前等待健康状态。
- 更新 README、配置示例、健康状态语义、工作流/规则版本和回归断言。

## 修改文件

- `app/draft_review/golden_annotations.py`、`scripts/draft_golden.py`、`docs/golden-annotations.md`：仓库外黄金标注协议与质量门。
- `app/adapters/llm/openai_client.py`、`app/draft_review/facts.py`、`app/workflows/draft_review.py`：LLM 纵向切片、证据校验、事实矩阵与降级。
- `app/schemas/results.py`、`frontend/src/components/report/FactMatrix.vue`：正式矩阵类型和报告展示。
- `tests/unit/test_*golden*`、`test_openai_llm_client.py`、`test_draft_facts.py` 及既有工作流/集成测试：新能力和回归覆盖。

## 接口、数据和配置变化

- API：路由和请求不变；Schema 版本保持 `2.0`，`fact_matrix` 从开放对象收紧为正式结构；`metadata.execution_mode` 新增 `HYBRID`。
- 数据库/迁移：无模型或迁移变化；Alembic 仍为 `0002_ungraded_risk_counts`。
- 配置：新增 `LLM_CHUNK_MAX_CHARS=12000`；健康状态只有在 LLM 已启用且 URL/Key 均完整时才报告 configured。
- Docker：Dockerfile/Compose/依赖未修改；重建 runtime/test 镜像以部署最终源码。
- 兼容性：Schema 1.0 旧结果兼容层保持不变；LLM 关闭或失败时继续生成现有规则结果。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 宿主机 `pytest tests/unit -q` | 通过 | `132 passed, 1 warning` |
| 变更范围 `ruff check ...` | 通过 | `All checks passed` |
| `python -m compileall -q app scripts tests` | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误；仅 Windows 行尾提示 |
| `npm run typecheck` | 通过 | 无 TypeScript/Vue 类型错误 |
| `npm run build` | 通过 | 1502 modules；仅既有约 1 MiB chunk warning |
| `draft_golden.py export` | 通过 | 29 个候选；文件 SHA-256 与既定黄金样本一致；正文未写入清单 |
| `draft_golden.py validate` | 待业务标注 | `INCOMPLETE`；29 项均未分类，0 stale，0 mismatch |
| LLM `/v1/models` 最小探测 | 失败并安全映射 | 未发送合同内容；返回 `LLM_UPSTREAM_ERROR`，未继续真实合同调用 |
| Docker runtime/test 构建 | 通过 | 最终镜像 `contract-review-agent:dev` / `:test` 构建成功 |
| Docker PostgreSQL 全量测试 | 通过 | 最终 `147 passed, 1 warning in 5.65s` |
| `alembic upgrade head` / `alembic check` | 通过 | `No new upgrade operations detected` |
| 首次完整脚本冒烟 | 失败并修复 | API 重建后未等待监听导致 connection refused；测试套件此前已通过 |
| 最终 API → Worker 冒烟 | 通过 | 任务 `tsk_01M0HTJ6EZXSBAPSNVYBA558CB`，SUCCEEDED/COMPLETED/100 |
| `/health`、`/ready` | 通过 | 均 HTTP 200 |
| 1440px / 1280px 浏览器视觉验收 | 未执行 | Browser 运行时无可用浏览器实例；未用 HTTP 200 冒充视觉通过 |
| 真实 LLM 合同抽取 / 完整五文件 OCR | 未执行 | 网关探测失败；PDF OCR 外部条件未恢复 |

唯一测试 warning 为 LangGraph 上游未来弃用提示，不影响当前运行。

## Docker 与运行状态

- API：最终 `contract-review-agent:dev` 镜像，healthy，`127.0.0.1:8000`。
- Worker：最终 `contract-review-agent:dev` 镜像，running。
- PostgreSQL：`postgres:16.10-alpine`，healthy；命名卷保留，未删除或清空。
- 控制台：已打包进最终 API 镜像；临时 Vite 服务已停止。
- 最终是否保持运行：是，API、Worker、PostgreSQL 均保持运行。

## 重要决策

- 黄金标注只保存于仓库外；Git 只保存格式、读取/校验器和测试。
- 黄金指纹由文件 SHA-256、双方位置、规范化内容哈希和差异类型组成，不依赖顺序型 `diff_id`。
- 业务分类不能由开发者猜测；未分类清单必须阻止黄金验收通过。
- LLM 自报证据不能直接进入结果，必须反查当前解析文档的位置和文本。
- 只有至少一份有效模型抽取时才标记 `HYBRID`；模型失败不能抹掉模板确定性结果。
- 只有明确的跨来源不同值产生 `SOURCE_CONFLICT` 风险；抽取不确定性只进入人工复核。

## 已知问题与风险

- 仓库外 `draft-review-golden-annotations.json` 的 29 个候选仍需业务人员分类，0.3.2 规则质量闭环尚未完成。
- 当前规则仍会将未经过黄金分类的 26 个模板差异作为风险输出；必须在业务标注后再针对可泛化模式收敛。
- LLM 模型列表探测返回上游错误，真实单文档抽取和人工证据验收被阻塞。
- 事实矩阵实现已完成并由 Mock/单元/工作流覆盖，但尚未取得真实模型与完整五文件效果基线。
- 浏览器运行时无可用实例，1280px/1440px 视觉验收仍待人工完成。

## 下一步建议

1. 业务人员完成仓库外 29 项 `RISK / ALLOWED_FILL / ALIGNMENT_FALSE_POSITIVE / MANUAL_REVIEW` 分类。
2. 运行黄金校验，根据 mismatch 只提炼通用允许填写/对齐规则，直到质量门通过。
3. LLM 网关恢复后重新执行一次 `/v1/models` 探测，再只用 `项目方案确认函.docx` 做首次真实抽取与人工证据核验。
4. 在可用浏览器中完成任务中心、起草报告和放款报告的 1440px/1280px 视觉清单。
5. OCR 服务恢复后运行一次完整五文件黄金任务，建立真实事实矩阵效果快照。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260821-173144_draft-golden-llm-matrix.md`
- `docs/golden-annotations.md`
- `app/workflows/draft_review.py`
- `app/draft_review/facts.py`
- `app/draft_review/golden_annotations.py`

## 交接摘要

DRAFT_REVIEW 已升级为 workflow 0.4.0 / rules 0.3.2。
黄金标注质量门、OpenAI Client、逐文件证据抽取、事实矩阵和报告展示均已实现。
仓库外已生成 29 项无正文待标注清单；业务分类尚未完成，因此规则质量里程碑仍为 PARTIAL。
宿主机 132 单元通过，最终 Docker PostgreSQL 147 全量通过，Alembic 与 API→Worker 冒烟通过。
LLM 模型探测为上游错误，未发送合同；真实 LLM 与完整 OCR 验收未执行。
浏览器不可用，桌面视觉验收待人工。
API、Worker、PostgreSQL 正运行，数据库卷未改动；未 commit、未 push。
