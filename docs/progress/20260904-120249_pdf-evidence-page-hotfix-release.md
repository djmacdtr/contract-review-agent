# 任务进度：PDF 证据页码修复与生产增量封版

## 基本信息

- 时间：2026-09-04 12:02:49 +08:00
- 状态：OFFLINE RELEASE PACKAGED / PRODUCTION DEPLOYMENT PENDING
- 分支：`feat/draft-review-multidoc`
- 原生产版本：`2e96fbd`
- 修复代码提交：`ccee853215ef67a364736039a851d2b8e39087ba`
- 远端同步：`origin/feat/draft-review-multidoc` 已包含修复与本封版记录。
- 新业务镜像：`contract-review-agent:ccee853`
- 目标平台：openEuler 24.03 LTS SP4 x86_64 / Docker 29.7.2 / Compose 5.5.0

## 生产故障与根因

- 甲方任务 `tsk_01M1N45SAC8KS6BEHG2TM6CSGN` 在 92% 失败。
- 错误为 `DOCX_PAGE_LOCATION_INCOMPLETE / PUBLIC_DIFF_PAGE_MISSING`，页码证据覆盖 39/40。
- 唯一缺失位置实际来自参考 PDF `评审会评审意见表（对内版).pdf` 的 `table_index=0,row=2`，并非 DOCX。
- LLM 抽取检查点为保证可复用会去除 `page`；旧逻辑能恢复证据文本，但未从 OCR 表格单元格为“整行证据”恢复唯一物理页。
- 当该行被 LLM 选为正式事实冲突时，无页码位置进入公开差异，最终被公开证据页码门禁正确拒绝。

## 修复内容

- 在解析文档上建立逻辑位置到解析器物理页的索引。
- 对段落、表格单元格和整行证据统一恢复页码。
- 仅在逻辑位置唯一对应一个物理页时绑定；跨页歧义不猜测页码，继续由发布门禁拒绝。
- 新抽取、分批检查点和整文档检查点复用路径均执行页码恢复。
- 新增生产形态单元测试：PDF 行证据成为事实冲突 baseline 时，公开差异页码门禁必须通过。

## 自动化验证

- 定向单元测试：`70 passed`。
- Compose 全量测试：`570 passed`，仅一个既存 LangGraph 弃用预告。
- 修改文件 Ruff 静态检查：通过（Ruff 0.15.5）。
- Python `compileall`：通过。
- `git diff --check`：通过。
- 生产镜像构建：通过。
- 镜像 ID：`sha256:cdd033d4b1abba67072a74408ae41367a22c73966a5e4db4d535b8c3f473e3e0`。
- 镜像平台：`linux/amd64`；Docker 展开大小：174,415,669 bytes。

## 精确四文件真实回归

- 新镜像任务：`tsk_01M1N8M404GC27TRY43N4T7NH2`。
- 状态：`SUCCEEDED / COMPLETED / 100%`，一次执行成功。
- 开始：2026-09-04 03:50:36 UTC；结束：2026-09-04 03:58:48 UTC。
- 输入与生产失败任务为同一组本地原文件，上传 SHA-256：
  - 目标 DOCX：`b8fa0231de6e161a147a065776e81a57a73ca0903f8236848aac6ee5481c8bb3`
  - 模板 DOCX：`5b73208658f27993d5433ac4a738851aca973aee98dc73abbe81bfdfd5f3f4ef`
  - 合规报告 DOCX：`1ef98af1f65045f0613ce1eae590c142a1c9e9816bcf0403229eb41c3dbb66a2`
  - 评审意见 PDF：`b90721c55f6c8d6a3c34f72b3d0cb640f4072426c6dc9bd13f0d6c8308deacb3`
- 输出：27 项差异/风险。
- 公开页码证据：要求 78、覆盖 78、缺失 0。
- Advice：27/27；模型建议 25，确定性兜底 2。
- 文件页数：目标 25、模板 24、合规报告 7、评审意见 PDF 1。
- OCR 解析器：`textin-document-parser`；LLM：`GLM-5.3-Flash`。
- 本次回归复用了既有文档缓存，直接覆盖了旧检查点“无页码后恢复”的关键路径。

## 生产增量离线包

- 文件：`backups/contract-review-agent-ccee853-linux-amd64-offline-upgrade.tar.gz`
- SHA-256 文件：`backups/contract-review-agent-ccee853-linux-amd64-offline-upgrade.tar.gz.sha256`
- SHA-256：`d5ace775cce8c70303a99dee15cf41ca4b310b2a5a37fbb622bb34e8b4e47ade`
- 大小：173,713,947 bytes。
- 包含新业务镜像、Nginx:80 兼容 Compose、Nginx 配置、升级脚本、版本元数据和中文升级/回滚说明。
- 不包含 `.env`、Docker 安装包、PostgreSQL 镜像或 Nginx 镜像；复用甲方现有配置和已部署基础镜像。
- 最终归档已完成：外层 SHA-256 校验、重新解压、内层 `SHA256SUMS`、Linux `bash -n`、`docker load`、镜像 ID/平台校验。

## 甲方升级安全边界

- `upgrade.sh` 在替换文件前会备份现有 `compose.yaml`、`nginx.conf` 和版本文件到 `/opt/contract-review/backups/before-ccee853-时间戳/`。
- 升级保留 `/opt/contract-review/current/.env`、`contract-review-postgres-data` 和 `contract-review-upload-data`。
- 执行 `alembic upgrade head` 后只重建必要服务；不执行 `down -v`，不删除卷。
- 生产对外入口继续为 `http://10.50.199.89/`，仅 TCP 80；PostgreSQL 继续仅绑定 `127.0.0.1:15432`。
- 回滚恢复备份 Compose/Nginx 文件并 `docker compose up -d`，旧镜像 `contract-review-agent:2e96fbd` 保留。

## 跨会话接续

1. 先阅读本记录和 `20260904-111229_production-docx-page-repro-preflight.md`。
2. 代码修复以提交 `ccee853` 为准；不要重新实现同一修复。
3. 将增量包和外部 `.sha256` 上传甲方 `/root`，按包内 `README-upgrade-zh.md` 执行。
4. 升级后核对 `/health`、`/ready`、`/console/`、Compose 服务和监听端口。
5. 在甲方环境重新上传同一四文件创建任务；验收要求任务成功、公开页码缺失为 0、Advice 覆盖全部风险。
6. 本地真实回归结束后已停止临时 API/Worker，只保留原有 PostgreSQL 测试容器；未删除任何卷。
7. 工作区其他历史 JSON、旧封版记录和测试临时目录未纳入本次提交，也未清理。
