#!/bin/bash
set -u

# 加载配置
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
[ -f "${APP_DIR}/config.sh" ] && source "${APP_DIR}/config.sh"

# 检查配置
if [ -z "${WEBDAV_URL:-}" ] || [ -z "${WEBDAV_USER:-}" ] || [ -z "${WEBDAV_PASS:-}" ]; then
    echo "[WARN] WebDAV 未配置，跳过备份"
    exit 0
fi

# 变量
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
TIMESTAMP=$(TZ="${TZ:-Asia/Shanghai}" date +"%Y-%m-%d-%H-%M-%S")
BACKUP_FILE="lunes-host-backup-${TIMESTAMP}.zip"

echo "=========================================="
echo "  Uptime Kuma WebDAV 备份"
echo "=========================================="

# 检查数据目录
if [ ! -d "$DATA_DIR" ]; then
    echo "[ERROR] 数据目录不存在: $DATA_DIR"
    exit 1
fi

# 临时目录
TEMP_DIR="/tmp/backup-$$"
mkdir -p "$TEMP_DIR"
cd "$TEMP_DIR"

# 复制数据
echo "[INFO] 准备数据..."
cp -R "$DATA_DIR" "$TEMP_DIR/data"
rm -rf "$TEMP_DIR/data/upload" "$TEMP_DIR/data/"*.log 2>/dev/null

# 压缩
echo "[INFO] 压缩: $BACKUP_FILE"
if [ -n "${BACKUP_PASS:-}" ]; then
    zip -r -P "$BACKUP_PASS" "$BACKUP_FILE" data/
else
    zip -r "$BACKUP_FILE" data/
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[INFO] 大小: $BACKUP_SIZE"

# 上传到 WebDAV
echo "[INFO] 上传到 WebDAV..."
UPLOAD_STATUS=$(curl -u "${WEBDAV_USER}:${WEBDAV_PASS}" \
    -T "$BACKUP_FILE" \
    -s -w "%{http_code}" -o /dev/null \
    "${WEBDAV_URL}${BACKUP_FILE}")

if [ "$UPLOAD_STATUS" -ge 200 ] && [ "$UPLOAD_STATUS" -lt 300 ]; then
    echo "[SUCCESS] 上传成功 ✓"
else
    echo "[ERROR] 上传失败 (HTTP $UPLOAD_STATUS)"
    rm -rf "$TEMP_DIR"
    exit 1
fi

# 删除旧备份
KEEP_DAYS="${KEEP_DAYS:-7}"
echo "[INFO] 清理 ${KEEP_DAYS} 天前的备份..."

# 计算过期日期
if date --version >/dev/null 2>&1; then
    # GNU date (Linux)
    OLD_DATE=$(date --date="${KEEP_DAYS} days ago" +"%Y-%m-%d")
else
    # BSD date (macOS/FreeBSD)
    OLD_DATE=$(date -v -${KEEP_DAYS}d +"%Y-%m-%d")
fi

# 获取 WebDAV 文件列表并删除旧文件
FILELIST=$(curl -s -u "${WEBDAV_USER}:${WEBDAV_PASS}" \
    -X PROPFIND \
    -H "Depth: 1" \
    "${WEBDAV_URL}" 2>/dev/null)

# 提取文件名
echo "$FILELIST" | grep -oE 'lunes-host-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.zip' | sort -u | while read old_file; do
    # 提取日期部分
    file_date=$(echo "$old_file" | sed -n 's/lunes-host-backup-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/p')
    
    if [ -n "$file_date" ] && [ "$file_date" \< "$OLD_DATE" ]; then
        echo "[INFO] 删除旧备份: $old_file"
        DELETE_STATUS=$(curl -s -u "${WEBDAV_USER}:${WEBDAV_PASS}" \
            -X DELETE \
            -w "%{http_code}" -o /dev/null \
            "${WEBDAV_URL}${old_file}")
        
        if [ "$DELETE_STATUS" -ge 200 ] && [ "$DELETE_STATUS" -lt 300 ]; then
            echo "  ✓ 已删除"
        else
            echo "  ✗ 删除失败 (HTTP $DELETE_STATUS)"
        fi
    fi
done

# 清理临时文件
rm -rf "$TEMP_DIR"

echo "=========================================="
echo "[SUCCESS] 备份完成: $BACKUP_FILE 🎉"
echo "=========================================="
