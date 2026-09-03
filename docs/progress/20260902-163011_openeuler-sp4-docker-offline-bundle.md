# openEuler SP4 Docker CE 离线安装包交付记录

## 目标

制作一套可上传到甲方 `openEuler 24.03 LTS SP4 x86_64` 服务器的 Docker CE 离线 RPM 安装包，并在交付前完成真正的断网安装与容器运行验收。

## 交付物

- 压缩包：`docker-ce-29.7.2-openeuler-24.03-lts-sp4-x86_64-offline.tar.gz`
- 外部校验文件：`docker-ce-29.7.2-openeuler-24.03-lts-sp4-x86_64-offline.tar.gz.sha256`
- 压缩包大小：125,875,232 bytes。
- SHA-256：`3e0d80c9b09caa40b74efdeb8d3ba8dfa7a28445de9894206bc156e8e39b1009`。
- 桌面副本与工作区源文件哈希一致。

## 包内内容

- 39 个 RPM，约 121 MB；系统依赖全部为 `.oe2403sp4`，Docker 包为 `.el9`。
- openEuler 与 Docker CE GPG Key。
- 47 项包内 SHA-256 校验清单。
- `install.sh`：系统/架构预检、哈希与签名校验、冲突检查、离线 DNF 安装、systemd 启动、hello-world 验收。
- `verify.sh`：安装后状态、版本、存储驱动、cgroup、SELinux 检查。
- 中文 `README-安装说明.md`、RPM Manifest 和构建元数据。
- linux/amd64 `hello-world:latest` 离线镜像；镜像摘要 `sha256:5dd0d3e6e255913fc30f90b9f2b1d359cc2cbdb48090cc4b65f1676e203243cc`。

## 精确版本

| 组件 | 版本 |
| --- | --- |
| Docker Engine | 29.7.2 (`3:29.7.2-1.el9`) |
| Docker CLI | 29.7.2 (`1:29.7.2-1.el9`) |
| containerd.io | 2.3.4 (`2.3.4-1.el9`) |
| Docker Buildx Plugin | 0.36.1 (`0.36.1-1.el9`) |
| Docker Compose Plugin | 5.5.0 (`5.5.0-1.el9`) |

## 下载与完整性验证

- 基于官方 `openeuler/openeuler:24.03-lts-sp4` linux/amd64 镜像构建。
- Docker CE RPM 来自清华 TUNA CentOS 9 stable 镜像；openEuler 系统依赖来自 SP4 `OS`、`update`。
- 显式补齐最小 SP4 基础镜像所需的 `dbus-libs`，最终形成 39 包依赖闭包。
- 39 个 RPM 全部通过 `rpm -K`，结果为 `digests signatures OK`。
- 未发现 `.oe2403sp1`、`.oe2403sp2` 或 `.oe2403sp3` 包。
- 47 项包内 SHA-256 全部通过。
- tar.gz 外部 SHA-256、解压、包内 SHA-256 和脚本 `bash -n` 全部通过。

## 强制断网验收

在全新的官方 SP4 x86_64 容器中使用 `--network none`，只读挂载离线包：

1. 校验包内 SHA-256；
2. 导入两套 GPG Key 并校验全部 RPM；
3. 使用 `dnf --disablerepo='*'` 完成 39 包实际安装；
4. 启动嵌套 Docker daemon；
5. 加载包内 hello-world 镜像；
6. 使用 `--network none` 成功运行容器并输出 `Hello from Docker!`。

最终版本输出：Client/Server 29.7.2、Compose 5.5.0、Buildx 0.36.1、containerd.io 2.3.4，系统识别为 openEuler 24.03 LTS SP4 x86_64、cgroup v2。

## 安全与安装行为

- 安装脚本不会关闭 SELinux、修改防火墙、修改 Docker 数据目录或设置 registry mirror。
- 检测到旧 Docker、`podman-docker`、系统 `containerd` 或 `runc` 时会停止，不会自动卸载。
- DNF 安装强制禁用全部在线 repo，只使用包内 RPM。
- 脚本会设置 Docker 开机启动并立即启动 daemon。

## 环境清理与运行状态

