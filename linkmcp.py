"""
LINK 服务器端 MCP —— 跟 xiuxiuda 一个路数：MCP 在服务器上，电脑只留一个薄中继。

   手机 / Grok ──(一串地址)──▶ 这里（十个工具）──WebSocket──▶ 电脑上的 linkrelay.py ──▶ 本机桥 ──▶ Intiface ──▶ 设备

特点：
  - 手机只填一串地址：  http(s)://<服务器>/k/<LINK_TOKEN>/mcp   （钥匙在路径里，不用另填）
  - 电脑没开也能"连一下"、列出工具；真调用时回一句 "掌机没在线"，不会报连接失败
  - 工具名、参数、说明和本机桥（bridge.py）完全一致，小手机那边不用改

环境变量：
  LINK_TOKEN    手机地址里的那串（必填）
  RELAY_TOKEN   电脑中继进来的暗号（必填，和上面不同）
  LINK_HOST     监听地址，默认 127.0.0.1（tx 直接对外时填 0.0.0.0）
  LINK_PORT     端口，默认 8802
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

LINK_TOKEN = os.getenv("LINK_TOKEN", "").strip()
RELAY_TOKEN = os.getenv("RELAY_TOKEN", "").strip()
HOST = os.getenv("LINK_HOST", "127.0.0.1")
PORT = int(os.getenv("LINK_PORT", "8802"))
CALL_TIMEOUT = float(os.getenv("LINK_CALL_TIMEOUT", "75"))

OFFLINE = "err: 掌机没在线（电脑上的 LINK 没开，或者电脑睡着了）。让对方先开机。"

PC: WebSocket | None = None
PENDING: dict[str, asyncio.Future] = {}
SEEN = {"since": 0.0, "last_call": 0.0}


def note(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def ask_pc(tool: str, args: dict) -> str:
    """把一次工具调用递给电脑，等它回话。"""
    ws = PC
    if ws is None:
        return OFFLINE
    rid = uuid.uuid4().hex[:12]
    fut = asyncio.get_running_loop().create_future()
    PENDING[rid] = fut
    try:
        await ws.send_text(json.dumps({"id": rid, "tool": tool, "args": args}, ensure_ascii=False))
        SEEN["last_call"] = time.time()
        return await asyncio.wait_for(fut, timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return "err: 电脑那边没回话（超时），可能玩具或桥卡住了"
    except Exception as e:
        return f"err: 转给电脑失败：{type(e).__name__}"
    finally:
        PENDING.pop(rid, None)


mcp = MCPServer(name="link")


def _clean(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


# 下面十个工具：名字、参数、说明都和 bridge.py 一致（改一边记得改另一边）


@mcp.tool(description="链路状态：上游连没连上、有几路通道、上次出错原因。排查先看这个。")
async def link_status() -> str:
    return await ask_pc("link_status", {})


@mcp.tool(description="列出当前可用的通道：编号、名称、支持哪几类输出、有没有电量。指定通道时用这里的编号或名称。")
async def channel_list() -> str:
    return await ask_pc("channel_list", {})


@mcp.tool(description="重新扫一遍通道。seconds 是扫描时长，默认 8 秒。")
async def channel_scan(seconds: int = 8) -> str:
    return await ask_pc("channel_scan", {"seconds": seconds})


@mcp.tool(description="设置 A 路输出。val 是 0-100，sec 秒后自动归零；不写 sec 就一直保持。device 填编号或名称，不填用第一个。")
async def output_a(val: float, device: str = "", sec: float = 0) -> str:
    return await ask_pc("output_a", {"val": val, "device": device, "sec": sec})


@mcp.tool(description="设置 B 路输出。参数同 output_a：val 0-100，sec 秒，device 编号或名称。")
async def output_b(val: float, device: str = "", sec: float = 0) -> str:
    return await ask_pc("output_b", {"val": val, "device": device, "sec": sec})


@mcp.tool(description="设置 C 路输出。参数同 output_a：val 0-100，sec 秒，device 编号或名称。")
async def output_c(val: float, device: str = "", sec: float = 0) -> str:
    return await ask_pc("output_c", {"val": val, "device": device, "sec": sec})


@mcp.tool(description="设置行程位置。val 0-100，sec 是走完这段的秒数，device 编号或名称。")
async def output_pos(val: float, device: str = "", sec: float = 1.0) -> str:
    return await ask_pc("output_pos", {"val": val, "device": device, "sec": sec})


@mcp.tool(
    description=(
        "按顺序放一串值。seq 是 0-100 的数组，比如 [20,60,20,80]；"
        "step 是每段几秒，默认 1 秒。放完自动归零。device 填编号或名称。"
    )
)
async def output_seq(seq: list[int] | str, device: str = "", step: float = 1.0) -> str:
    return await ask_pc("output_seq", {"seq": seq, "device": device, "step": step})


@mcp.tool(description="立刻归零。device 不填就是所有通道全归零。")
async def output_stop(device: str = "") -> str:
    return await ask_pc("output_stop", {"device": device})


@mcp.tool(description="查电量，返回百分比整数。device 不填查第一个。")
async def channel_level(device: str = "") -> str:
    return await ask_pc("channel_level", {"device": device})


# ───────────────────────── 电脑那头 ─────────────────────────


async def relay_ws(ws: WebSocket) -> None:
    global PC
    if not RELAY_TOKEN or not hmac.compare_digest(ws.query_params.get("t", ""), RELAY_TOKEN):
        await ws.close(code=4003)
        return
    await ws.accept()
    old, PC = PC, ws
    SEEN["since"] = time.time()
    if old is not None:
        try:
            await old.close(code=4001)
        except Exception:
            pass
    note("电脑中继已连上")
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            fut = PENDING.get(msg.get("id", ""))
            if fut is not None and not fut.done():
                fut.set_result(str(msg.get("text", "")))
    except (WebSocketDisconnect, Exception) as e:
        note(f"电脑中继断了：{type(e).__name__}")
    finally:
        if PC is ws:
            PC = None


async def health(request) -> JSONResponse:
    return JSONResponse({"ok": True, "pc_online": PC is not None, "pc_since": SEEN["since"] or None})


class KeyPath:
    """/k/<LINK_TOKEN>/mcp  →  /mcp。钥匙不对就当没这个地址。"""

    def __init__(self, app) -> None:
        self.app = app
        self.prefix = f"/k/{LINK_TOKEN}"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in ("/health", "/healthz"):
                return await self.app(scope, receive, send)
            if path == self.prefix + "/mcp" or path.startswith(self.prefix + "/mcp/"):
                scope = dict(scope, path=path[len(self.prefix):], raw_path=path[len(self.prefix):].encode())
                return await self.app(scope, receive, send)
            # 标准写法：干净的 /mcp + 请求头 Authorization: Bearer <LINK_TOKEN>（小手机的「网址 + 钥匙」两个框）
            if path == "/mcp" or path.startswith("/mcp/"):
                auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
                if hmac.compare_digest(auth, f"Bearer {LINK_TOKEN}"):
                    return await self.app(scope, receive, send)
            return await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)


def build_app():
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        host=HOST,
        stateless_http=True,
        json_response=True,
        # 对外开放时 Host 是域名/IP，不是 127.0.0.1，关掉本机限定
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.add_route("/health", health, methods=["GET"])
    app.router.add_websocket_route("/relay", relay_ws)
    return KeyPath(app)


def main() -> None:
    if not LINK_TOKEN or not RELAY_TOKEN or LINK_TOKEN == RELAY_TOKEN:
        raise SystemExit("[x] 必须设置 LINK_TOKEN 和 RELAY_TOKEN，而且两者不能一样。")
    if len(LINK_TOKEN) < 24:
        raise SystemExit("[x] LINK_TOKEN 太短，至少 24 位随机字符。")
    note(f"LINK 服务器端 MCP 在听 {HOST}:{PORT}")
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
