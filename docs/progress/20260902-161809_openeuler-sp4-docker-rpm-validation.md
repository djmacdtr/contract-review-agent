# openEuler 24.03 LTS SP4 Docker CE RPM 兼容性验证

## 目标

在本机 Docker Desktop 中使用官方 openEuler 24.03 LTS SP4 x86_64 容器镜像，验证此前建议的 Docker CE EL9 RPM 版本组合能否完成依赖解析、GPG 校验、实际安装和 Docker daemon 启动。

## 验证基线

- Docker Desktop：4.63.0，Linux Engine 29.2.1，`linux/amd64`。
- openEuler 镜像：`openeuler/openeuler:24.03-lts-sp4`。
- 镜像索引摘要：`sha256:17c15554be2a5bc46023acb6e04d609d77642b8c20e236e88deb18e41ae4558e`。
- 容器系统：`openEuler 24.03 (LTS-SP4)`，`x86_64`。
- Docker CE 软件源：清华 TUNA CentOS 9 Docker CE stable 镜像。

## 精确验证版本

| 组件 | RPM 版本 | 结果 |
| --- | --- | --- |
| Docker Engine | `3:29.7.2-1.el9` | 通过 |
| Docker CLI | `1:29.7.2-1.el9` | 通过 |
| containerd.io | `2.3.4-1.el9` | 通过 |
| Docker Buildx Plugin | `0.36.1-1.el9` | 通过 |
| Docker Compose Plugin | `5.5.0-1.el9` | 通过 |

## 依赖解析

- DNF 在 SP4 环境中成功解析 39 个安装包，总下载约 121 MB，安装后约占 462 MB。
- Docker CE 的 5 个目标包来自 EL9 软件源。
- 所有系统依赖均来自 openEuler SP4 的 `OS`/`update` 仓库，版本标识为 `.oe2403sp4`，未混入 `.oe2403sp3` 包。
- 关键依赖包括：`container-selinux-2.230.0-1.oe2403sp4`、`libseccomp-2.5.4-5.oe2403sp4`、`policycoreutils`、`selinux-policy-targeted`、`iptables`、`nftables`、`systemd` 等。
- DNF transaction check 与 transaction test 均通过。

## GPG 校验

- openEuler GPG Key 成功导入并验证。
- Docker CE GPG Key 成功导入并验证，指纹：`060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35`。
- 39 个 RPM 全部完成 DNF 安装与验证。

## daemon 验证

在特权临时容器中以 VFS 存储驱动、关闭 bridge/iptables 的方式启动嵌套 Docker daemon：

- Docker Client：29.7.2。
- Docker Server：29.7.2。
- Docker Compose：v5.5.0。
- Docker Buildx：v0.36.1。
- daemon 识别：`openEuler 24.03 (LTS-SP4)`、`x86_64`、cgroup v2。
- daemon 成功进入可响应 `docker info` 的状态并正常停止。

日志中的 fs-verity、EROFS、devmapper、ZFS 和 nftables 清理提示来自 Docker-in-Docker/VFS/禁用网络桥接的测试约束，不是 RPM 依赖或 daemon 启动失败。

## 结论

该精确版本组合可在 openEuler 24.03 LTS SP4 x86_64 用户空间中完成依赖解析、GPG 校验、RPM 安装和 Docker daemon 启动。可以据此制作 SP4 专用离线 RPM 包。

不能把本次容器验证等同于生产 VM 的全部验收：Docker Desktop 容器共享 WSL2 Linux 内核，未完整覆盖生产服务器的 openEuler 内核、systemd PID 1、SELinux Enforcing、真实 overlay2、iptables/nftables 和重启自启动。RPM 包上传到甲方 SP4 服务器后仍需执行一次最终主机验收。

## 环境清理

- 所有实际安装和 daemon 操作均在临时 `--rm` 容器内执行。
- 一次被中断的查询容器 `brave_kare` 已确认启动命令属于本次验证，随后停止并由 `--rm` 自动删除；无可恢复数据，也不包含项目数据。
- 最终仅项目既有 API 与 PostgreSQL 容器保持运行且健康；未重启、停止或修改它们。
- 未创建持久验证镜像、卷或网络；官方 SP4 基础镜像保留在本机。
- 未 commit、未 push，未修改业务代码或部署配置。

## 甲方服务器最终验收建议

离线包上传后依次验证：操作系统版本与架构、RPM 签名、离线 DNF 依赖闭包、`systemctl enable --now docker`、Docker/Compose/Buildx 版本、overlay2/cgroup/SELinux 状态、daemon 重启自启动，以及加载离线测试镜像后的容器运行。
