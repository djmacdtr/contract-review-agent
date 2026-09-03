# FINAL_COMPARE 稳定版验收预检

## 状态

READY：验收脚本已切换为表格稳定版独立开关，并完成零任务、零外部调用预检；尚未创建真实任务。

## 本轮修改

- `scripts/final_compare_three_pair_public_acceptance.py` 显式启用 `FINAL_COMPARE_LOGICAL_V2_ENABLED` 和 `FINAL_COMPARE_EQUIVALENT_FILTER_ENABLED`。
- 显式关闭 `FINAL_COMPARE_LLM_ADJUDICATION_ENABLED`，不再使用旧的候选验证兼容开关。
- 从正式验收脚本移除人工金标 dry-run、重复簇 Canary 及相关报告字段；缓存门禁、逐组执行和首个失败即停止逻辑保持不变。
- 增加 `--pair-limit {1,2,3}` 任务数量门禁，默认行为仍为三组；下一轮显式使用 `--pair-limit 1`，确保只创建首组唯一任务。

## 检查结果

- Ruff：通过。
- compileall：通过。
- `git diff --check`：通过，仅有既有换行策略提示。
- `runtime_settings` 断言：逻辑 V2 开启、确定性过滤开启、LLM 差异裁决关闭。
- `--preflight-only`：`PREFLIGHT_PASSED`。
- OCR 缓存：6/6；DOCX 页码 sidecar：3/3；印章缓存：3/3。
- API、PostgreSQL healthy，Worker 已启动；活动任务 0。
- 任务创建 0，OCR 调用 0，LLM 调用 0，数据库写入 0。

## 下一步

仅执行第一组融资租赁合同真实 FINAL_COMPARE 验收；成功后核对 81 项附近的稳定结果、12 项确定性过滤、100% 页码覆盖、Advice 覆盖及控制台无风险等级展示，再进入封版检查。未执行 commit、push、reset 或 clean。
