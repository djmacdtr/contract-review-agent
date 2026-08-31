# FINAL_COMPARE 三组验收：串行缓存预热与首文件 OCR Canary

## 状态

BLOCKED（验收脚本已修正；首个受控 OCR Canary 被上游服务拒绝，未创建业务任务）。

## 范围与保护

- 分支：`feat/draft-review-multidoc`
- 当前 HEAD：`6d8166dabddee45c2be541c32e7a8a7c4f13d636`
- 本轮仅修改三组 FINAL_COMPARE 公共验收脚本；未修改 FINAL_COMPARE 生产算法、公开接口或数据库结构。
- 保留工作区既有修改、`backups/`、`tmp/` 和 `.real-diagnostic-temp/`；未执行 reset、clean、push 或删除操作。

## 脚本收敛

`scripts/final_compare_three_pair_public_acceptance.py` 已完成：

- 六份文件按固定配对串行预热，预热并发固定为 1。
- 缓存错误按配对、角色和文件记录，区分解析/页码/印章模式；日志只保留安全错误类型、错误码和统计。
- 缓存门禁改为逐配对：一组准备完成后才允许该组创建任务，单组失败不阻塞其他组。
- 同一 SHA、解析模式和 `include_stamp_images` 组合在本轮最多执行一次 OCR 请求。
- 新增 `--ocr-canary-only`，只对首个缺失缓存执行一次精确 OCR Canary；失败不创建 FINAL_COMPARE 任务。

## 唯一 Canary 结果

命令：

`python scripts/final_compare_three_pair_public_acceptance.py --ocr-canary-only --output tmp/final-compare-first-ocr-canary-20260831.json`

首个缺失项为第 1 组 TARGET《金坛东旭农业-融资租赁合同（回租）.pdf》，使用专用 `scan` OCR 模式和 `include_stamp_images=true`。本轮仅发起 1 次 OCR 请求：

- HTTP 状态：`504`
- 安全错误码：`OCR_SERVICE_UNAVAILABLE`
- 上游分类：`UPSTREAM_504`
- 请求次数：`1`
- LLM 调用：`0`
- 结果：缓存未生成，停止后续验收

未保存 OCR 正文、完整响应、URL、密钥或合同内容。

## 检查与服务

- 变更脚本 Ruff：通过
- 变更脚本 compileall：通过
- `git diff --check`：通过（仅保留既有换行格式提示）
- 未调用公开 FINAL_COMPARE 创建接口，因此没有新任务 ID、报告地址或业务结果可验收。
- Canary 结束后 Docker Worker 已恢复；API 与 PostgreSQL 健康，Worker 运行中。

## 未完成项

待甲方 OCR 服务恢复后，在新的明确授权轮次中重新执行受控缓存预热/Canary；本轮不重试该 504、不创建三组任务、不调用 LLM。服务恢复后仍应按三组逐配对门禁执行，并分别记录成功或首个安全错误。
