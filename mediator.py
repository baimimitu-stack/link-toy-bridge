"""
中介：一部不会思考的手机。

它的定位 —— 角色在另一头（小手机里的模型），角色做判断，中介只负责三件事：
  1. 把角色回复里夹的那段数据串认出来，原样执行；
  2. 把数据串从回复里抹掉，让对方看到的还是一句正常的话；
  3. 把发生过什么显示在自己的屏幕上（/ 面板），让角色（和人）能看到反馈。

它自己不推理、不判断，也绝口不提这些通道是做什么的。

对外三层：
  /v1/chat/completions   装成 OpenAI 模型接口，小手机把接口地址指过来就能用
  /api/*                 给面板用的状态与动作接口
  /                      面板本体（手机浏览器也能开，和电脑看到的是同一块屏幕）

数据串协议（会进模型上下文，措辞刻意中性）：
  {"ch":1,"val":50,"sec":10}       ch=通道 1/2/3/4，val=0-100，sec=秒（可省）
  {"seq":[20,60,20],"step":1}      放一串值
  {"op":"stop"}                    全部归零
  {"op":"list"} / "status" / "level"

环境变量（同目录 .env）：
  MEDIATOR_PORT      8800
  MEDIATOR_TOKEN     给小手机和面板填的钥匙，留空则不校验
  UPSTREAM_BASE_URL  真正的模型接口，如 https://api.deepseek.com/v1（不填就只当转发器）
  UPSTREAM_API_KEY   它的钥匙
  UPSTREAM_MODEL     模型名
  BRIDGE_MCP         桥的地址，默认 http://127.0.0.1:8791/mcp
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

# 本进程只跟本机说话（127.0.0.1:8791 / 127.0.0.1:8800），urllib 会读
# 环境里的 HTTP_PROXY，把本机请求也丢给代理被拒。清掉。
# （将来如果给 UPSTREAM_BASE_URL 填了需要走代理的外网模型接口，
#   在 .env 里按名字重新设就行，这里只保证默认情况下本地通信不被拦。）
for _k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

load_dotenv()

HERE = Path(__file__).parent
WEB = HERE / "web"
PORT = int(os.getenv("MEDIATOR_PORT", "8800"))
UPSTREAM = os.getenv("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_KEY = os.getenv("UPSTREAM_API_KEY", "")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "")
BRIDGE_MCP = os.getenv("BRIDGE_MCP", "http://127.0.0.1:8791/mcp")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
MEDIATOR_TOKEN = os.getenv("MEDIATOR_TOKEN", "").strip()
PROTOCOL = "2025-03-26"

# 这段会进模型上下文，所以只讲格式、不讲用途
PROTOCOL_HINT = (
    "\n\n你可以在回复里夹一段数据来设置通道输出，格式：\n"
    '{"ch":1,"val":50,"sec":10}\n'
    "ch 是通道号 1/2/3/4，val 是 0-100，sec 是持续秒数（可省，不写就一直保持）。\n"
    '想放一串值用 {"seq":[20,60,20],"step":1}。归零用 {"op":"stop"}。\n'
    "看当前通道用 {\"op\":\"list\"}。\n"
    "数据串会被吃掉，不会显示给对方，所以你照样正常说话。"
)

# 屏幕上的记忆：谁说了什么、执行了什么、现在各通道是多少
EVENTS: deque[dict] = deque(maxlen=300)
SEQ = {"n": 0}
CH_STATE: dict[int, dict] = {}
SUBS: list[asyncio.Queue] = []
LOG_LINES: deque[str] = deque(maxlen=200)

# ── 别让电脑睡着 ──
# 面板开着的时候，人可能不在电脑前面。系统一睡眠，服务就断，手机上就永远"连不上"。
# Windows 上正经办法是 SetThreadExecutionState，跟播放视频时浏览器做的事是同一件。
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
WAKE = {"on": False, "ok": False, "why": ""}


def set_wake(on: bool) -> None:
    """注意：这个 API 按线程生效，必须在这个进程活着的线程里调用，不能丢线程池。"""
    WAKE["on"] = on
    if platform.system() != "Windows":
        WAKE["ok"] = False
        WAKE["why"] = f"{platform.system()} 上没接这个开关，用系统设置里的睡眠选项"
        return
    try:
        flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0)
        r = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        WAKE["ok"] = bool(r)
        WAKE["why"] = "" if r else "系统拒绝了这个请求"
        if r:
            note(f"保持唤醒：{'开（系统不睡、屏幕不息）' if on else '关（恢复系统默认）'}")
    except Exception as e:
        WAKE["ok"] = False
        WAKE["why"] = str(e)


def extra_persona() -> str:
    p = HERE / "persona.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def note(line: str) -> None:
    LOG_LINES.append(f"[{stamp()}] {line}")


def emit(src: str, said: str = "", cmd: dict | None = None, out: str = "", raw: str = "") -> dict:
    """往屏幕上写一行。返回这条事件，同时推给正在看面板的人。"""
    SEQ["n"] += 1
    evt = {"id": SEQ["n"], "t": stamp(), "src": src, "said": said, "cmd": cmd, "out": out, "raw": raw}
    EVENTS.append(evt)
    for q in list(SUBS):
        try:
            q.put_nowait(evt)
        except Exception:
            pass
    return evt


# ───────────────────────── 跟桥说话 ─────────────────────────


class Bridge:
    def __init__(self) -> None:
        self.session: str | None = None

    def _post(self, payload: dict, expect: bool = True) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if BRIDGE_TOKEN:
            headers["Authorization"] = f"Bearer {BRIDGE_TOKEN}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(BRIDGE_MCP, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
        if not expect:
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
            return {"error": {"message": "桥回了东西，但没有这次的答复"}}
        return json.loads(body) if body else {}

    def handshake(self) -> None:
        self.session = None
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "mediator", "version": "0.1.0"}}})
        if r.get("error"):
            raise RuntimeError(r["error"].get("message", "握手失败"))
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)

    def _call(self, name: str, args: dict) -> str:
        r = self._post({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**9, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        if r.get("error"):
            return f"err: {r['error'].get('message')}"
        content = r.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict)) or "ok"

    def call(self, name: str, args: dict) -> str:
        """桥可能会重启，那时候手里的 session 就作废了。撞上就重新握一次再试。"""
        try:
            return self._call(name, args)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 409):
                try:
                    self.handshake()
                    return self._call(name, args)
                except Exception as e2:
                    return f"err: 重新握手也没成（{e2}）"
            return f"err: 桥回了 HTTP {e.code}"
        except Exception as e:
            return f"err: 连不上桥（{e}）。桥起来了吗？"


bridge = Bridge()


# ───────────────────────── 认数据串 ─────────────────────────


BALANCED = re.compile(r"\{[^{}]*\}")


def take_commands(text: str) -> tuple[list[dict], str]:
    """把文本里的数据串挑出来，剩下的原样返回。"""
    cmds: list[dict] = []
    clean = text
    for m in BALANCED.finditer(text):
        raw = m.group(0)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if not ({"ch", "seq", "op"} & set(obj.keys())):
            continue
        cmds.append(obj)
        clean = clean.replace(raw, "")
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return cmds, clean


CH_TOOL = {1: "output_a", 2: "output_b", 3: "output_c", 4: "output_pos"}
OP_TOOL = {"stop": "output_stop", "list": "channel_list", "status": "link_status", "level": "channel_level"}


def run_command(cmd: dict) -> str:
    try:
        return _run(cmd)
    except Exception as e:
        return f"err: {e}"


def _run(cmd: dict) -> str:
    out = _dispatch(cmd)
    # SDK 只肯给模型一个工具名，真实原因在桥的窗口里
    if out.startswith("Error executing tool"):
        return out + "（真实原因看桥那个窗口的日志）"
    return out


def bcall(tool: str, args: dict) -> str:
    """
    调桥，并先记个指纹。

    桥那边每次被调用都会往这块屏幕报一笔，而中介自己也要记一笔——
    同一次动作会出现两行。指纹就是用来认出"这是我刚经手的那次"的。
    """
    try:
        SEEN.append(_fp(tool, args))
    except Exception:
        pass
    return bridge.call(tool, args)


def _dispatch(cmd: dict) -> str:
    try:
        if "op" in cmd:
            tool = OP_TOOL.get(str(cmd["op"]).lower())
            if not tool:
                return f"err: 不认识的 op={cmd['op']}"
            out = bcall(tool, {"device": cmd.get("device", "")})
            if str(cmd["op"]).lower() == "stop":
                CH_STATE.clear()
            return out
        if "seq" in cmd:
            out = bcall("output_seq", {"seq": cmd["seq"], "device": cmd.get("device", ""),
                                       "step": cmd.get("step", 1)})
            CH_STATE.clear()
            return out
        ch = int(cmd.get("ch", 1))
        if ch == 0:
            out = bcall("output_stop", {"device": cmd.get("device", "")})
            CH_STATE.clear()
            return out
        tool = CH_TOOL.get(ch)
        if not tool:
            return f"err: 不认识的 ch={ch}"
        args = {"val": cmd.get("val", 0), "device": cmd.get("device", "")}
        sec = float(cmd.get("sec", 0) or 0)
        if ch != 4:
            args["sec"] = sec
        else:
            args["sec"] = sec or 1
        out = bcall(tool, args)
        val = float(cmd.get("val", 0))
        CH_STATE[ch] = {"val": val, "until": time.time() + sec if sec > 0 else None}
        return out
    except Exception as e:
        return f"err: {e}"


async def execute(cmds: list[dict], src: str, said: str = "", raw: str = "") -> list[str]:
    """执行一串指令，每条都往屏幕上记一笔。阻塞调用丢线程池，别把面板卡住。"""
    outs: list[str] = []
    for c in cmds:
        out = await asyncio.to_thread(run_command, c)
        outs.append(out)
        note(f"{src} {json.dumps(c, ensure_ascii=False)} -> {out}")
        emit(src=src, said=said, cmd=c, out=out, raw=raw or json.dumps(c, ensure_ascii=False))
    return outs


# 中介自己经手过的指令记个指纹。桥那边也会主动报一笔过来，
# 两边都记的话屏幕上会重复两行——用指纹去重，只留中介这条。
def _fp(tool: str, args: dict) -> str:
    try:
        return tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
    except Exception:
        return tool


SEEN: deque[str] = deque(maxlen=60)


def seen_by_screen(cmd: dict) -> None:
    SEEN.append(_fp(str(cmd.get("tool") or ""), cmd.get("args") or {}))


async def api_ingest(request) -> JSONResponse:
    """
    桥主动报过来的口子。

    角色可以直接调桥（8791），那条路不经过中介，屏幕上本来什么都看不到。
    有了这个口子，不管谁调的、走哪条路，这块屏幕都记一笔。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    tool = str(body.get("tool") or "?")
    args = body.get("args") or {}
    out = str(body.get("out") or "")

    if _fp(tool, args) in SEEN:      # 中介自己刚记过，跳过
        return JSONResponse({"ok": True, "dup": True})

    cmd = {"tool": tool, "args": args}
    note(f"MCP {tool} {json.dumps(args, ensure_ascii=False)} -> {out}")
    emit(src="角色", said="", cmd=cmd, out=out, raw=json.dumps(cmd, ensure_ascii=False))
    return JSONResponse({"ok": True, "dup": False})


