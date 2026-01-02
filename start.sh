#!/bin/bash
set -e

echo "=========================================="
echo "  🚀 Uptime Kuma 启动中 (Lunes.host)"
echo "=========================================="

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${APP_DIR}/data"
KUMA_DIR="${APP_DIR}/uptime-kuma"
AGENT_NAME="nezha-agent"
AGENT_PID=""
CONFIG_FILE=""

[ -f "${APP_DIR}/config.sh" ] && source "${APP_DIR}/config.sh" && echo "✓ 配置已加载"

mkdir -p "$DATA_DIR"
export DATA_DIR

# =========================
# 清理函数
# =========================
cleanup() {
    echo ""
    echo "[INFO] 正在清理..."
    
    if [ -n "$AGENT_PID" ] && kill -0 $AGENT_PID 2>/dev/null; then
        kill $AGENT_PID 2>/dev/null
        echo "✓ 哪吒 Agent 已停止"
    fi
    
    [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ] && rm -f "$CONFIG_FILE"
    
    exit 0
}
trap cleanup SIGTERM SIGINT

# =========================
# 下载预构建版本
# =========================
if [ ! -f "$KUMA_DIR/server/server.js" ]; then
    
    if [ -z "${KUMA_DOWNLOAD_URL:-}" ]; then
        echo "[ERROR] 请设置 KUMA_DOWNLOAD_URL"
        exit 1
    fi
    
    echo "[INFO] 下载 Uptime Kuma..."
    
    rm -rf "$KUMA_DIR"
    mkdir -p "$KUMA_DIR"
    
    curl -sL "$KUMA_DOWNLOAD_URL" | tar -xz --strip-components=1 -C "$KUMA_DIR"
    
    if [ ! -f "$KUMA_DIR/server/server.js" ]; then
        echo "[ERROR] 下载失败"
        exit 1
    fi
    
    echo "✓ 下载完成"
fi

if [ ! -d "$KUMA_DIR/node_modules" ]; then
    echo "[ERROR] node_modules 不存在"
    exit 1
fi

# =========================
# 哪吒 Agent 配置检查
# =========================
NEZHA_ENABLED=false

if [ -n "${NZ_SERVER:-}" ] || [ -n "${NZ_UUID:-}" ] || [ -n "${NZ_CLIENT_SECRET:-}" ]; then
    if [ -n "${NZ_SERVER:-}" ] && [ -n "${NZ_UUID:-}" ] && [ -n "${NZ_CLIENT_SECRET:-}" ]; then
        NEZHA_ENABLED=true
        echo "✓ 哪吒监控配置完整"
    else
        echo "[WARN] 哪吒配置不完整，Agent 将不会启动"
        echo "       需要配置: NZ_SERVER, NZ_UUID, NZ_CLIENT_SECRET"
        [ -z "${NZ_SERVER:-}" ] && echo "       - 缺少 NZ_SERVER"
        [ -z "${NZ_UUID:-}" ] && echo "       - 缺少 NZ_UUID"
        [ -z "${NZ_CLIENT_SECRET:-}" ] && echo "       - 缺少 NZ_CLIENT_SECRET"
    fi
fi

