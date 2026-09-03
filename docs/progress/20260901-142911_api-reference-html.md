# 合同智能对比系统 API 接口文档交付记录

## 目标

参照桌面 `文档解析API接口文档.html` 的 ReDoc 单页格式，为当前合同智能对比系统整理一份可离线打开的 API 接口文档，并交付到桌面。

## 需求与决策

- 参考文件仅用于版式和信息架构分析，不复用其中的 TextIn 接口内容。
- 以当前工作树中 FastAPI 实际生成的 OpenAPI 3.1 契约为数据源，不手工臆造公开路由。
- 文档采用异步任务调用流程：创建任务（202）→ 查询状态 → 获取成功结果；失败任务可创建重试任务。
- 当前应用没有定义业务鉴权请求头；文档明确生产环境应由网关、反向代理或网络访问控制承接鉴权。
- 文件 URL、单文件大小和辅助资料数量说明与当前代码及默认配置保持一致。

## 实现范围

- 新增 `scripts/generate_api_reference.py`：
  - 从 `app.main.app.openapi()` 读取 8 个公开接口和完整 Schema。
  - 将路由分为健康检查、起草检查、定稿比对、任务管理四组。
  - 增补中文总览、调用流程、状态说明、通用请求头、请求/响应示例及 400/404/409/500/503 错误契约。
  - 复用参考文件中的 ReDoc standalone 运行库，输出不依赖 CDN 的单文件 HTML。
- 未修改后端路由、数据库、业务逻辑、前端或部署配置。

## 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查 |
| GET | `/ready` | 数据库就绪检查 |
| POST | `/api/v1/draft-reviews` | 创建合同起草检查任务 |
| POST | `/api/v1/final-comparisons` | 创建放款阶段比对任务 |
| GET | `/api/v1/tasks` | 查询历史任务 |
| GET | `/api/v1/tasks/{task_id}` | 查询任务详情 |
| GET | `/api/v1/tasks/{task_id}/result` | 获取任务结果 |
| POST | `/api/v1/tasks/{task_id}/retry` | 重试失败任务 |

## 验证

| 检查 | 结果 |
| --- | --- |
| 生成器 `py_compile` | 通过 |
| OpenAPI 路径集合断言 | 通过，8 个路径与代码一致 |
| Schema 数量 | 45（含文档补充的统一错误响应 Schema） |
| FastAPI 默认 422 与实际 400 错误处理校正 | 通过 |
| 重试接口 409 示例 | 通过，使用 `TASK_NOT_RETRYABLE` |
| HTML 单文件结构 | 通过，包含内嵌 ReDoc 与 OpenAPI 数据 |
| 桌面端浏览器渲染 | 通过，左侧目录/中间正文/右侧示例正常 |
| 移动端 390×844 渲染 | 通过，内容与示例区响应式排列正常 |
| 浏览器控制台 | 0 条 warning/error |
| 参考业务内容泄漏检查 | 通过，未包含 `TextIn Document Master` 规范内容 |

说明：项目虚拟环境未安装 Ruff 可执行文件，因此本次未执行 Ruff；生成器已通过 Python 编译检查并实际成功生成文档。

## 交付物

- 桌面文档：`C:\Users\ROG\Desktop\合同智能对比系统API接口文档.html`
- 可重复生成脚本：`scripts/generate_api_reference.py`
- 临时 OpenAPI/HTML 构建产物位于被忽略的 `tmp/api-docs/`，不作为源码交付。

## 环境与状态

- 文档基于当前未提交工作树生成，能够反映当前公开 Schema（含结果 `schema_version=2.1`）。
- 未启动、停止或重建项目 API、Worker、PostgreSQL、Docker 服务。
- 仅为视觉验收启动临时本地静态 HTTP 服务；交付后关闭。
- 未 commit、未 push；未触碰工作树中原有的其他修改和未跟踪文件。

## 后续注意事项

- 若公开路由、Pydantic Schema、错误处理、默认文件限制或鉴权方式发生变化，应重新运行生成脚本并复核文档。
- 生产部署地址、鉴权头和甲方网关策略确定后，可在下一版文档中替换当前的参数化 Server 地址及鉴权说明。
