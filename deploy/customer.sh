#!/usr/bin/env bash
# 多客户部署：一份代码，一个客户一个独立实例（独立端口、独立密钥、独立路径）。
#
#   customer.sh setup              第一次：装代码和模板服务（需要 sudo、python3-venv）
#   customer.sh add 客户名 [端口]    新增一个客户，打印要发给客户的信息
#   customer.sh list               列出客户、端口、运行状态（不显示密钥）
#   customer.sh remove 客户名 --yes 停掉并删除这个客户（密钥作废）
#
# setup 之前，先把这三个文件放到服务器的 /tmp：
#   scp linkmcp.py requirements-server.txt "deploy/link-mcp@.service" 你@服务器:/tmp/
# 然后：scp deploy/customer.sh 你@服务器:/tmp/ && ssh 你@服务器 'bash /tmp/customer.sh setup'
#
# 本脚本不会改 Caddy/Nginx（你的机器上可能还有别的服务）。add 之后它会把要加的那一段打印出来，
# 你自己先备份、再 validate、再 reload。
set -euo pipefail

APP_USER=linkmcp
APP_DIR=/opt/link-mcp
CONF_DIR=/etc/link-mcp
FIRST_PORT=8810

die() { echo "[x] $*" >&2; exit 1; }
need_name() {
    [[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "客户名只能用小写字母、数字、短横线，且不超过 31 个字符。"
}

port_in_use() {
    local p="$1"
    # 已经分给别的客户
    if sudo grep -qsE "^LINK_PORT=${p}\s*$" "$CONF_DIR"/*.env 2>/dev/null; then return 0; fi
    # 系统里已经有人在听
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then return 0; fi
    return 1
}

cmd_setup() {
    for f in linkmcp.py requirements-server.txt "link-mcp@.service"; do
        [ -f "/tmp/$f" ] || die "缺少 /tmp/$f，先 scp 上来（见本脚本开头的说明）。"
    done
    id "$APP_USER" >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin -d "$APP_DIR" "$APP_USER"
    sudo mkdir -p "$APP_DIR" "$CONF_DIR"
    sudo cp /tmp/linkmcp.py "$APP_DIR/linkmcp.py"
    [ -d "$APP_DIR/venv" ] || sudo python3 -m venv "$APP_DIR/venv"
    sudo "$APP_DIR/venv/bin/pip" install -q -r /tmp/requirements-server.txt
    sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    sudo chown root:"$APP_USER" "$CONF_DIR"
    sudo chmod 750 "$CONF_DIR"
    sudo cp "/tmp/link-mcp@.service" /etc/systemd/system/link-mcp@.service
    sudo systemctl daemon-reload
    echo "[ok] 已装好。接下来用：bash customer.sh add 客户名"
    echo "     （如果这台机器上已有旧的单实例服务 link-mcp，它不受影响，也不会被改动。）"
}

cmd_add() {
    local name="${1:-}" port="${2:-}"
    need_name "$name"
    [ -f /etc/systemd/system/link-mcp@.service ] || die "还没 setup。先跑：bash customer.sh setup"
    [ ! -e "$CONF_DIR/$name.env" ] || die "客户 $name 已经存在。要重来先 remove。"

    if [ -z "$port" ]; then
        port=$FIRST_PORT
        while port_in_use "$port"; do port=$((port + 1)); done
    else
        [[ "$port" =~ ^[0-9]+$ ]] || die "端口必须是数字。"
        port_in_use "$port" && die "端口 $port 已被占用。"
    fi

    local link_token relay_token
    link_token=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
    relay_token=$(python3 -c 'import secrets; print(secrets.token_hex(24))')

    sudo bash -c "umask 077; cat > '$CONF_DIR/$name.env'" <<EOF
LINK_TOKEN=$link_token
RELAY_TOKEN=$relay_token
LINK_HOST=127.0.0.1
LINK_PORT=$port
EOF
    sudo chown root:"$APP_USER" "$CONF_DIR/$name.env"
    sudo chmod 640 "$CONF_DIR/$name.env"

    sudo systemctl enable --now "link-mcp@$name"
    sleep 3
    local state
    state=$(systemctl is-active "link-mcp@$name" || true)
    [ "$state" = "active" ] || die "服务没起来（$state）。看日志：journalctl -u link-mcp@$name -n 30"
    curl -s -m 5 "http://127.0.0.1:$port/health" >/dev/null || echo "[!] 健康检查没通过，先看日志。"

    cat <<EOF

================ 客户 $name 已创建（端口 $port） ================
下面的密钥只显示这一次，请复制到客户资料里，不要贴进聊天。

【1】给客户电脑（安装时问的两项）
    服务器地址：wss://你的域名/link-$name
    暗号：      $relay_token

【2】给客户的 AI 客户端（一串地址，钥匙在路径里）
    https://你的域名/link-$name/k/$link_token/mcp

【3】你要自己加的 Caddy 配置（先备份 Caddyfile，caddy validate 通过再 reload）
    handle_path /link-$name/* {
        reverse_proxy 127.0.0.1:$port
    }
================================================================
EOF
}

cmd_list() {
    [ -d "$CONF_DIR" ] || die "还没 setup。"
    printf '%-24s %-8s %s\n' 客户 端口 状态
    local f name port
    for f in $(sudo ls "$CONF_DIR" 2>/dev/null | grep '\.env$' || true); do
        name="${f%.env}"
        port=$(sudo grep -E '^LINK_PORT=' "$CONF_DIR/$f" | cut -d= -f2)
        printf '%-24s %-8s %s\n' "$name" "$port" "$(systemctl is-active "link-mcp@$name" 2>/dev/null || true)"
    done
}

cmd_remove() {
    local name="${1:-}" yes="${2:-}"
    need_name "$name"
    [ -e "$CONF_DIR/$name.env" ] || die "没有这个客户：$name"
    if [ "$yes" != "--yes" ]; then
        echo "将会：停掉 link-mcp@$name，并删除 $CONF_DIR/$name.env（这个客户的密钥立刻作废，无法恢复）。"
        echo "确认请加 --yes：bash customer.sh remove $name --yes"
        exit 0
    fi
    sudo systemctl disable --now "link-mcp@$name" || true
    sudo rm -f "$CONF_DIR/$name.env"
    echo "[ok] 已删除 $name。别忘了把 Caddyfile 里 /link-$name/* 那一段也去掉（先备份）。"
}

case "${1:-}" in
    setup)  cmd_setup ;;
    add)    shift; cmd_add "$@" ;;
    list)   cmd_list ;;
    remove) shift; cmd_remove "$@" ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
esac
