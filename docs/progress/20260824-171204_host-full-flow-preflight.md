# 任务进度：宿主机完整流程运行前置检查

## 基本信息

- 时间：2026-08-24 17:12:04 +08:00
- 状态：BLOCKED
- 任务类型：DIAGNOSE / TEST
- 代码目录：`D:\work\contract_review\contract-review-agent`
- 当前分支：`feat/draft-review-multidoc`
- 当前提交：`581ea08`
- 工作树状态：dirty；保留上一轮全部未提交功能修改，本次仅新增本进度记录

## 用户目标

在宿主机使用截图对应的同一组文件完整运行 DRAFT_REVIEW，启用真实 OCR 和全部 LLM 阶段，生成可在正式前端查看的新结果。

## 本次完成

- 定位截图对应的旧任务及 4 份同名脱敏文件：目标合同、合同模板和 2 份参考资料均已在宿主机找到。
- 确认这 4 份原始输入均为 DOCX；正式 DRAFT_REVIEW 会使用本地 DOCX 解析，按设计不会强制调用 OCR。
- 脱敏检查当前宿主机配置：OCR 与 LLM 均为 enabled/configured；抽取和建议模型为 `GLM-5.2`，默认评审模型仍为网关未登记的 `GLM-5.2-reviewer`。
- 本次探测使用进程级 `Gemma-4-31B` 作为独立评审模型候选，没有修改 `.env`。
- 使用既有单页合成扫描 PDF 重新执行真实 OCR `scan` probe，确认宿主机 OCR 链路可用且逐页解析完整。
- 依次执行无正文模型列表探测和最小合成 Advice completion；两次均安全失败为 `LLM_UPSTREAM_ERROR`，未发送合同正文。
- 因当前新工作流要求启用的事实抽取/评审/映射必须完成，未创建一条必然失败的真实合同任务，没有产生误导性的部分正式结果。

## 修改文件

- `docs/progress/20260824-171204_host-full-flow-preflight.md`：新增本次外部能力前置检查记录。

## 接口、数据和配置变化

- API、数据库、代码和 `.env`：无变化。
- 进程级探测配置：独立评审模型临时使用 `Gemma-4-31B`；进程结束后不保留。
- 外部数据：只发送最小合成 Advice 项；未发送合同正文、文件、URL 或凭据。

## 测试与验证

| 命令/检查 | 结果 | 关键数字或说明 |
|---|---|---|
| 宿主机 OCR `scan` probe | 通过 | 1 页、10 blocks、1 table、12 cells；engine `3.20.11`；最低置信度 `0.9987`；逐页覆盖完整 |
| LLM `/v1/models` | 失败 | `LLM_UPSTREAM_ERROR`，`retryable=true`；未发送正文 |
| LLM 最小 Advice completion | 失败 | `LLM_UPSTREAM_ERROR`，`retryable=true`；仅发送合成风险项 |
| 同组 4 文件真实任务 | 未创建 | LLM 前置门未通过，避免生成已知会失败的任务 |

## Docker 与运行状态

- Docker API、Worker、PostgreSQL 维持上一轮运行状态；本次未重启或停止。
- 未启动宿主机 API/Worker，也未建立数据库端口代理。
- OCR 调用来自宿主机；LLM 探测同样来自宿主机。

## 重要决策

- DOCX 输入不强制走 OCR；若要在同一业务任务中验证 OCR，需要明确把目标文件替换为对应 PDF，这将不再是截图中的完全相同输入集合。
- 启用能力未完成时任务必须失败，不能退回规则模式生成看似完整的新报告。
- 在模型列表和 completion 均失败后不发送真实合同，避免无意义外部调用和不完整任务记录。

## 已知问题与风险

- 当前 LLM 网关或其模型后端仍返回上游错误，完整事实抽取、独立评审、映射和 Advice 无法执行。
- `.env` 默认评审模型不是网关文档登记模型；网关恢复后仍需使用真实独立模型完成本次任务。
- 4 份同样文件均为 DOCX，因此该任务本身不能覆盖 OCR Adapter；OCR 已通过独立真实 probe 验证。

## 下一步建议

1. 平台侧恢复 LLM completion 后，先确认 `GLM-5.2` 与 `Gemma-4-31B` 均可用。
2. 模型门通过后仅创建一次同组 4 文件真实 DRAFT_REVIEW，持续监控到终态。
3. 如需报告任务本身同时包含 OCR，可另行确认是否将目标 DOCX 替换为对应脱敏 PDF。

## 下一会话首先阅读

- `AGENTS.md`
- `docs/progress/20260824-171204_host-full-flow-preflight.md`
- `app/adapters/llm/openai_client.py`
- `app/workflows/draft_review.py`

## 交接摘要

同组 4 份 DOCX 已定位，宿主机 OCR 真实 probe 成功。
LLM 模型列表与最小 completion 均返回可重试的 `LLM_UPSTREAM_ERROR`。
未发送合同正文，未创建必然失败的任务。
Docker 服务保持运行，代码和配置未改变。

## 17:19 后续执行补记

- 按用户要求改为完整的 Windows 宿主机执行链路：启动宿主机文件服务、FastAPI（端口 `8001`）和 Worker；Docker Worker 已停止，确保不会领取本次任务或从容器调用 LLM。
- Docker 仅继续提供 PostgreSQL，并通过临时端口转发供宿主机进程访问；合同解析、工作流、OCR 与 LLM 客户端均运行在宿主机 `.venv`。
- 使用同一组 4 份 DOCX 创建新任务 `tsk_01M0SGYZ95BXG1BQ53PWDGBTKQ`。任务由 `host-full-flow` 宿主机 Worker 领取，于 `TEMPLATE_COMPARE` 65% 失败，错误为 `COMPARISON_INCOMPLETE`：目标合同与模板存在无法可靠完成逐项检查的表格结构变化。
- 只读核对原文件表格形状：模板表格为 `5×7、24×2、5×7、6×5`，目标合同为 `117×11、24×2、117×11、12×5`。当前严格失败门命中的是实质性扩展，不应绕过后生成不完整正式报告。
- 任务尚未进入事实抽取阶段；随后再次从宿主机 `.venv` 直接调用 LLM 模型列表，仍得到 `LLM_UPSTREAM_ERROR`、`retryable=true`。因此即使解决表格检查能力，当前网关状态仍会阻止全 LLM 流程完成。
- 同组输入均为 DOCX，正式任务按格式不会进入 OCR 分支；本记录前述真实扫描 PDF OCR probe 已通过，不能将该独立连通性检查误写成这条 DOCX 任务的 OCR 阶段。
- 当前宿主机文件服务、API、Worker 和临时数据库转发保持运行，便于继续诊断；Docker Worker 保持停止。未修改 `.env`、业务代码或数据库 Schema。
