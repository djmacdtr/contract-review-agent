# FINAL_COMPARE Advice 分批与再生 Canary 记录

## 状态

BLOCKED（Advice-only 再生未创建；唯一 Canary 未达到发布门禁）。

## 实现与离线门禁

- 新增共享 Advice 分批协调器：初始每批最多 8 项，缺失/不合格项最多一次 4 项恢复，逻辑调用上限 48，并发上限 2。
- 标准 FINAL_COMPARE 工作流改用逐项接收；单批失败不会丢弃其他批次，未解决项目保留确定性 fallback。
- 新增私有 Advice-only 任务服务、路由和宿主机脚本；来源任务结果只读，文件引用重映射，风险 ID 与差异 ID保持稳定。
- 不修改 OCR、比对算法、页码计算或公开接口。
- 定向测试：70 passed；Ruff、compileall、`git diff --check` 通过。

## 唯一 Advice Canary

来源任务：`tsk_01M1BBHY5424N69QRDFA8N96VZ`。

执行命令：

`python scripts/regenerate_final_compare_advice_host.py --canary-only --output tmp/final-compare-advice-canary-20260831.json`

本次只复制来源报告前 8 项风险并发起 1 次 Advice 请求，未创建任务、未写入来源结果：

- HTTP：`200`
- `finish_reason`：`stop`
- 逻辑调用：`1`
- 返回建议：`8`
- 共享校验接受：`6`
- `NOT_SPECIFIC`：`2`
- fallback：`2`
- `DUPLICATED`、`INTERNAL_ID`、`TECHNICAL_TERM`、`MULTI_SENTENCE`：均为 0
- OCR 调用：`0`
- 差异/页码调用：`0`

6/8 的模型覆盖率为 75%，低于至少 95% 的发布门禁。因此按计划停止，不创建 Advice-only 再生任务，不重复 Canary，不调用 retry。

## 服务与保护

- 来源任务保持只读，未创建新任务。
- Docker Worker 已恢复运行；API healthy，PostgreSQL healthy。
- 未保存模型建议正文、合同正文、完整响应、URL 或凭据。
- 保留现有工作区、历史报告、`backups/`、`tmp/` 和 `.real-diagnostic-temp/`；未执行 reset、clean、push 或 commit。

## 未完成项

Advice 发布门禁未通过，最终报告路径不存在。后续需在新的授权轮次中针对 `NOT_SPECIFIC` 的具体性判定或模型输出继续定位；本轮不再进行外部调用。
