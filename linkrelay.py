"""
LINK 电脑中继 —— 跑在你电脑上，很薄。

服务器（linkmcp.py）收到手机的工具调用，顺着这条 WebSocket 递过来；
这里把它转给本机的桥（bridge.py，127.0.0.1:8791），拿到结果再送回去。
所有真正的逻辑（限幅、自动归零、报给面板屏幕）都还在桥里，这里只负责搬运。

环境变量（同目录 .env）：
  LINK_SERVERS      服务器中继地址，可写多个用逗号隔开，如
                    wss://your-domain.example/link,ws://YOUR_SERVER_IP:8801
  LINK_RELAY_TOKEN  进服务器的暗号（和服务器上的 RELAY_TOKEN 一样）
  ROOM_BRIDGE       本机桥地址，默认 http://127.0.0.1:8791
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

import websockets
from dotenv import load_dotenv

load_dotenv()

SERVERS = [s.strip().rstrip("/") for s in os.getenv("LINK_SERVERS", "").split(",") if s.strip()]
TOKEN = os.getenv("LINK_RELAY_TOKEN", "").strip()
BRIDGE = os.getenv("ROOM_BRIDGE", "http://127.0.0.1:8791").rstrip("/") + "/mcp"
RECONNECT = float(os.getenv("LINK_RECONNECT", "5"))
MEDIATOR = os.getenv("ROOM_LOCAL", "http://127.0.0.1:8800").rstrip("/")
PROTOCOL = "2025-03-26"

_session: str | None = None
_lock = asyncio.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _post(payload: dict, session: str | None) -> tuple[int, str | None, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
    if session:
        h["Mcp-Session-Id"] = session
    # 本机请求不走代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(BRIDGE, json.dumps(payload).encode(), h, method="POST")
    try:
        with opener.open(req, timeout=70) as r:
            return r.status, r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", "replace")


def _reply_of(body: str, rid: int) -> dict:
    """响应可能是 JSON，也可能是 SSE：都拆出和 rid 对得上的那条。"""
    s = body.strip()
    if s.startswith("{"):
        return json.loads(s)
    for blk in body.split("\n\n"):
        data = "".join(x[5:].strip() for x in blk.split("\n") if x.startswith("data:"))
        if data:
            m = json.loads(data)
            if m.get("id") == rid:
                return m
    return {}


def _handshake() -> str | None:
    st, sid, body = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "link-relay", "version": "1"}}}, None)
    if st != 200:
        raise RuntimeError(f"桥握手失败 HTTP {st}")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def call_bridge(tool: str, args: dict) -> str:
    global _session
    for attempt in (1, 2):
        try:
            if _session is None:
                _session = _handshake()
            rid = int(time.time() * 1000) % 10_000_000
            st, _, body = _post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                 "params": {"name": tool, "arguments": args}}, _session)
            if st in (400, 404):  # 会话丢了（桥重启过）：重新握手再来一次
                _session = None
                continue
            if st != 200:
                return f"err: 桥回了 HTTP {st}"
            msg = _reply_of(body, rid)
            if msg.get("error"):
                return f"err: {msg['error'].get('message', '桥报错')}"
            res = msg.get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
            return ("err: " + text) if res.get("isError") and not text.startswith("err") else text
        except (urllib.error.URLError, ConnectionError, OSError):
            _session = None
            if attempt == 2:
                return "err: 电脑上的桥没开（运行 Start LINK）"
        except Exception as e:
            _session = None
            if attempt == 2:
                return f"err: 中继出错 {type(e).__name__}: {e}"
    return "err: 桥没有应答"


async def handle(ws, msg: dict) -> None:
    async with _lock:  # 一次一个：设备本来也只有一个，别让两条指令互相踩
        text = await asyncio.to_thread(call_bridge, msg.get("tool", ""), msg.get("args") or {})
    await ws.send(json.dumps({"id": msg.get("id"), "text": text}, ensure_ascii=False))


def _report(name: str, up: bool) -> None:
    """告诉本机中介（掌机面板）：我和这台服务器连着没有。面板没开也无所谓。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(MEDIATOR + "/api/relay", json.dumps({"name": name, "up": up}).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
        opener.open(req, timeout=2).read()
    except Exception:
        pass


async def beat(name: str) -> None:
    while True:
        await asyncio.to_thread(_report, name, True)
        await asyncio.sleep(10)


async def serve(server: str) -> None:
    uri = f"{server}/relay?t={TOKEN}"
    tag = server.split("://", 1)[-1]
    while True:
        hb = None
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                log(f"[{tag}] 已连上服务器，等手机来调")
                hb = asyncio.create_task(beat(tag))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "tool" in msg:
                        asyncio.create_task(handle(ws, msg))
        except Exception as e:
            log(f"[{tag}] 断了（{type(e).__name__}），{RECONNECT:.0f} 秒后重连")
        finally:
            if hb:
                hb.cancel()
            await asyncio.to_thread(_report, tag, False)
        await asyncio.sleep(RECONNECT)


async def main() -> None:
    if not SERVERS or not TOKEN:
        print("[x] .env 里没填 LINK_SERVERS 或 LINK_RELAY_TOKEN。")
        return
    print(f"本机桥：{BRIDGE}")
    await asyncio.gather(*(serve(s) for s in SERVERS))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("走了")
