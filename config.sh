#!/bin/bash
# ============================================
# Uptime Kuma 配置文件
# ============================================

export PORT="2114"
export TZ="Asia/Shanghai"

# 预构建包下载地址
export KUMA_DOWNLOAD_URL="https://github.com/oyz8/action/releases/download/2.0.2/uptime-kuma-2.0.2.tar.gz"

# ============================================
# WebDAV 备份配置
# ============================================
export WEBDAV_URL="https://zeze.teracloud.jp/dav/backup/Uptime-Kuma/"
export WEBDAV_USER="用户名"
export WEBDAV_PASS="密码"

# 备份密码（可选，留空则不加密）
export BACKUP_PASS=""

# 每天备份时间（小时，0-23）
export BACKUP_HOUR=4

# 保留备份天数
export KEEP_DAYS=5

# ============================================
# 哪吒监控 Agent 配置（可选）
# ============================================
# 从哪吒面板获取以下信息：
# 管理面板 -> 服务器 -> 添加服务器 -> 复制配置

export NZ_SERVER="beck.nyc.mn:443"      # 哪吒服务器地址:端口
export NZ_UUID="fe3d**************ae7"  # Agent UUID
export NZ_CLIENT_SECRET="444*****************444" # 客户端密钥
export NZ_TLS=true                          # 是否启用 TLS (true/false)
# export NZ_AGENT_VERSION="v1.0.0"           # 可选，默认自动获取最新版
