# 任务进度：生产公开证据页码失败本地复现与根因定位

## 基本信息

- 时间：2026-09-04 11:12:29 +08:00
- 状态：ROOT CAUSE CONFIRMED / CANDIDATE FIX VERIFIED
- 分支：`feat/draft-review-multidoc`
- 生产版本：`2e96fbd`
- 生产任务：`tsk_01M1N45SAC8KS6BEHG2TM6CSGN`

## 生产失败结论

- 任务类型：`DRAFT_REVIEW`；在 92% 的公开证据页码门禁失败。
- 错误：`DOCX_PAGE_LOCATION_INCOMPLETE / PUBLIC_DIFF_PAGE_MISSING`。
- 页码覆盖：39/40；唯一缺失位置为文件 `fil_01M1N45SAC8KS6BEHG2TM6CSGS` 的 `table_index=0,row=2`。
- 不是 Nginx、Docker、数据库、OCR TCP 或 LLM TCP 的部署故障；生产主流程已运行到最终发布校验。

## 本地 Docker 恢复

- Docker Desktop 4.63.0 因损坏的 Windows Unix-socket reparse point 无法启动。
- 保留并改名了临时运行目录：
  - `C:\Users\ROG\AppData\Local\Docker\run.stale-20260904-1105`
  - `C:\Users\ROG\AppData\Local\docker-secrets-engine.stale-20260904-1110`
  - `C:\Users\ROG\AppData\Local\Docker\run.stale-20260904-1115`
- 原 Docker 设置备份：`C:\Users\ROG\AppData\Roaming\Docker\settings-store.before-disable-ai-20260904.json`。
- 仅将 `EnableDockerAI` 从 `true` 改为 `false`；Docker Desktop/WSL 后端恢复运行。
- 镜像、容器、卷和项目数据均未重置或删除。

## 网络实测

- 本地镜像：`contract-review-agent:2e96fbd`，Linux/amd64。
- 容器到 OCR `10.50.11.17:80` 和 LLM `10.50.11.18:8080` 的 TCP 建连均成功。
- 容器携带真实密钥访问 LLM `/v1/models`：HTTP 200。
- 容器执行标准 OCR PDF POST：上游 HTTP 502，`OCR_SERVICE_UNAVAILABLE`。
- Windows 宿主机执行相同 OCR 探针：`NETWORK_ERROR`；因此 OCR 阻断不属于 Docker NAT 特有问题。
- `.wslconfig` 当前不存在，尚未启用 mirrored；由于宿主机本身无法完成 OCR 请求，单独启用 mirrored 不能保证解决。

## 追加发现

- 生产 `fil_01M1N45SAC8KS6BEHG2TM6CSGS` 实际是参考 PDF `评审会评审意见表（对内版).pdf`，不是 DOCX；失败位置是该 PDF 的 `table_index=0,row=2`。
- 本机 `04 合同素材文件` 已找到四份同名候选输入；SHA-256 分别为目标 `b8fa0231...`、模板 `5b732086...`、合规报告 `1ef98af1...`、评审意见 PDF `b90721c5...`。
- 本地数据库已确认上述四个哈希均有 `sys_ocr_cache_v1 / ocr-parsed-document-v1` 成功缓存；三个 DOCX 另有页码 Sidecar。
- 本地历史成功任务 `tsk_01M16XN8BFR11RPP7Y4RZR36KE` 包含上述四份文件和额外的 `项目方案确认函.docx`，说明可基于缓存构造精确的四文件生产变体回归。
- 用户确认生产任务就是通过上述四份本地原文件上传创建，无需再次比对生产 SHA。

## 精确四文件复现结果

- 本地使用与生产相同的镜像 ID；`contract-review-agent:2e96fbd` 与本地 `dev` 标签均指向 `sha256:699d4c25...`。
- 通过与 Web 控制台相同的上传接口创建四文件任务：`tsk_01M1N730BK6VY38GA2571J5FT3`。
- 四个上传响应的 SHA-256 与本地候选输入完全一致。
- 任务成功完成：27 项风险/差异，公开页码覆盖 `78/78`，Advice `27/27`（模型 23，安全回退 4）。
- 任务成功并不否定生产故障：本地本次 LLM 输出没有把 PDF 第 2 行选成正式事实冲突，因此未生成以该 PDF 行为 baseline 的公开差异。

## 根因

- 为使 LLM 抽取检查点可跨分页复用，抽取载荷会主动去除 `page`。
- 抽取结果回填时，普通段落/单元格依赖原位置或 DOCX Sidecar 恢复页码；PDF 的整行表格证据只恢复了文本，没有从 OCR 单元格恢复唯一物理页。
- 当 LLM 偶发地把 PDF `table_index=0,row=2` 选为事实冲突时，`fact_conflict_diff_items` 会将这个无 `page` 的位置发布到差异 baseline，最终被 `PUBLIC_DIFF_PAGE_MISSING` 正确拦截。
- 因此故障是一个依赖 LLM 选择结果才暴露的确定性页码回填缺口，不是 Docker、Nginx、文件损坏、OCR 服务或 LLM 网络故障。

## 候选修复与验证

- 为解析文档建立逻辑位置到物理页的映射；仅在解析器给出唯一物理页时回填，跨页歧义仍保持失败门禁，不猜测页码。
- 覆盖新抽取、批次检查点复用和整文档检查点复用；PDF 表格行、表格单元格及段落均可复用同一机制。
- 新增回归用例，直接构造“PDF 行证据成为事实冲突 baseline”的生产失败分支，并验证公开页码门禁通过。
- 定向测试：`70 passed`。
- 完整测试：按 Compose 测试环境执行，`570 passed`；仅有既存 LangGraph 弃用预告。
- 候选修复目前只在本地工作区，尚未提交、构建镜像或更新甲方服务器。

## 下一步

1. 审阅并提交候选修复。
2. 构建新业务镜像，并在本地再次执行四文件真实回归。
3. 制作只包含新业务镜像与升级说明的离线增量包。
4. 在甲方服务器保留现有 `2e96fbd` 回滚点，加载新镜像、重建 API/Worker，并重跑原四文件任务验收。
