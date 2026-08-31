# 任务进度：三组 FINAL_COMPARE 放款阶段真实验收

## 基本信息

- 时间：2026-08-30 14:36:00 +08:00
- 状态：BLOCKED
- 任务类型：TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`6d8166dabddee45c2be541c32e7a8a7c4f13d636`
- 工作树状态：dirty；保留既有 DRAFT_REVIEW 改动、五文件验收脚本、备份和 `.real-diagnostic-temp/`

## 用户目标

通过公开 FINAL_COMPARE 接口，按固定顺序对三组起草 DOCX 与盖章 PDF 创建全新任务，使用正式 OCR/页码/印章路径生成可在控制台查看的报告。

## 本次完成

- 新增独立验收脚本 `scripts/final_compare_three_pair_public_acceptance.py`。
- 脚本使用公开 `POST /api/v1/final-comparisons`，不调用 retry、历史任务或私有创建方法；每组仅允许一次创建。
- 固定六份文件已存在，六个 SHA-256 均唯一，配对和 MIME 检查通过。
- 缓存审计严格区分 DOCX `auto`/普通 OCR、DOCX `docx-page-location-v2` sidecar 以及 PDF `scan + include_stamp_images=true` 专用 OCR 缓存。
- 脚本启动失败和 OCR 预热失败均保留安全类型/错误码传播，不输出正文、响应、URL 或凭据。
- 生成了预检/执行安全摘要：`20260830-143354_final-compare-three-pair.json` 及同名 Markdown。

## 修改文件

- `scripts/final_compare_three_pair_public_acceptance.py`：新增三组公开 FINAL_COMPARE 验收、内容缓存审计、宿主机 Worker 执行、结果安全摘要和服务恢复逻辑。
- `docs/progress/20260830-143600_final-compare-three-pair.md`：记录本次实现和真实预检阻塞。

## 接口、数据和配置变化

- API：未修改；脚本只调用公开 FINAL_COMPARE 创建接口和 GET 查询。
- 数据库/迁移：未修改；预检只读缓存和任务状态，未创建业务任务。
- 配置：未修改 `.env`；脚本通过运行时 Settings 覆盖宿主机下载白名单、OCR、页码、LLM 和数据库地址。
- 兼容性：未改变 DRAFT_REVIEW、FINAL_COMPARE 工作流或公开结果 Schema。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| `ruff check scripts/final_compare_three_pair_public_acceptance.py` | 通过 | 新脚本无 Ruff 错误 |
| `python -m compileall -q scripts/final_compare_three_pair_public_acceptance.py` | 通过 | 语法检查通过 |
| `git diff --check` | 通过 | 仅有既有换行格式提示，无 whitespace 错误 |
| `\.venv\Scripts\python.exe ... --preflight-only` | 阻塞 | API/PostgreSQL/Worker 健康、活动任务 0；缓存仅 1/6 完整 |
| `\.venv\Scripts\python.exe ... --output ...` | 安全停止 | 缺失缓存预热调用 OCR 2 次后返回 `WorkflowError`；未创建 FINAL_COMPARE 任务、LLM 调用 0 |

## 缓存与真实运行状态

- 六份文件：文件存在、SHA 唯一、配对正确。
- OCR/页码缓存：初始审计 `1/6` OCR 解析缓存命中、DOCX sidecar `1/3` 命中；三份带印章 PDF 专用缓存均未命中。
- 外部调用：本轮预热实际尝试 OCR 2 次；由于“同一 SHA/解析模式整轮最多一次”门禁，未重试已尝试资源；未创建任务，LLM 0 次。
- Docker Worker：脚本执行期间停止，预热失败后自动恢复；最终 API 和 PostgreSQL healthy，Worker running。
- 控制台：无新任务，暂无报告路径。

## 重要决策

- 未使用普通 PDF OCR 缓存冒充 `include_stamp_images=true` 专用缓存。
- 缓存预热失败后没有创建任何业务任务，也没有重复调用 OCR、调用 retry 或放宽页码/印章门禁。
- 之前的执行摘要是在错误码传播修复前生成，只有 `WorkflowError` 类型和安全调用计数；后续脚本已补齐 `WorkflowError.code/details` 的安全传播，但本轮不再次调用外部 OCR。

## 已知问题与风险

- 三份盖章 PDF 专用 OCR 缓存、两份 DOCX sidecar/解析缓存仍未完成；首次预热的 2 次 OCR 未形成可用缓存，具体安全子码未由旧版本摘要保留。
- 因缓存门禁未通过，三组 FINAL_COMPARE 任务创建数为 0，未执行业务差异、Advice、页码或印章图片验收。
- 当前结果不能宣称放款阶段真实闭环完成。

## 下一步建议

1. 先由运行环境定位并明确首次 OCR 预热返回的安全子码；按当前轮次规则不要重试已尝试的 SHA/模式。
2. 在获得新的外部验收授权和可用缓存后，重新执行脚本的唯一三组任务流程。
3. 成功后再记录每组任务 ID、差异/风险/通过项、Advice、页码覆盖、印章图片数和控制台路径。

## 下一会话首先阅读

- `AGENTS.md`
- `scripts/final_compare_three_pair_public_acceptance.py`
- `docs/progress/20260830-143600_final-compare-three-pair.md`

## 交接摘要

已新增三组 FINAL_COMPARE 公开验收脚本并通过静态检查。六份文件校验通过，服务健康，但内容缓存预热首次 OCR 尝试在 2 次调用后以 `WorkflowError` 停止，严格门禁阻止创建业务任务。Docker Worker 已恢复运行，API/PostgreSQL healthy，LLM 调用 0，现有工作树未清理或回退。
