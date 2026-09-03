# Profile 请求降压与标准 Worker 收口

## 目标

针对标准 DRAFT_REVIEW 冷启动在 `FACT_EXTRACTION / profile` 阶段长时间无响应的问题，复用此前成功链路中“减少 LLM 压力”的有效做法，同时保留旧版高质量事实抽取、动态检查项、映射和 Advice 流程。

## 实现

- Profile 概览增加独立配置：
  - `LLM_PROFILE_MAX_OVERVIEW_BLOCKS=32`
  - `LLM_PROFILE_MAX_OVERVIEW_CHARS=4000`
- 概览超限时保留开头和结尾结构单元，避免只保留中间条款。
- 标准旧版 map-reduce、独立 map-reduce、checkpoint 预检均使用同一 Profile 限制。
- Profile 请求显式关闭模型思考模式；Numeric/Text/Mapping/Advice 逻辑和预算未改动。
- `.env` 与 `.env.example` 已同步配置。

## 验证

- `tests/unit/test_draft_facts.py`、`tests/unit/test_openai_llm_client.py`：`96 passed`
- `tests/unit/test_draft_review_workflow.py --basetemp=.test-temp-profile`：`4 passed`
- Ruff：通过
- compileall：通过
- `git diff --check`：通过
- 真实 275 结构单元合同零外部调用测量：Profile 概览由约 `6000 字符/44 块` 收缩至约 `2917 字符/32 块`。
- API `/health`：HTTP 200；未创建或重试业务任务，未调用 OCR/LLM。

## 部署前唯一动作

使用本机宿主机 Worker（Docker 无法访问甲方内网 LLM/OCR）创建一次全新控制台任务并观察完整链路。若仍失败，只记录首个安全错误，不自动 retry 或创建第二任务。

## 未执行

- 未修改数据库、未清库。
- 未 commit、push、reset 或 clean。
- 未宣称真实甲方闭环已通过。
