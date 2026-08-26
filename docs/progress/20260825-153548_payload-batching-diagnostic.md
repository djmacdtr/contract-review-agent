# 任务进度：按最终 Payload 分批与受控二分诊断

## 基本信息

- 时间：2026-08-25 15:35:48 +08:00
- 状态：PARTIAL
- 任务类型：FIX / DIAGNOSE
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`797d06d fix(draft-review): classify llm response failures`
- 工作树状态：dirty；包含本阶段分块、预算、测试和本记录修改；未修改 `.real-diagnostic-temp`。

## 用户目标

按最终序列化抽取 payload 大小、普通段落/表格行单元和数值候选数进行分批；单批非法 JSON 只允许一次结构纠错，仍失败时仅二分当前批次，并限制拆分深度和每文档调用预算。

## 本次完成

- 新增内部抽取配置：最终 payload 上限 24000 字符、最大拆分深度 3、每文档抽取请求预算 16 次。
- 保留正文初步分块上限 12000 字符和每批 48 个数值候选。
- 表格在事实抽取路径按行生成独立单元，保留 `table_index/row/column`；程序侧补充行级证据回查。
- payload 上限使用与模型用户消息一致的紧凑 JSON 序列化计数，不截断、不静默丢弃。
- 抽取失败仅对 `LLM_INVALID_JSON` 的当前批次进行局部二分；Schema、证据、文件身份和 envelope 错误不触发二分。
- 事实抽取最多执行一次结构纠错；内部 `LlmClientError` 仅增加请求次数和纠错次数元数据，不改变公开异常或结果契约。

## 修改文件

- `app/core/config.py`、`.env.example`：新增抽取 payload、拆分深度和每文档预算配置。
- `app/draft_review/facts.py`：增加表格行单元、最终 payload 计数、payload 分批和行级证据索引。
- `app/adapters/llm/openai_client.py`：抽取纠错上限为一次，并保留安全请求计数。
- `app/workflows/draft_review.py`：增加失败批次队列、局部二分和安全聚合日志字段。
- `tests/unit/test_draft_facts.py`、`tests/unit/test_openai_llm_client.py`、`tests/unit/test_draft_review_workflow.py`：增加大型表格、行证据、纠错、二分、预算和非 JSON 错误回归。

## 接口、数据和配置变化

- API：未改变。
- 数据库/迁移：未改变。
- 公开结果 Schema：未改变。
- FINAL_COMPARE、确定性文字差异逻辑和业务检查类型：未改变。
- 内部配置：新增 `LLM_EXTRACTION_PAYLOAD_MAX_CHARS`、`LLM_EXTRACTION_MAX_SPLIT_DEPTH`、`LLM_EXTRACTION_MAX_REQUESTS_PER_DOCUMENT`。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 定向事实、LLM client、DRAFT_REVIEW、模板、比较、结果、风险和建议 pytest | 通过 | 160 passed；1 个既有 LangGraph 弃用告警 |
| 变更 Python 文件 Ruff | 通过 | 无 lint 错误 |
| 变更 Python 文件定向 compileall | 通过 | 无编译错误 |
| `git diff --check` | 通过 | 无空白错误 |
| 全仓 pytest / Docker / OCR / MiniMax | 未执行 | 按范围排除 |

合成门覆盖大型多行表格 payload 上限、行/单元格位置、数值候选上限、批次合并、非法 JSON 局部二分、最大深度、每文档预算和非 JSON 响应错误。

## 唯一真实三文件诊断

已启动且仅启动 1 次固定三文件诊断，目标合同、同名模板和 `项目方案确认函.docx` 均来自既定脱敏真实合同目录；配置为 4096 输出 token、结构纠错 1 次、OCR 关闭、正文上限 12000、payload 上限 24000、拆分深度 3、每文档预算 16 次。

- 诊断尝试次数：1。
- 安全采集器在底层响应等待期间卡住，未输出安全聚合结果；实际 LLM 请求次数、finish reason、usage、响应字符数和阶段完成状态均未可靠记录，不能据此推断。
- 已立即终止该次运行，未重试、未继续后续阶段、未增加辅助资料。
- 未生成可验收的正式差异、双侧证据或 AI 建议；不将本次尝试表述为成功。
- 未输出或保存合同正文、事实值、完整模型响应、响应片段、密钥或签名 URL。

## 已知问题与风险

- 本阶段代码和合成回归通过，但真实三文件端到端链路尚未完成。
- 下一次真实运行前必须先修复并离线验证诊断采集器的底层响应关闭/资源释放路径；在获得新的真实授权前不重复调用。

## 下一步建议

1. 先修复真实诊断采集器的安全资源释放和阶段指标 flush，确保不会因采集器阻塞而重复消耗模型调用。
2. 复核本阶段未提交代码和本记录后，再另行申请一次真实三文件诊断。
3. 真实成功后再评估是否扩大辅助资料范围。

## 交接摘要

payload 分批、表格行证据和非法 JSON 局部二分已实现，160 项定向回归通过。唯一真实诊断已启动但因临时安全采集器卡住而停止，未获得可验证的真实链路结果；没有重试或第二次真实调用。工作区保留未提交修改，未推送。
