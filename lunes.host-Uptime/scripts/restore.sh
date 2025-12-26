#!/bin/bash
set -u

# 加载配置
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
[ -f "${APP_DIR}/config.sh" ] && source "${APP_DIR}/config.sh"

# 检查配置
if [ -z "${WEBDAV_URL:-}" ] || [ -z "${WEBDAV_USER:-}" ] || [ -z "${WEBDAV_PASS:-}" ]; then
    echo "[ERROR] WebDAV 未配置"
    exit 1
fi

DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_FILE="${1:-}"

echo "=========================================="
echo "  Uptime Kuma WebDAV 恢复"
echo "=========================================="

# 如果没有指定文件，查找最新备份
if [ -z "$BACKUP_FILE" ]; then
    echo "[INFO] 查找最新备份..."
    
    FILELIST=$(curl -s -u "${WEBDAV_USER}:${WEBDAV_PASS}" \
        -X PROPFIND \
        -H "Depth: 1" \
        "${WEBDAV_URL}" 2>/dev/null)
    
    BACKUP_FILE=$(echo "$FILELIST" | grep -oE 'lunes-host-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.zip' | sort -r | head -n1)
    
    if [ -z "$BACKUP_FILE" ]; then
        echo "[INFO] 未找到备份文件"
        exit 0
    fi
fi

echo "[INFO] 恢复: $BACKUP_FILE"

# 临时目录
TEMP_DIR="/tmp/restore-$$"
mkdir -p "$TEMP_DIR"
cd "$TEMP_DIR"

# 下载备份
echo "[INFO] 下载备份..."
curl -s -u "${WEBDAV_USER}:${WEBDAV_PASS}" \
    -o "backup.zip" \
    "${WEBDAV_URL}${BACKUP_FILE}"

if [ ! -s "backup.zip" ]; then
    echo "[ERROR] 下载失败"
    rm -rf "$TEMP_DIR"
    exit 1
fi

echo "[INFO] 解压..."
if [ -n "${BACKUP_PASS:-}" ]; then
    unzip -P "$BACKUP_PASS" -o backup.zip
else
    unzip -o backup.zip
fi

if [ ! -d "data" ]; then
    echo "[ERROR] 解压失败，未找到 data 目录"
    rm -rf "$TEMP_DIR"
    exit 1
fi

# 恢复数据
echo "[INFO] 恢复数据到 $DATA_DIR..."
mkdir -p "$DATA_DIR"
cp -R data/* "$DATA_DIR/"

# 清理
rm -rf "$TEMP_DIR"

echo "=========================================="
echo "[SUCCESS] 恢复完成 ✓"
echo "=========================================="
