#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
用法：
  sudo bash deploy_offline_upgrade.sh <升级包.tar.gz> [升级包.tar.gz.sha256]

可选环境变量：
  CONTRACT_REVIEW_APP_ROOT 现有部署根目录，默认 /opt/contract-review
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || -z "${1:-}" ]]; then
  usage
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: 请使用 root 用户运行，或在命令前加 sudo。" >&2
  exit 1
fi

ARCHIVE_PATH="$(readlink -f -- "$1")"
CHECKSUM_PATH="$(readlink -f -- "${2:-${ARCHIVE_PATH}.sha256}")"
APP_ROOT="${CONTRACT_REVIEW_APP_ROOT:-/opt/contract-review}"
CURRENT_DIR="${APP_ROOT}/current"

if [[ ! -f "${ARCHIVE_PATH}" || ! -r "${ARCHIVE_PATH}" ]]; then
  echo "ERROR: 升级包不存在或不可读：${ARCHIVE_PATH}" >&2
  exit 1
fi
if [[ ! -f "${CHECKSUM_PATH}" || ! -r "${CHECKSUM_PATH}" ]]; then
  echo "ERROR: SHA-256 文件不存在或不可读：${CHECKSUM_PATH}" >&2
  exit 1
fi
if [[ "${ARCHIVE_PATH}" != *.tar.gz ]]; then
  echo "ERROR: 升级包必须是 .tar.gz 文件。" >&2
  exit 1
fi

for path in \
  "${CURRENT_DIR}/.env" \
  "${CURRENT_DIR}/compose.yaml" \
  "${CURRENT_DIR}/nginx.conf"; do
  if [[ ! -e "${path}" ]]; then
    echo "ERROR: 缺少现有部署文件：${path}" >&2
    exit 1
  fi
done

ARCHIVE_DIR="$(dirname -- "${ARCHIVE_PATH}")"
ARCHIVE_NAME="$(basename -- "${ARCHIVE_PATH}")"
cd "${ARCHIVE_DIR}"
echo "[1/5] 校验外层升级包：${ARCHIVE_NAME}"
sha256sum -c "${CHECKSUM_PATH}"

STAGE_DIR="$(mktemp -d /tmp/contract-review-offline-upgrade.XXXXXX)"
cleanup() {
  rm -rf -- "${STAGE_DIR}"
}
trap cleanup EXIT

echo "[2/5] 解压到临时目录：${STAGE_DIR}"
tar -xzf "${ARCHIVE_PATH}" -C "${STAGE_DIR}"

mapfile -t PACKAGE_DIRS < <(
  find "${STAGE_DIR}" -mindepth 1 -maxdepth 1 -type d \
    -name 'contract-review-agent-*-linux-amd64-offline-upgrade' -print
)
if [[ "${#PACKAGE_DIRS[@]}" -ne 1 ]]; then
  echo "ERROR: 归档中必须恰好包含一个 linux/amd64 升级目录。" >&2
  exit 1
fi
PACKAGE_DIR="${PACKAGE_DIRS[0]}"

for path in \
  "${PACKAGE_DIR}/SHA256SUMS" \
  "${PACKAGE_DIR}/upgrade.sh" \
  "${PACKAGE_DIR}/VERSION.txt"; do
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: 升级包缺少文件：${path}" >&2
    exit 1
  fi
done

echo "[3/5] 校验包内文件"
cd "${PACKAGE_DIR}"
sha256sum -c SHA256SUMS

echo "[4/5] 执行受控升级"
bash "${PACKAGE_DIR}/upgrade.sh"

echo "[5/5] 升级完成；现有 .env、数据库卷、上传文件和历史报告未被删除。"