# ───────────────────────── 跟真模型说话 ─────────────────────────


def upstream_chat(payload: dict) -> dict:
    if not UPSTREAM:
        raise RuntimeError("没配 UPSTREAM_BASE_URL")
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
    req = urllib.request.Request(f"{UPSTREAM}/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型接口 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上模型接口：{e.reason}")


# ───────────────────────── 屏幕状态 ─────────────────────────


def live_channels() -> dict[int, dict]:
    """把到期的归零掉再返回，屏幕上的数字才不会骗人。"""
    now = time.time()
    for ch, st in list(CH_STATE.items()):
        if st.get("until") and st["until"] <= now:
            CH_STATE[ch] = {"val": 0, "until": None}
    return dict(CH_STATE)


# 电脑中继（linkrelay.py）报过来的连线状态：名字 -> 最后一次报的时间和是否连着
RELAYS: dict[str, dict] = {}
RELAY_STALE = 25  # 超过这么多秒没报，当作断了（中继每 10 秒报一次）


def relay_view() -> dict[str, bool]:
    now = time.time()
    return {name: bool(r["up"]) and now - r["t"] < RELAY_STALE for name, r in RELAYS.items()}


async def api_relay(request) -> JSONResponse:
    """中继报过来：我连上（或断开）了哪台服务器。只给面板上的灯用。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    name = str(body.get("name") or "?")[:60]
    RELAYS[name] = {"up": bool(body.get("up")), "t": time.time()}
    return JSONResponse({"ok": True})


def snapshot(since: int = 0) -> dict:
    bridge_ok = True
    try:
        link = json.loads(bridge.call("link_status", {}) or "{}")
    except Exception as e:
        bridge_ok = False
        link = {"upstream_connected": False, "channel_count": 0, "channels": [], "last_error": str(e)}
    if isinstance(link, str):
        link = {"upstream_connected": False, "channel_count": 0, "channels": [], "last_error": link}
    evts = [e for e in EVENTS if e["id"] > since]
    return {
        "ok": True,
        "now": stamp(),
        "model": UPSTREAM_MODEL or "（没配）",
        "upstream_set": bool(UPSTREAM),
        "link": link,
        "bridge_ok": bridge_ok,
        "relays": relay_view(),
        "channels": live_channels(),
        "max_seq": SEQ["n"],
        "events": evts[-60:],
        "log": list(LOG_LINES)[-40:],
        "wake": dict(WAKE),
    }


# ───────────────────────── HTTP ─────────────────────────


def reply(model: str, content: str) -> JSONResponse:
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )


async def models(request) -> JSONResponse:
    return JSONResponse({"object": "list",
                         "data": [{"id": UPSTREAM_MODEL or "mediator", "object": "model", "owned_by": "mediator"}]})


async def health(request) -> JSONResponse:
    try:
        out = await asyncio.to_thread(bridge.call, "link_status", {})
        return JSONResponse({"ok": True, "upstream_model": UPSTREAM or "（没配，只当转发器）",
                             "model": UPSTREAM_MODEL or "（没配）", "bridge": out})
    except Exception as e:
        return JSONResponse({"ok": False, "bridge": str(e)}, status_code=503)


async def chat(request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "发来的不是 JSON"}}, status_code=400)

    messages = body.get("messages") or []
    if not messages:
        return JSONResponse({"error": {"message": "messages 是空的"}}, status_code=400)
    model = body.get("model") or UPSTREAM_MODEL or "mediator"

    last = messages[-1].get("content") or ""
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))

    # 1) 有人直接塞了数据串（面板手动发的，或角色已经把判断写好了）
    cmds, rest = take_commands(last)
    if cmds:
        outs = await execute(cmds, src="角色", said=rest, raw=last[:400])
        tail = " ".join(outs)
        return reply(model, f"{rest}\n{tail}".strip() if rest else tail)

    # 2) 没有数据串：交给上游模型当角色，它要是夹了数据串就执行
    if not UPSTREAM:
        emit(src="角色", said=last[:400], out="（没配模型接口，也没在数据里看到可执行的指令）")
        return reply(model, "（没配模型接口，也没在数据里看到可执行的指令）")

    convo = list(messages)
    convo.insert(0, {"role": "system", "content": (extra_persona() + PROTOCOL_HINT).strip()})
    try:
        res = await asyncio.to_thread(upstream_chat,
                                      {"model": UPSTREAM_MODEL or model, "messages": convo, "stream": False})
    except Exception as e:
        note(f"上游出错：{e}")
        emit(src="角色", said=last[:400], out=f"上游出错：{e}")
        return reply(model, f"（中介没能把话送到模型那边：{e}）")

    content = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    cmds, clean = take_commands(content)
    if cmds:
        await execute(cmds, src="角色", said=clean[:400], raw=content[:400])
    else:
        emit(src="角色", said=clean[:400], out="（这次没带数据串）")
    return reply(model, clean or content)


async def api_state(request) -> JSONResponse:
    since = int(request.query_params.get("since", "0") or 0)
    return JSONResponse(await asyncio.to_thread(snapshot, since))


async def api_send(request) -> JSONResponse:
    """面板按键走这里：给它一段话或一段数据，它照做。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "发来的不是 JSON"}, status_code=400)

    if isinstance(body, dict) and any(k in body for k in ("ch", "seq", "op")):
        cmds: list[dict] = [body]
        text = ""
    else:
        text = str(body.get("text") or "")
        if not text.strip():
            return JSONResponse({"ok": False, "msg": "空的"}, status_code=400)
        cmds, rest = take_commands(text)
        if not cmds:
            return JSONResponse({"ok": False, "msg": "这段话里没认出数据串"})
        text = rest

    outs = await execute(cmds, src="面板", said=text[:200], raw=json.dumps(cmds, ensure_ascii=False))
    bad = all(o.startswith("err") or o.startswith("Error") for o in outs)
    return JSONResponse({"ok": not bad, "out": " ".join(outs), "state": await asyncio.to_thread(snapshot, 0)})


