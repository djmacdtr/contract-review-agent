# 控制台任务长时间等待诊断

- 任务：`tsk_01M1G2AF8ABVMV6QE5QPB8ZZQ0`
- 初始状态：`PENDING / QUEUED / 0%`，原因是 Docker Worker 已退出且宿主机 Worker 未运行。
- 启动宿主机 Worker 后，任务自动从 stale 状态重新排队并被领取。
- 发现宿主机默认 `TEMP_ROOT=/tmp/contract-review` 在 Windows 下会卡住任务临时目录创建；改用项目内 Windows 可写目录后，下载阶段正常结束。
- 最终失败：`FACT_EXTRACTION / profile / 75% / LLM_NETWORK_ERROR`，未进入 Numeric/Text/Mapping/Advice。
- 对全部 5 个控制台上传 URL 的只读 HTTP GET 均为 HTTP 200；因此不是上传文件不可访问。
- `10.50.11.18:8080` TCP 探测失败，符合甲方 LLM 网关当前不可达/未监听的现象。
- 未创建新任务，未调用 retry；任务已达到 `attempt_count=2/2`。
