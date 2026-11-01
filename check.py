"""
自检：完全按小手机的方式连一次桥（initialize → notifications/initialized → tools/list）。
用法：
    python check.py                      连本机 8791，钥匙从 .env 读
    python check.py http://192.168.x.x:8791/mcp   连别的地址（比如从手机网络看）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
PORT = os.getenv("BRIDGE_PORT", "8791")
URL = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{PORT}/mcp"
PROTOCOL = "2025-03-26"
session: str | None = None


def post(payload: dict, expect_reply: bool = True) -> dict:
    global session
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                session = sid
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:300]}")
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"✗ 连不上：{e.reason}")
        print("  多半是桥没开，或者手机和电脑不在同一个 WiFi")
        raise SystemExit(1)

    if not expect_reply:
        return {}
    if "text/event-stream" in ctype:
        for block in body.split("\n\n"):
            data = "\n".join(l[5:].strip() for l in block.split("\n") if l.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
                if msg.get("id") == payload.get("id"):
                    return msg
            except json.JSONDecodeError:
                pass
        return {"raw": body[:300]}
    return json.loads(body) if body else {}


print(f"连：{URL}")
init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                        "clientInfo": {"name": "virtual-phone", "version": "0.1.0"}}})
if init.get("error"):
    print("✗ initialize 失败：", init["error"])
    raise SystemExit(1)
info = init.get("result", {})
print(f"✓ 握手成功：{info.get('serverInfo', {}).get('name')} · session={session}")

post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)

tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).get("result", {}).get("tools", [])
print(f"✓ 工具 {len(tools)} 个：")
for t in tools:
    params = ", ".join(t.get("inputSchema", {}).get("properties", {}).keys())
    print(f"   - {t['name']}({params})  {t.get('description', '')[:56]}")

# 顺手看看 Intiface 连上没有
st = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "link_status", "arguments": {}}})
txt = st.get("result", {}).get("content", [{}])[0].get("text", "")
print("✓ 桥状态：", txt)