- 所有构建和验收容器均使用 `--rm`，最终无 SP4 临时容器残留。
- 未创建持久验证卷、网络或自定义镜像。
- 本机新增/更新了官方 `hello-world:latest` 镜像，作为离线验收镜像来源；可复用，不影响项目数据。
- 项目既有 API 与 PostgreSQL 容器保持运行且健康，未重启、停止或修改。
- 离线包源目录及归档位于被忽略的 `backups/`；未修改业务代码、API、数据库或部署配置。
- 未 commit、未 push。

## 生产服务器仍需验证

上传后仍需在真实服务器检查 systemd PID 1、SELinux Enforcing、overlay2、iptables/nftables、Docker 开机启动及重启后的容器运行。安装时先验证外部 `.sha256`，再解压并运行包内 `install.sh`；具体命令已写入包内中文说明，后续由当前会话逐步指导执行。

## 甲方服务器实际安装结果（2026-09-02）

用户在甲方服务器 `zjzl89` 返回的实际执行输出确认：

- 系统与架构：openEuler 24.03 LTS SP4 x86_64。
- 外层压缩包及包内 47 项 SHA-256 全部通过。
- 服务器安装前无 Docker、podman-docker、containerd 或 runc 冲突包。
- 39 包离线依赖闭包完成实际 DNF 安装；`diffutils` 与 `policycoreutils` 升级到包内 SP4 版本。
- Docker systemd 服务已创建开机启动链接并成功启动。
- 包内 hello-world 镜像加载成功，实际容器运行输出 `Hello from Docker!`。
- Docker Client/Server：29.7.2。
- Docker Compose：5.5.0。
- Docker Buildx：0.36.1。
- 实际存储驱动：overlayfs。
- 实际架构：x86_64。
- SELinux：Enforcing，安装过程未关闭。
- 主机当前使用 cgroup v1；Docker 29.7.2 可运行并仅给出 2029 年前计划移除支持的预告，不阻塞本项目当前部署，但应作为操作系统后续维护事项跟踪，不能在未评估的情况下直接修改内核启动参数。

RPM scriptlet 对 `restorecond.service` 的 daemon-reload 提示已由安装脚本随后执行的 `systemctl daemon-reload` 覆盖处理。以上结果来自用户回传的生产服务器控制台输出；仍建议在部署业务容器前完成一次 Docker 服务重启、开机启用状态和 daemon 日志复核。

### 重启与日志复核

用户随后执行 `systemctl restart docker`、`verify.sh`、离线 hello-world 运行及最近 10 分钟 daemon 日志检查，结果确认：

- Docker 重启后为 `active`，开机启动为 `enabled`。
- 重启后 Client/Server 仍为 29.7.2，Compose 5.5.0、Buildx 0.36.1。
- 重启后实际存储驱动为 overlayfs，Docker Root 为 `/var/lib/docker`，架构 x86_64，SELinux Enforcing。
- 重启后再次使用 `--network none` 成功运行 hello-world。
- daemon 两次均完成初始化并监听 `/run/docker.sock`，systemd 报告 `Started Docker Application Container Engine`；停止过程为正常 graceful shutdown。
- `failed check for fsverity support` 为底层文件系统不支持可选 fs-verity 能力，不影响 overlayfs 或容器运行。
- 删除不存在的 `docker-bridges` nftables 表为启动清理阶段的 info 级提示；随后 firewalld docker zone/forwarding policy 正常创建或复用，未阻塞网络初始化。
- cgroup v1 为已记录的弃用预告，不阻塞当前部署。
- 缺少 `git` 只禁用 BuildKit 的 Git source 功能；生产方案加载预构建离线镜像，不在服务器上从 Git 构建，因此不构成部署依赖。
- daemon 提示未保留非 localhost 上游 DNS 并采用默认外部 DNS；业务部署前需读取宿主机 `/etc/resolv.conf` 并在容器网络中验证域名解析。OCR/LLM 当前使用固定内网 IP，不依赖该 DNS，但文件下载域名可能受影响。

结论：Docker 运行环境的离线安装、daemon 启动、重启、开机启用、存储驱动、SELinux 保持和离线容器执行均已通过，可进入业务镜像打包与服务器部署阶段。