async def api_scan(request) -> JSONResponse:
    sec = max(1, min(60, int(request.query_params.get("sec", "8") or 8)))
    try:
        out = await asyncio.to_thread(bridge.call, "channel_scan", {"seconds": sec})
    except Exception as e:
        out = f"err: {e}"
    note(f"扫通道 {sec}s -> {out}")
    emit(src="面板", cmd={"op": "list"}, out=out)
    return JSONResponse({"ok": True, "out": out, "state": await asyncio.to_thread(snapshot, 0)})


async def api_stream(request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    SUBS.append(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            if q in SUBS:
                SUBS.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def panel(request) -> HTMLResponse:
    f = WEB / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>面板文件不见了：web/index.html</h1>", status_code=404)
    return HTMLResponse(f.read_text(encoding="utf-8"))


SAFE_SUFFIX = {".css", ".js", ".html", ".ico", ".png", ".svg", ".woff2"}


async def skin(request) -> FileResponse:
    """面板的外观文件。只认白名单后缀，且不许往上跳目录。"""
    name = request.path_params.get("name", "")
    f = (WEB / name).resolve()
    if f.suffix.lower() not in SAFE_SUFFIX or WEB.resolve() not in f.parents:
        return JSONResponse({"error": "不给这个文件"}, status_code=404)  # type: ignore[return-value]
    if not f.exists():
        return JSONResponse({"error": "没这个文件"}, status_code=404)  # type: ignore[return-value]
    return FileResponse(str(f), media_type="text/css" if f.suffix == ".css" else None)


async def api_wake(request) -> JSONResponse:
    """开关保持唤醒。必须在 event loop 线程里调，不能丢线程池——这个 API 按线程生效。"""
    on = str(request.query_params.get("on", "1")).lower() not in ("0", "false", "off", "no")
    set_wake(on)
    return JSONResponse({"ok": WAKE["ok"], "on": WAKE["on"], "why": WAKE["why"]})


class TokenGate(BaseHTTPMiddleware):
    """本机来的当自己人；外面的（手机、面板）要带钥匙。面板页面本身放行，钥匙在页面里填。"""

    async def dispatch(self, request, call_next):
        host = request.client.host if request.client else ""
        local = host in ("127.0.0.1", "::1", "localhost")
        path = request.url.path
        # /s/ 是皮肤文件（css），浏览器用 <link> 拉，带不了头，也没有敏感东西
        if MEDIATOR_TOKEN and not local and path not in ("/", "/panel", "/health") and not path.startswith("/s/"):
            # SSE 没法带头，所以也认 ?key= 查询串
            ok = request.headers.get("authorization") == f"Bearer {MEDIATOR_TOKEN}" \
                or request.query_params.get("key", "") == MEDIATOR_TOKEN
            if not ok:
                return JSONResponse({"error": {"message": "钥匙不对或没填"}}, status_code=401)
        return await call_next(request)


app = Starlette(
    routes=[
        Route("/", panel, methods=["GET"]),
        Route("/panel", panel, methods=["GET"]),
        Route("/s/{name}", skin, methods=["GET"]),
        Route("/api/wake", api_wake, methods=["GET", "POST"]),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/chat/completions", chat, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/models", models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/api/state", api_state, methods=["GET"]),
        Route("/api/send", api_send, methods=["POST"]),
        Route("/api/scan", api_scan, methods=["GET", "POST"]),
        Route("/api/ingest", api_ingest, methods=["POST"]),
        Route("/api/relay", api_relay, methods=["POST"]),
        Route("/api/stream", api_stream, methods=["GET"]),
    ]
)
app.add_middleware(TokenGate)


def main() -> None:
    try:
        bridge.handshake()
        print("桥已连上：", BRIDGE_MCP)
    except Exception as e:
        print(f"[!] 桥还没连上（{e}）。它起来了会自动连，不影响启动。")
    if not UPSTREAM:
        print("[!] 没配 UPSTREAM_*：现在只能执行直接发来的数据串，不会去找模型。")
    print(f"中介在听：http://0.0.0.0:{PORT}")
    print(f"  面板（手机浏览器也能开）：http://电脑IP:{PORT}/")
    print(f"  小手机接口填：http://电脑IP:{PORT}/v1")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
