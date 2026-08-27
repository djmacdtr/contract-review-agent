# Numeric 小批次 Canary 与唯一 Retry 验收

日期：2026-08-27

## Canary

来源任务：`tsk_01M10YQ3Z99FB3AP5PAKN5PNE5`

目标文件 SHA-256：`730e27c9305053bb047014efb75bb88db3b6ba45aba46f13337c84a25fd0b228`

在不修复历史 checkpoint、不增加恢复预算的前提下，严格命中检查后从 91 个 numeric 缓存未命中批次中选择输入规模最大的 3 个新批次。三批均为 6 个结构单元，numeric candidate 数分别为 8、9、8；每批只调用一次 LLM，共 3 次，均通过完整候选分类和证据回查。

Canary 批次：

- `batch_ceef60557b7e21c399c4536a`：6 单元、8 候选
- `batch_ab7b7f2fec2b317d555fd966`：6 单元、9 候选
- `batch_b703f09b8d9faf8fb165fc7b`：6 单元、8 候选

Canary 未写入 checkpoint、未下载文件、未调用 OCR、未创建任务。

## 唯一 Retry

宿主机 Worker 启动日志确认：`json_schema`、native structured output、numeric unit limit 6 生效；本次 Worker 使用本地文件服务，页码开关开启，OCR 重试为 0。

唯一 POST：

- 来源：`tsk_01M10YQ3Z99FB3AP5PAKN5PNE5`
- 新任务：`tsk_01M1127FS0ACRW0A3T0EBCMR63`
- POST request id：`req_01M1127FQS40BFD72HKVM17D6T`
- 之后只使用 GET 轮询，未调用 retry，未创建第二个任务。

三份文件均由本地只读文件服务返回 200。任务随后在 `FACT_EXTRACTION / 75%` 失败。

首个安全错误摘要：

- `failure_stage=FACT_EXTRACTION`
- `chain=numeric`
- `file_id=fil_01M10YQ3Z99FB3AP5PAKN5PNE64`
- `batch_id=batch_d29dfe2c9f5429fc72f03f26`
- `batch_depth=0`
- `unit_count=6`
- `failure_code=DYNAMIC_CHECK_INCOMPLETE`

Worker 日志与任务 GET 返回一致；本轮不继续追查或重试该任务。

## 结果与恢复

正式任务未达到 `SUCCEEDED / COMPLETED / 100`，因此 39 项差异、4 项通过、页码、局部高亮和建议覆盖率未完成验收。失败任务没有发布正式结果。

本轮已停止宿主机 Worker 和本地文件服务，并恢复 Docker Worker；Docker API、Worker、PostgreSQL 保持运行，交付 `.env` 的正式文件域名白名单未被持久化修改。未执行全量测试、commit、push、reset、clean，也未清理 `.real-diagnostic-temp/`。

## 未完成项

该唯一 retry 的 numeric 失败仍只暴露泛化 `DYNAMIC_CHECK_INCOMPLETE`，需要下一阶段从该新批次的 Worker 安全诊断链继续定位；本轮不再调用外部 LLM/OCR。
