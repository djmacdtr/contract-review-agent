# Numeric 小批次定向恢复诊断

日期：2026-08-27

## 范围

本轮只实现 numeric 小批次规划、截断恢复和只读定向诊断；未创建新任务，未调用 retry，未调用 OCR，未修改公开 API、差异算法、事实身份或 checkpoint 表结构。

## 离线实现

- numeric planner 将结构单元批次限制为最多 6 个，candidate 上限 24 仍独立保留。
- 密集表格按行、单元格递归拆分；长文本按句子、条款或安全文本边界拆分，不能安全拆分时保留 `NUMERIC_BATCH_TOO_LARGE`。
- `LLM_OUTPUT_TRUNCATED` numeric 恢复固定为 `6 → 3 → 1`，每个子批次重新生成 payload 和 batch identity；单结构无法继续拆分时不原样重试。
- 失败诊断保留安全 `batch_id`，Worker 结构化日志同步记录；未写入正文、请求/响应、URL 或密钥。
- 新增 `scripts/numeric_recovery_diagnostic.py`：仅使用本地 `python-docx`、只读来源任务和严格 checkpoint 命中查询；无法唯一确定历史失败批次时安全停止。

## 离线验证

- Compose test 容器：`384 passed`。
- 宿主机 unit tests：`373 passed`；本机集成测试的 16 个错误均为测试进程使用 Docker 内部主机名 `postgres` 的既有环境问题。
- numeric/extraction 定向测试：`44 passed`。
- Ruff、compileall、`git diff --check`：通过。

## 唯一来源诊断

来源任务：`tsk_01M10YQ3Z99FB3AP5PAKN5PNE5`

诊断文件 SHA-256：`730e27c9305053bb047014efb75bb88db3b6ba45aba46f13337c84a25fd0b228`

来源目标文件：`fil_01M10YQ3Z99FB3AP5PAKN5PNE6`

安全摘要：

- `profile-v2` checkpoint：3
- `numeric-v2` checkpoint：16
- 严格命中：1（含 profile 命中）
- numeric 规划批次：91
- numeric 缓存未命中：91
- 最大规划结构单元数：6
- LLM 调用：0
- 首个安全阶段：`NUMERIC_BATCH_RECONSTRUCTION`
- 首个安全错误码：`NUMERIC_FAILED_BATCH_NOT_UNIQUE`

来源任务的错误详情只有 `chain=numeric`、`batch_depth=1`、`unit_count=10` 和 `LLM_OUTPUT_TRUNCATED`，没有历史 `batch_id`。来源事件也没有补充批次标识；在当前严格 6 单元规划下无法从安全信息唯一重建旧的 10 单元失败分支。因此按门禁安全停止，没有猜测位置/顺序、没有调用 LLM，也没有执行 retry。

## 未完成项

唯一 retry、39 项差异/4 项通过、页码/高亮/建议覆盖率和控制台验收尚未执行；需要后续任务在错误详情包含可靠 `batch_id` 后继续。`.real-diagnostic-temp/` 未修改，未执行 commit、push、reset 或 clean。
