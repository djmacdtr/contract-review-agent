# 历史起草检查报告恢复记录

## 结果

历史数据库任务行无法原样恢复，但遗留抽取 checkpoint 可用。已通过正常 Worker、Repository 和结果持久化链路重新生成一份可在控制台查看的真实起草检查报告。

- 恢复任务：`tsk_01M12YAHV8G4N9Y4CEZ1YZEXV7`
- 状态：`SUCCEEDED / COMPLETED / 100%`
- 结论：`RISK_FOUND`
- 风险项：40
- 校验通过项：1
- 事实矩阵：493
- 非空 AI 建议：40/40
- API 任务详情、结果接口与控制台入口：均验证通过

## 恢复方式

1. 恢复前先执行 PostgreSQL 完整备份。
2. 核对旧成功任务 `tsk_01M0Z48FK9QFS0J83HV14GMNP0` 遗留的 114 条成功 checkpoint 与三份原始文件 SHA-256 一致。
3. 使用旧 checkpoint 建立恢复来源，并以当前版本 checkpoint 增量补齐不兼容分片。
4. 为避免甲方 OCR 和 DOCX 页码解析阻塞，本次恢复明确关闭 OCR 与页码 sidecar，仅使用本地 DOCX 解析；未修改生产默认配置。
5. 任务越过 `FACT_EXTRACTION` 后完成跨资料映射、差异汇总、AI 建议和正式结果持久化。
6. 恢复成功后导出完整结果 JSON，并再次执行 PostgreSQL 完整备份。

## 演示入口与备份

- 控制台报告：`http://127.0.0.1:8000/console/#/reports/draft/tsk_01M12YAHV8G4N9Y4CEZ1YZEXV7`
- 完整结果：`backups/tsk_01M12YAHV8G4N9Y4CEZ1YZEXV7_result.json`
- 恢复前数据库快照：`backups/contract_review_20260828-083935.dump`
- 恢复后数据库快照：`backups/contract_review_restored_20260828-092123.dump`

## 边界说明

- 该报告是基于相同三份脱敏真实文件重新生成的正式结果，并非伪造或手工拼接旧报告。
- 由于本次以尽快恢复演示为目标，未执行 DOCX 真实页码补全；报告内容、差异和 AI 建议可正常展示，页码可在后续独立补充。
- 未运行会访问数据库的集成测试，未执行 `commit`、`push`、`reset` 或工作区清理。
