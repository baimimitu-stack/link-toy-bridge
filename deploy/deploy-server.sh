#!/usr/bin/env bash
# 把服务器端 MCP 装到一台 Linux 服务器上（需要 sudo、python3-venv）。
# 用法（在本仓库根目录）：
#   1. 先在服务器上写好 /etc/link-mcp.env（见 deploy/link-mcp.env.example）
#   2. scp linkmcp.py requirements-server.txt deploy/link-mcp.service 你@服务器:/tmp/
#   3. ssh 你@服务器 'bash -s' < deploy/deploy-server.sh
set -euo pipefail

APP_USER=linkmcp
APP_DIR=/opt/link-mcp

id "$APP_USER" >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin -d "$APP_DIR" "$APP_USER"
sudo mkdir -p "$APP_DIR"
sudo cp /tmp/linkmcp.py "$APP_DIR/linkmcp.py"
[ -d "$APP_DIR/venv" ] || sudo python3 -m venv "$APP_DIR/venv"
sudo "$APP_DIR/venv/bin/pip" install -q -r /tmp/requirements-server.txt
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo chown "root:$APP_USER" /etc/link-mcp.env
sudo chmod 640 /etc/link-mcp.env
sudo cp /tmp/link-mcp.service /etc/systemd/system/link-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now link-mcp
sleep 3
systemctl is-active link-mcp
curl -s -m 5 "http://127.0.0.1:${LINK_PORT:-8802}/health" || true
echo
