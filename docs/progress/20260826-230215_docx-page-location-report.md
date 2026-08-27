# DOCX 真实页码与差异证据展示进度

## 状态

部分完成，真实验收被页码能力验证阻断。

- 日期：2026-08-26
- 分支：`feat/draft-review-multidoc`
- 基线提交：`23246f2`
- 正式任务：未创建
- 既有三文件任务：`tsk_01M0Z48FK9QFS0J83HV14GMNP0`
- 保护项：未执行 commit、push、reset 或清理 `.real-diagnostic-temp/`

## 已完成实现

### DOCX 页码

- 新增内部配置 `DOCX_PAGE_LOCATION_ENABLED`，默认关闭。
- 开启后仍以 `python-docx` 作为正文、表格和逻辑位置的权威解析来源，并额外调用现有甲方文档解析适配器。
- 新增独立 sidecar，将外部 `page_id` 按规范化文字、块类型、顺序和重复文本的唯一单调路径映射回本地段落、表格及单元格。
- 支持跨页段落、多页范围、表格单元格和空单元格的表格页锚定；无法完整覆盖、页号不连续、无法唯一定位或外部服务失败时统一以 `DOCX_PAGE_LOCATION_INCOMPLETE` 安全失败。
- sidecar 只在建议生成完成后的结果持久化节点补充公开证据位置和文件页数，未提前改动事实 ID、逻辑 `location_key`、抽取 payload 或 checkpoint 状态。
- `DOCX_PAGE_LOCATION_ENABLED` 已接入配置示例、Compose 环境传递和文档说明。

### 前端

- 公共差异证据组件和任务详情差异卡移除“合同模板”“基准文件”“当前文件”“目标文件”等侧标题。
- 业务位置格式统一为 `《文件名》 · 第 N 页`，连续多页显示为 `第 N–M 页`；缺失内容使用页前、页后或页间表述。
- 差异卡不再显示段落、表格、行列、OCR、置信度等结构字段；这些信息仍保留在 JSON 追溯数据中。
- 保留双栏文本、局部差异片段高亮和卡片下方分析建议。

## 受控外部验证

使用单份脱敏 DOCX `融资租赁合同（回租）.docx` 调用甲方文档解析服务，配置门禁已确认存在服务地址、鉴权键和请求头。服务调用最终返回安全错误：

```text
DOCX_PAGE_LOCATION_INCOMPLETE：DOCX 真实页码解析或映射未能可靠完成
```

因此按门禁停止，没有创建正式三文件任务，没有切换到段落比例、文档属性总页数或固定段落数量估算，也没有进入五文件测试。本次服务调用摘要未带出更细的首个子错误码；探针现已保留安全的 `failure_code` 摘要字段，下一次具备外部服务调试权限时应先确认它是外部页号完整性、映射唯一性还是覆盖不足，再决定修正方向。

## 测试结果

通过：

- `pytest` 相关后端集合：`101 passed, 1 warning`
- `python -m compileall -q app tests scripts`
- 变更文件 Ruff：通过
- 前端 `npm run test:format`：通过
- 前端 `npm run typecheck`：通过
- 前端 `npm run build`：通过；仅有既有 chunk size warning
- `git diff --check`：通过

全量后端测试：`324 passed, 15 errors`。错误均发生在集成测试 fixture 连接宿主机数据库时，宿主机无法解析 Compose 内部主机名 `postgres`；未产生本次页码或前端断言失败。

## 未完成项

- 需要先定位并修复甲方服务返回结果的首个页码解析/映射失败点。
- 外部验证通过后，才可重建启用页码配置的 API/worker，并新建一次正式三文件任务。
- 正式任务需再次核对 39 项确定性差异、4 项校验、事实 ID/checkpoint 摘要与旧任务保持不变，并确认所有控制台证据侧有文件名和有效页码。
- 尚未进行浏览器视觉验收；当前完成的是静态格式、typecheck 和构建验证。

## 变更文件

- 后端：`app/documents/page_locations.py`、`app/documents/router.py`、`app/workflows/draft_review.py`、`app/workflows/final_compare.py`、`app/core/config.py`、`app/adapters/document_parser/textin_client.py`
- 前端：`frontend/src/utils/reportEvidence.ts`、差异/风险/复核/来源证据组件及任务详情视图
- 测试与探针：`tests/unit/test_docx_page_locations.py`、路由和配置单测、`frontend/tests/reportEvidence.test.ts`、`scripts/docx_page_location_probe.py`

## 下一步读取

继续工作前先阅读本记录、`AGENTS.md`、上一条正式三文件闭环记录，以及甲方 HTML 原型中的证据卡片相关片段。
