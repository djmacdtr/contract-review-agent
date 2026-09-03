# 三组 FINAL_COMPARE 放款阶段验收记录

- 时间：2026-09-02T17:07:01+08:00
- 状态：SUCCEEDED
- 当前提交：55d356d20d19bc90b3159655ac71ec2eb717cc20
- 任务创建：仅使用公开 `POST /api/v1/final-comparisons`，每组最多一次
- 视觉验收：由用户在控制台完成

## 固定配对结果

| 组 | 任务 ID | 状态 | 差异 | 风险 | 通过 | OCR 调用 | LLM 调用 | 页码覆盖 | 印章图片 |
|---:|---|---|---:|---:|---:|---:|---:|---|---:|
| 1 | tsk_01M1GNVTXSE194YC1RW7RB16TR | SUCCEEDED | 81 | 81 | 0 | 0 | 17 | 222/222 | 22 |

## 缓存与预检

- 服务预检：`{"active_task_count": 0, "docker": {"api": {"available": true, "health": "healthy", "running": true}, "postgres": {"available": true, "health": "healthy", "running": true}, "worker": {"available": true, "health": null, "running": true}}, "health_status": 200, "passed": true, "ready_status": 200}`
- 缓存预检：`{"1": {"all_ready": true, "docx_sidecar_hits": 1, "docx_sidecar_total": 1, "items": [{"cache_mode": "auto", "file_name": "融资租赁合同（回租）.docx", "include_stamp_images": false, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "ocr_cache_hit": true, "page_count": 25, "page_sidecar_hit": true, "pair_index": 1, "pair_name": "融资租赁合同（回租）", "role": "BASELINE", "sha256": "b8fa0231de6e161a147a065776e81a57a73ca0903f8236848aac6ee5481c8bb3", "stamp_image_count": 0}, {"cache_mode": "scan", "file_name": "金坛东旭农业-融资租赁合同（回租）.pdf", "include_stamp_images": true, "mime_type": "application/pdf", "ocr_cache_hit": true, "page_count": 27, "page_sidecar_hit": null, "pair_index": 1, "pair_name": "融资租赁合同（回租）", "role": "TARGET", "sha256": "dd35e05f1cced16cd0ccc7c3aa43dab22ccaa767f3e39af3462bcfacdfaa4690", "stamp_image_count": 22}], "ocr_cache_hits": 2, "ocr_cache_total": 2, "preheat_http_calls": 0, "preheated": [], "stamp_cache_hits": 1}}`

## 已知未完成项

- 控制台页面视觉、印章图片切换和建议语气由用户人工抽查。
