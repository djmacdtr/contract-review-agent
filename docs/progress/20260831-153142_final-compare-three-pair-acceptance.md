# FINAL_COMPARE 三组真实验收记录

## 结果

本轮 OCR 恢复后，三组固定配对均通过公开接口创建并完成，未调用 retry，未重复创建同一配对任务。

- 总状态：`SUCCEEDED`
- 任务创建数：`3`
- 创建接口：`POST /api/v1/final-comparisons`
- 三组任务均：`SUCCEEDED / COMPLETED / 100%`
- 三组结果均成功持久化，状态与结果接口 HTTP 200
- 三组均 `alignment_reliable=true`
- 所有公开证据页码覆盖完整，未回退到段落号、表格号或估算页码

## 任务与报告

| 组别 | 任务 ID | 风险/差异 | 通过项 | 页码覆盖 | 印章图片 | 控制台报告 |
| --- | --- | ---: | ---: | --- | ---: | --- |
| 1 融资租赁合同 | `tsk_01M1BBHY5424N69QRDFA8N96VZ` | 189 / 189 | 0 | 640 / 640 | 22 | `/console/#/tasks/tsk_01M1BBHY5424N69QRDFA8N96VZ/report` |
| 2 租赁物转让合同 | `tsk_01M1BBKRJM1H4HD8M81Z9593YQ` | 38 / 38 | 1 | 134 / 134 | 12 | `/console/#/tasks/tsk_01M1BBKRJM1H4HD8M81Z9593YQ/report` |
| 3 保证合同 | `tsk_01M1BBMT7E3293M8GN828Y9VS1` | 26 / 26 | 1 | 82 / 82 | 12 | `/console/#/tasks/tsk_01M1BBMT7E3293M8GN828Y9VS1/report` |

任务详情控制台路径分别为：

- `/console/#/tasks/tsk_01M1BBHY5424N69QRDFA8N96VZ`
- `/console/#/tasks/tsk_01M1BBKRJM1H4HD8M81Z9593YQ`
- `/console/#/tasks/tsk_01M1BBMT7E3293M8GN828Y9VS1`

## 缓存与调用

恢复 Canary 对第 2 组基线 DOCX 发起 1 次 OCR 请求并成功返回 HTTP 200。随后正式三组串行预热新增 3 次 OCR 请求：第 2 组 TARGET 1 次，第 3 组 BASELINE/TARGET 各 1 次；第 1 组未新增预热请求。三组业务任务内 OCR transport 均为 0。

三组任务的 LLM 安全统计如下：

- 第 1 组：HTTP 200 请求 1 次，`finish_reason=stop`；
- 第 2 组：HTTP 200 请求 1 次，finish reasons 均为 `stop`；
- 第 3 组：HTTP 200 请求 1 次，finish reasons 均为 `stop`。

Advice 结果：

- 第 1 组：189/189 非空，模型建议 0，fallback 189；存在 `LLM_ADVICE_UNAVAILABLE`，文案质量应标记为 PARTIAL。
- 第 2 组：38/38 非空，模型建议 38，fallback 0。
- 第 3 组：26/26 非空，模型建议 26，fallback 0。

## 文件与缓存安全摘要

六份文件 SHA-256 已在脚本 JSON 摘要中记录且互不重复；正式任务均生成 2 个新文件 ID，未设置 `source_task_id` 或历史兼容参数。页码/印章缓存均按文件内容与解析模式使用，未复用旧任务业务结果、风险、差异或建议。

本轮脚本输出：

- `tmp/final-compare-first-ocr-canary-20260831-recovery.json`
- `tmp/final-compare-three-pair-20260831-recovery.json`
- `tmp/final-compare-three-pair-20260831-recovery.md`

## 服务与止损

- Docker Worker 在业务执行期间停止，宿主机 Worker 顺序执行三组任务。
- 三组结束后 Docker Worker 已恢复运行；API healthy，PostgreSQL healthy。
- 未清空数据库，未修改已有报告，未执行 reset、clean、push 或 commit。

## 未完成项

第 1 组 Advice 全部使用 fallback，虽然报告成功且建议非空，但未达到模型建议质量完全通过；需要后续独立质量改进轮次处理。本轮不重跑任务、不追加外部调用。