# =========================
# 下载哪吒 Agent
# =========================
if [ "$NEZHA_ENABLED" = true ] && [ ! -x "${APP_DIR}/${AGENT_NAME}" ]; then
    echo "[INFO] 下载哪吒监控 Agent..."
    
    cd "${APP_DIR}"
    arch=$(uname -m)
    case $arch in
        x86_64)  fileagent="nezha-agent_linux_amd64.zip" ;;
        aarch64) fileagent="nezha-agent_linux_arm64.zip" ;;
        s390x)   fileagent="nezha-agent_linux_s390x.zip" ;;
        *) 
            echo "[WARN] 不支持的架构: $arch，哪吒 Agent 将不会启动"
            NEZHA_ENABLED=false
            fileagent="" 
            ;;
    esac
    
    if [ -n "$fileagent" ]; then
        if [ -z "${NZ_AGENT_VERSION:-}" ] || [ "${NZ_AGENT_VERSION}" = "latest" ]; then
            NZ_AGENT_VERSION=$(curl -s https://api.github.com/repos/nezhahq/agent/releases/latest | grep '"tag_name":' | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')
        fi
        
        if [ -n "$NZ_AGENT_VERSION" ]; then
            URL="https://github.com/nezhahq/agent/releases/download/${NZ_AGENT_VERSION}/${fileagent}"
            
            if curl -sL "$URL" -o "$fileagent" && [ -s "$fileagent" ]; then
                unzip -qo "$fileagent" -d . && rm -f "$fileagent"
                mv ./nezha-agent "./${AGENT_NAME}" 2>/dev/null || true
                chmod +x "./${AGENT_NAME}"
                echo "✓ 哪吒 Agent 下载完成 (${NZ_AGENT_VERSION})"
            else
                echo "[WARN] 哪吒 Agent 下载失败，Agent 将不会启动"
                NEZHA_ENABLED=false
            fi
        else
            echo "[WARN] 无法获取哪吒 Agent 版本，Agent 将不会启动"
            NEZHA_ENABLED=false
        fi
    fi
fi

# =========================
# 启动哪吒 Agent
# =========================
if [ "$NEZHA_ENABLED" = true ] && [ -x "${APP_DIR}/${AGENT_NAME}" ]; then
    echo "[INFO] 启动哪吒监控 Agent..."
    
    # 生成随机文件名 (使用 $RANDOM 和 PID，不依赖 xxd)
    RANDOM_NAME=$(echo "${RANDOM}${$}$(date +%s)" | md5sum | head -c 8)
    CONFIG_FILE="${APP_DIR}/.${RANDOM_NAME}.yaml"
    
    cat > "${CONFIG_FILE}" <<EOF
client_secret: ${NZ_CLIENT_SECRET}
debug: false
disable_auto_update: true
disable_command_execute: false
disable_force_update: true
disable_nat: false
disable_send_query: false
gpu: false
insecure_tls: false
ip_report_period: 1800
report_delay: 4
server: ${NZ_SERVER}
skip_connection_count: false
skip_procs_count: false
temperature: false
tls: ${NZ_TLS:-false}
use_gitee_to_upgrade: false
use_ipv6_country_code: false
uuid: ${NZ_UUID}
EOF
    
    "${APP_DIR}/${AGENT_NAME}" -c "${CONFIG_FILE}" >/dev/null 2>&1 &
    AGENT_PID=$!
    sleep 3
    
    if kill -0 $AGENT_PID 2>/dev/null; then
        echo "✓ 哪吒 Agent 已启动 (PID: $AGENT_PID)"
    else
        echo "[WARN] 哪吒 Agent 启动失败"
        AGENT_PID=""
    fi
elif [ "$NEZHA_ENABLED" = true ]; then
    echo "[WARN] 哪吒 Agent 可执行文件不存在，Agent 将不会启动"
fi

# =========================
# 首次启动恢复备份
# =========================
if [ -n "${WEBDAV_URL:-}" ] && [ ! -f "$DATA_DIR/kuma.db" ]; then
    echo "[INFO] 首次启动，检查 WebDAV 备份..."
    bash "${APP_DIR}/scripts/restore.sh" || echo "[WARN] 恢复失败或无备份"
fi

# =========================
# 备份守护进程
# =========================
if [ -n "${WEBDAV_URL:-}" ]; then
    (
        while true; do
            sleep 3600
            
            current_date=$(date +"%Y-%m-%d")
            current_hour=$(date +"%H")
            
            LAST_BACKUP_FILE="/tmp/last_backup_date"
            last_backup_date=""
            [ -f "$LAST_BACKUP_FILE" ] && last_backup_date=$(cat "$LAST_BACKUP_FILE")
            
            if [ "$current_hour" -eq "${BACKUP_HOUR:-4}" ] && [ "$last_backup_date" != "$current_date" ]; then
                echo "[INFO] 执行每日备份..."
                bash "${APP_DIR}/scripts/backup.sh" && echo "$current_date" > "$LAST_BACKUP_FILE"
            fi
        done
    ) &
    
    echo "✓ 备份守护进程已启动 (每天 ${BACKUP_HOUR:-4}:00)"
fi

# =========================
# 启动 Uptime Kuma
# =========================
echo "[INFO] 启动 Uptime Kuma..."

export UPTIME_KUMA_PORT="${PORT:-3001}"
export NODE_OPTIONS="--max-old-space-size=256"

echo "=========================================="
echo "  端口: $UPTIME_KUMA_PORT"
echo "  数据: $DATA_DIR"
if [ -n "$AGENT_PID" ]; then
    echo "  哪吒: 已启用 (PID: $AGENT_PID)"
else
    echo "  哪吒: 未启用"
fi
echo "=========================================="

cd "$KUMA_DIR"
exec node server/server.js
