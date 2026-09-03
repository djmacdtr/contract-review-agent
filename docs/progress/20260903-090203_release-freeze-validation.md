# 交付封版验证记录

## 结论

当前工作区已完成稳定版收敛、一次真实 FINAL_COMPARE 验收、隔离全量回归、生产镜像构建与服务冒烟，可进入 Git 封版提交。

## 真实验收

- 任务：`tsk_01M1GNVTXSE194YC1RW7RB16TR`
- 状态：`SUCCEEDED / COMPLETED / 100%`
- 结果：81 项差异/风险，公开页码证据 `222/222`，印章图片 22 张
- Advice：`81/81` 非空，其中模型建议 71、fallback 10
- 外部调用：OCR 0 次；LLM 17 次且 HTTP 均为 200
- 任务为全新任务，`source_task_id=null`，仅执行第一组，未创建第二个任务
- 控制台：`/console/#/tasks/tsk_01M1GNVTXSE194YC1RW7RB16TR/report`

## 封版门禁

- 结构化抽取定向测试：`55 passed`
- Compose 隔离全量测试：`568 passed, 1 warning`
- 普通交付命令 `docker compose --profile tools run --rm test`：`568 passed, 1 warning`
- 当前变更范围 Ruff：通过
- Python compileall：通过
- 前端 format、typecheck、build：通过
- `git diff --check`：通过（仅换行转换提示）
- 生产 API/Worker 镜像：构建成功

全仓 Ruff 仍有 11 个既有告警，位于本轮未修改的旧文件；遵循“不扩大改动范围”约束未做无关格式清理。新建及本轮修改的 Python 文件均通过 Ruff。

## 测试隔离修复

Compose `test` 服务原先会继承生产 `.env` 的 DOCX 页码、LLM 重试及响应模式配置，造成测试误入外部解析路径并产生大量假失败。现已仅在 `test` 服务中显式恢复代码默认测试参数，生产 API/Worker 配置未改变。

## 服务冒烟

- `/health`：HTTP 200
- `/ready`：HTTP 200
- `/console/`：HTTP 200
- 验收任务详情：HTTP 200
- 验收任务结果：HTTP 200
- PostgreSQL、API、Worker：运行正常

## 保护项

- 未清空或迁移生产数据库
- 未修改历史成功报告
- 未清理 `backups/`、`.real-diagnostic-temp/` 或现有诊断材料
- 提交时排除业务验收 JSON、备份、缓存和临时目录
