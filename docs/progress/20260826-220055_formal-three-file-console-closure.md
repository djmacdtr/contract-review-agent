# 正式三文件任务与控制台展示验收

日期：2026-08-26

## 结论

正式 API → PostgreSQL → Worker → 控制台闭环已完成。固定三份脱敏 DOCX 均进入正式任务，任务最终为 `SUCCEEDED / COMPLETED / 100%`，`task_result` 已持久化。

本次任务：`tsk_01M0Z48FK9QFS0J83HV14GMNP0`

业务关联 ID：`formal-draft-review-20260826-213054`

创建时间：2026-08-26 21:30:55（Asia/Shanghai）

完成时间：2026-08-26 21:55:54（约 1499.7 秒）

## 实际配置与架构

- Compose 透传 `LLM_RESPONSE_FORMAT` 与 `LLM_NATIVE_STRUCTURED_OUTPUT`；本次运行使用已验证的 `json_schema` 和原生结构化输出。
- `LLM_FACT_REVIEW_ENABLED=false`、`LLM_MAPPING_REVIEW_ENABLED=false`、`LLM_SEMANTIC_PLAN_ENABLED=false`；结果元数据为 `independent_review=false`、`review_mode=NOT_RUN`。
- `fixture-server` 以只读方式挂载 `D:\work\contract_review\脱敏真实合同`，未映射宿主机端口；Worker 使用 Docker 内部 `fixture-server:8080` 访问文件。OCR 未启用。
- 起草报告页恢复“检出风险、删除 / 缺失、新增 / 变更、校验通过”四个 Tab；未修改公开结果 Schema 或 `FINAL_COMPARE` 逻辑。
- checkpoint 读取修复为当前任务优先、源任务回退，并在 SQL 查询层匹配 `payload_digest`。同主键但不同输入摘要的旧部分结果不会覆盖，也不会阻断源 checkpoint 读取。

## 任务与结果统计

- 数据库确认：3 条文件记录、39 条任务事件、1 条持久化 `task_result`。
- API 结果：Schema `2.1`、`mock=false`、`execution_mode=HYBRID`。
- 文件：目标合同、合同模板、项目方案确认函共 3 份，文件身份和 SHA-256 均可追溯。
- 确定性模板差异：39 项，与上一闭环统计一致。
- 正式风险：39 项；全部关联有效差异证据，全部有一句 `analysis_advice`。
- 校验通过：4 项。
- 事实矩阵：298 项，其中 `CONSISTENT=3`、`MISSING=295`；295 条内部缺失未膨胀为用户风险。
- 差异位置：39 项均有至少一侧有效文件位置，其中 21 项具有双侧位置；新增 / 删除类差异按可用侧展示。
- `SEMANTIC_PLAN`：0 次。

## checkpoint 与调用统计

- 源任务 `real_8c2e88a0e5a807fb` 的成功 checkpoint 库存为 64 条。
- 本正式任务按文件哈希、批次、版本和 payload 摘要精确命中并复用了 51 条源 checkpoint；其余 13 条因历史输入计划摘要不同未复用，未被强行套用。
- 成功执行段的抽取结果均为 checkpoint 命中：目标 `FACT_EXTRACTION` 有效 chunk 30、模板 0、辅助资料 31，抽取请求尝试数均为 0，恢复次数为 0。
- 成功执行段新增 LLM 逻辑调用 2 次、HTTP 请求尝试 2 次：`FACT_MAPPING=1`、`RISK_ADVICE=1`；事实评审、映射复核和语义规划均为 0。
- 需要特别区分：同一正式任务此前为定位环境 / checkpoint 问题曾产生 63 条本任务非源成功抽取 checkpoint（profile 3、numeric 19、text 41）。这些前置尝试的失败传输请求没有单独持久化计数，无法从现有结果安全还原精确 HTTP 总数；因此本记录将“成功执行段 2/2”与“前置尝试已有 checkpoint”分开记录，不将本任务宣称为全程仅 2 次调用。

## 验证命令与结果

已执行的直接相关检查：

```text
python -m pytest tests/unit/test_draft_review_checkpoints.py tests/unit/test_draft_review_workflow.py -q
48 passed, 1 warning

ruff check app/draft_review/checkpoints.py app/draft_review/extraction.py tests/unit/test_draft_review_checkpoints.py
All checks passed

python -m compileall -q app scripts
通过

git diff --check
通过
```

此前本阶段已完成的前端检查：`npm run typecheck` 通过，`npm run build` 顺序重跑通过，仅有 chunk size warning。正式服务健康，`/console/` 和 `/console/#/reports/draft/tsk_01M0Z48FK9QFS0J83HV14GMNP0` 均返回 HTTP 200。

未运行全仓 pytest、OCR、五文件 / 200 页任务，也未进行 Docker 全链路部署验收。未删除 `.real-diagnostic-temp/`、历史锁或历史 checkpoint。

## 控制台访问

- 任务中心：`http://127.0.0.1:8000/console/#/tasks`
- 正式起草报告：`http://127.0.0.1:8000/console/#/reports/draft/tsk_01M0Z48FK9QFS0J83HV14GMNP0`
- 报告组件只消费业务风险、差异、证据、位置和建议；源码已恢复四个 Tab，不在正式报告主体展示模型、checkpoint、置信度或内部诊断字段。

## 未完成项与下一步

- 需要在真实浏览器中由验收人员目视确认任务列表、四个 Tab、文件名、位置和局部差异高亮；本会话仅完成静态构建与 HTTP 路由检查。
- Worker 心跳仍受同步 DOCX 解析耗时影响，本次正式任务使用了临时 `WORKER_STALE_AFTER_SECONDS=900` 运行配置；后续部署前应单独修复解析阶段的心跳可观测性。
- 当前历史 checkpoint 的部分批次仍依赖验收任务中对齐的内部文件身份；后续应将稳定批次身份与任务级文件 ID 完全解耦，再进行通用恢复验收。
- 五文件、OCR、Alembic / Docker 完整部署、API/Worker/控制台冒烟及甲方内网联调转入下一独立交付验收阶段。
