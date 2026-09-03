# 三组 FINAL_COMPARE 放款阶段验收记录

- 时间：2026-09-01T11:20:56+08:00
- 状态：SUCCEEDED
- 当前提交：55d356d20d19bc90b3159655ac71ec2eb717cc20
- 任务创建：仅使用公开 `POST /api/v1/final-comparisons`，每组最多一次
- 视觉验收：由用户在控制台完成

## 固定配对结果

| 组 | 任务 ID | 状态 | 差异 | 风险 | 通过 | OCR 调用 | LLM 调用 | 页码覆盖 | 印章图片 |
|---:|---|---|---:|---:|---:|---:|---:|---|---:|
| 1 | tsk_01M1DF90EP4SD6K1C8E39T9YQK | SUCCEEDED | 186 | 186 | 0 | 0 | 45 | 594/594 | 22 |
| 2 | tsk_01M1DFN51DNXE43NVYGRPVA81A | SUCCEEDED | 41 | 41 | 1 | 0 | 11 | 144/144 | 12 |
| 3 | tsk_01M1DFPWBKDMMPZT48ASZKQ55P | SUCCEEDED | 26 | 26 | 1 | 0 | 5 | 82/82 | 12 |

## 缓存与预检

- 服务预检：`{"active_task_count": 0, "docker": {"api": {"available": true, "health": "healthy", "running": true}, "postgres": {"available": true, "health": "healthy", "running": true}, "worker": {"available": true, "health": null, "running": true}}, "health_status": 200, "passed": true, "ready_status": 200}`
- 缓存预检：`{"1": {"all_ready": true, "docx_sidecar_hits": 1, "docx_sidecar_total": 1, "items": [{"cache_mode": "auto", "file_name": "融资租赁合同（回租）.docx", "include_stamp_images": false, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "ocr_cache_hit": true, "page_count": 25, "page_sidecar_hit": true, "pair_index": 1, "pair_name": "融资租赁合同（回租）", "role": "BASELINE", "sha256": "b8fa0231de6e161a147a065776e81a57a73ca0903f8236848aac6ee5481c8bb3", "stamp_image_count": 0}, {"cache_mode": "scan", "file_name": "金坛东旭农业-融资租赁合同（回租）.pdf", "include_stamp_images": true, "mime_type": "application/pdf", "ocr_cache_hit": true, "page_count": 27, "page_sidecar_hit": null, "pair_index": 1, "pair_name": "融资租赁合同（回租）", "role": "TARGET", "sha256": "dd35e05f1cced16cd0ccc7c3aa43dab22ccaa767f3e39af3462bcfacdfaa4690", "stamp_image_count": 22}], "ocr_cache_hits": 2, "ocr_cache_total": 2, "preheat_http_calls": 0, "preheated": [], "stamp_cache_hits": 1}, "2": {"all_ready": true, "docx_sidecar_hits": 1, "docx_sidecar_total": 1, "items": [{"cache_mode": "auto", "file_name": "租赁物转让合同（回租）.docx", "include_stamp_images": false, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "ocr_cache_hit": true, "page_count": 10, "page_sidecar_hit": true, "pair_index": 2, "pair_name": "租赁物转让合同（回租）", "role": "BASELINE", "sha256": "646e1a53f54415ff12ef41fb4890dd042b7f7766cf20bafbce69296c88108830", "stamp_image_count": 0}, {"cache_mode": "scan", "file_name": "金坛东旭农业-租赁物转让合同（回租）.pdf", "include_stamp_images": true, "mime_type": "application/pdf", "ocr_cache_hit": true, "page_count": 10, "page_sidecar_hit": null, "pair_index": 2, "pair_name": "租赁物转让合同（回租）", "role": "TARGET", "sha256": "8dd2f417cee13617a0c245933afe0ddf995b590e07613353d508dfc48ff4efef", "stamp_image_count": 12}], "ocr_cache_hits": 2, "ocr_cache_total": 2, "preheat_http_calls": 0, "preheated": [], "stamp_cache_hits": 1}, "3": {"all_ready": true, "docx_sidecar_hits": 1, "docx_sidecar_total": 1, "items": [{"cache_mode": "auto", "file_name": "保证合同.docx", "include_stamp_images": false, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "ocr_cache_hit": true, "page_count": 8, "page_sidecar_hit": true, "pair_index": 3, "pair_name": "保证合同", "role": "BASELINE", "sha256": "37a2c7030017ca28fa5a31396727393989df4f2d71027eeb95c79d0cdc6fe964", "stamp_image_count": 0}, {"cache_mode": "scan", "file_name": "金坛东旭农业-保证合同.pdf", "include_stamp_images": true, "mime_type": "application/pdf", "ocr_cache_hit": true, "page_count": 8, "page_sidecar_hit": null, "pair_index": 3, "pair_name": "保证合同", "role": "TARGET", "sha256": "c38921eeef9ef07e600f0bf44c94ea1ead40fec204502c6e068a3fb34db2aebd", "stamp_image_count": 12}], "ocr_cache_hits": 2, "ocr_cache_total": 2, "preheat_http_calls": 0, "preheated": [], "stamp_cache_hits": 1}}`

## 已知未完成项

- 控制台页面视觉、印章图片切换和建议语气由用户人工抽查。
