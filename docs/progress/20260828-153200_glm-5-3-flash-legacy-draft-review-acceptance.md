# GLM-5.3-Flash 旧版 DRAFT_REVIEW 验收记录

日期：2026-08-28

## 本轮范围

- DRAFT_REVIEW 业务版本恢复为 `0.7.0 / rules 0.6.0`。
- 恢复旧版动态事实抽取、一次跨资料映射、程序化比较和通过项生成路径。
- 保留文档解析缓存、LLM 全局并发闸门、JSON Schema 支持和页码外围代码。
- 抽取、评审、建议模型统一为 `GLM-5.3-Flash`；本机和示例配置的 LLM 并发均为 3。
- 本轮正式验收关闭 OCR 与 DOCX 页码 sidecar；未修改 API、数据库 Schema 或 `.real-diagnostic-temp/`。

## 离线门禁

- 直接相关后端单元测试：`90 passed`。
- `compileall`：通过。
- 变更相关 Ruff：通过。
- `git diff --check`：通过。

## 模型 Canary

使用无合同正文的合成 payload，仅执行模型列表检查、FACT_MAPPING 和 RISK_ADVICE 各一次：

- `/v1/models` 包含 `GLM-5.3-Flash`，可用模型数量为 5。
- 两次 Canary 均 HTTP 200，`actual_model=GLM-5.3-Flash`，`finish_reason=stop`，首次请求成功且无结构纠错重试。
- FACT_MAPPING 返回 2 项，RISK_ADVICE 返回 2 项。
- Canary 总 HTTP 请求数：3（模型列表 1、业务 Canary 2）。

## 唯一正式任务

- 来源 checkpoint：`tsk_01M0Z48FK9QFS0J83HV14GMNP0`。
- 正式任务：`tsk_01M13MBF329VRRKTKA4DRR0K04`。
- 通过宿主机 Worker 执行；Docker Worker 在任务期间暂停，文件服务在任务结束后关闭。
- 结果：`FAILED`，阶段 `FACT_EXTRACTION`，进度 `75`。
- 首个安全错误：`DYNAMIC_CHECK_INCOMPLETE` / `FACT_EXTRACTION` / `numeric` / `NUMERIC_CANDIDATE_UNCLASSIFIED`。
- 失败批次结构单元数：6；LLM HTTP 调用数：9，HTTP 状态均为 200；OCR 调用数：0。
- 未进入映射、建议、结果持久化或控制台报告验收；未执行 retry，未创建第二个正式任务。
- 由于任务失败，本轮无法确认 39 项差异、4 项通过、建议覆盖率、控制台页面和 GLM 正式结果统计。

## 环境恢复

- API、PostgreSQL、Docker Worker 均已恢复运行并健康。
- Worker 实际读取：`GLM-5.3-Flash`、LLM 最大并发 3、抽取任务并发 3、`json_schema`、原生结构化输出开启。
- OCR 和页码 sidecar 保持关闭；正式下载白名单保持甲方文件域名，不包含 `fixture-server` 或通配符。

## 未完成项

GLM-5.3-Flash 下的旧版 DRAFT_REVIEW 三文件正式成功闭环尚未完成。当前应保留失败任务和诊断文件，后续仅针对 `NUMERIC_CANDIDATE_UNCLASSIFIED` 做定向分析；不得把本轮结果宣称为正式交付闭环。
