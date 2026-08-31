# FINAL_COMPARE Advice 质量门禁与受控恢复记录

## 本轮变更

- Canary 逻辑调用上限由 1 调整为 2，允许首批 8 项及一次最多 4 项恢复批次。
- 完整 Advice 在内存中生成，只有模型覆盖率达到 `0.95` 才允许创建再生任务。
- 再生执行器保留发布前覆盖率门禁，准备结果路径不重新调用 Advice。
- 未修改 OCR、比对、页码、Prompt、Schema、公开接口或来源报告。

## 离线检查

- 定向 pytest：`70 passed`。
- Ruff：通过。
- compileall：通过。
- `git diff --check`：通过。

## 唯一外部 Canary

- 来源任务：`tsk_01M1BBHY5424N69QRDFA8N96VZ`
- 任务创建数：`0`
- OCR 调用：`0`
- Canary HTTP 调用：`2`
- HTTP 状态：`200=2`
- finish reason：`stop=2`
- 初始批次：`1`
- 恢复批次：`1`
- 风险数：`8`
- 模型接受：`7`
- fallback：`1`
- 模型覆盖率：`0.875`
- 安全质量拒绝计数：`NOT_SPECIFIC=3`，其余分类为 `0`

## 结论

Canary 未达到本轮要求的 `8/8`，因此未执行完整 189 项内存预检，也未创建新的 Advice-only 任务。Docker Worker 已恢复；来源报告未修改。后续需针对该单项建议取得明确的补偿结果后，才能重新进入完整发布门禁；本轮不再调用外部服务。
