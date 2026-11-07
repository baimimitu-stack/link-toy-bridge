"""
Intiface ⇄ MCP 桥。

一头是 Intiface Central 的 WebSocket（Buttplug 协议），一头是标准 MCP Streamable HTTP 端点。
手机上任何支持 MCP 网址的客户端（小手机 / Operit 等）都能连进来，把通道当工具调用。

对外一律用中性代号：通道 / 输出值，工具清单会进模型上下文，不写具体用途。
真实对应关系见 README，只存在本机。

设计上抄了 SillyTavern 那类桥的三条经验：
  1. Intiface 没开也照样服务，并后台自动重连（桥本身永不下线）
  2. 结构化错误：参数错 / 没通道 / 上游没连，分得清
  3. sec 到点自动归零，不会忘了关

环境变量（也可写在同目录 .env）：
  INTIFACE_URL   上游地址，默认 ws://127.0.0.1:12345
  BRIDGE_HOST    监听地址，默认 127.0.0.1（只给本机用；要让局域网设备连才改成 0.0.0.0，且必须设 BRIDGE_TOKEN）
  BRIDGE_PORT    端口，默认 8791
  BRIDGE_TOKEN   钥匙；留空则不校验（同网可信时才这么做）
  MAX_STRENGTH   输出上限 0-100，默认 100
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import time

# 这个进程只跟本机说话（127.0.0.1），但 websockets/urllib 会读环境里的
# HTTP_PROXY 等变量，把发往 127.0.0.1 的请求也丢给代理——代理拒绝本机地址，
# 表现出来就是 "proxy rejected connection: HTTP 502"。
# 所以从哪起都先把代理变量清干净，谁也不许拦本机通信。
for _k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from contextlib import asynccontextmanager

import uvicorn
from buttplug import (
    ButtplugClient,
    ButtplugDevice,
    DeviceOutputCommand,
    InputType,
    OutputType,
)
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

load_dotenv()

INTIFACE_URL = os.getenv("INTIFACE_URL", "ws://127.0.0.1:12345")
HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("BRIDGE_PORT", "8791"))
TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
MAX_STRENGTH = float(os.getenv("MAX_STRENGTH", "100"))
RECONNECT_SEC = float(os.getenv("RECONNECT_SEC", "3"))
CALL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.log")
# 中介那块屏幕的地址。角色要是绕过中介直接调桥，屏幕上就什么都看不到，
# 所以桥这边每次被调用都主动报一笔过去。留空就不报。
MEDIATOR_INGEST = os.getenv("MEDIATOR_INGEST", "http://127.0.0.1:8800/api/ingest").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

OUTPUT_NAMES = {
    "A": OutputType.VIBRATE,
    "B": OutputType.OSCILLATE,
    "C": OutputType.ROTATE,
    "D": OutputType.CONSTRICT,
}


# ───────────────────────── 上游侧 ─────────────────────────


class Link:
    """握着 buttplug 客户端。上游掉了会自己重连，桥不受影响。"""

    def __init__(self) -> None:
        self.client = ButtplugClient("bridge")
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def up(self) -> bool:
        return bool(self.client.connected)

    def devices(self) -> dict[int, ButtplugDevice]:
        return dict(self.client.devices) if self.up else {}

    async def _loop(self) -> None:
        while True:
            try:
                await self.client.connect(INTIFACE_URL)
                self.last_error = None
                log.info("上游已连：%s", INTIFACE_URL)
                try:
                    await self.client.start_scanning()
                    await asyncio.sleep(5)
                    await self.client.stop_scanning()
                except Exception as e:
                    log.warning("扫描没跑起来：%s", e)
                while self.client.connected:
                    await asyncio.sleep(1)
                log.warning("上游断了，%s 秒后重连", RECONNECT_SEC)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.info("连不上上游（%s），%s 秒后重试", self.last_error, RECONNECT_SEC)
            await asyncio.sleep(RECONNECT_SEC)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def pick(self, want: str | int | None) -> ButtplugDevice:
        devs = self.devices()
        if not devs:
            raise LookupError("现在一个通道都没有")
        if want is None:
            return devs[min(devs)]
        if isinstance(want, int) or (isinstance(want, str) and want.strip().isdigit()):
            idx = int(str(want).strip())
            if idx not in devs:
                raise LookupError(f"没有编号 {idx} 的通道")
            return devs[idx]
        key = str(want).strip().lower()
        for d in devs.values():
            if key in d.name.lower() or key in (d.display_name or "").lower():
                return d
        raise LookupError(f"没有名字里带「{want}」的通道")


link = Link()


def clamp(v: float) -> float:
    s = max(0.0, min(100.0, float(v))) / 100.0
    return min(s, MAX_STRENGTH / 100.0)


async def _auto_stop(dev: ButtplugDevice, seconds: float) -> None:
    await asyncio.sleep(seconds)
    try:
        await dev.stop()
    except Exception as e:
        log.warning("自动归零失败：%s", e)


# ───────────────────────── MCP 侧 ─────────────────────────


@asynccontextmanager
async def lifespan(server: MCPServer):
    link.start()
    log.info("MCP 就绪：http://%s:%s/mcp（鉴权：%s）", HOST, PORT, "开" if TOKEN else "关")
    yield
    log.info("桥退出")


mcp = MCPServer(
    name="bridge",
    instructions=(
        "你通过这套工具读写一组通道的输出值。"
        "输出值一律 0-100，0 是归零。动手前先用 channel_list 看有哪些通道、各自支持哪类输出；"
        "记得给 sec，到点会自动归零。不确定就先问，别自己猜着改。"
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def need_link() -> None:
    if not link.up:
        raise RuntimeError(f"上游没连上（{link.last_error or '还没连过'}）。用 link_status 查。")


def _post_ingest(payload: dict) -> None:
    """把这一笔报给中介的屏幕。同步、短超时、出错全吞——屏幕记不上有关系，桥不能为此挂掉。"""
    if not MEDIATOR_INGEST:
        return
    try:
        import urllib.request

        req = urllib.request.Request(
            MEDIATOR_INGEST,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.5):
            pass
    except Exception:
        pass  # 中介没开、端口不通、都无所谓


def tell_screen(tool: str, args: dict, out: str) -> None:
    """异步丢给线程池，不占事件循环。"""
    if not MEDIATOR_INGEST:
        return
    payload = {"tool": tool, "args": args, "out": str(out)[:300]}
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_post_ingest, payload))
    except RuntimeError:
        pass  # 没有跑着的事件循环（同步上下文），跳过


def friendly(fn):
    """
    MCP SDK 默认把工具的真实报错吞掉，只回一句 "Error executing tool xxx"。
    这对"看得见反馈"这件事是致命的：角色不知道自己为什么没成。
    所以在这里把异常就地翻译成人话字符串返回，调用方照样拿到结果。

    另外：不管谁调的（角色直接调、还是中介转过来），都往中介屏幕报一笔。
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def aw(*a, **k):
            try:
                out = await fn(*a, **k)
            except Exception as e:
                log.warning("工具 %s 出错：%s", fn.__name__, e)
                out = f"err: {e}"
            tell_screen(fn.__name__, k, out)
            return out

        return aw

    @functools.wraps(fn)
    def w(*a, **k):
        try:
            out = fn(*a, **k)
        except Exception as e:
            log.warning("工具 %s 出错：%s", fn.__name__, e)
            out = f"err: {e}"
        tell_screen(fn.__name__, k, out)
        return out

    return w


@mcp.tool(description="链路状态：上游连没连上、有几路通道、上次出错原因。排查先看这个。")
@friendly
def link_status() -> str:
    devs = link.devices()
    return json.dumps(
        {
            "upstream_connected": link.up,
            "channel_count": len(devs),
            "channels": [d.name for d in devs.values()],
            "last_error": link.last_error,
            "max_output": MAX_STRENGTH,
        },
        ensure_ascii=False,
    )


@mcp.tool(description="列出当前可用的通道：编号、名称、支持哪几类输出、有没有电量。指定通道时用这里的编号或名称。")
@friendly
def channel_list() -> str:
    need_link()
    out = []
    for idx, d in link.devices().items():
        out.append(
            {
                "index": idx,
                "name": d.name,
                "outputs": [k for k, o in OUTPUT_NAMES.items() if d.has_output(o)],
                "has_level": bool(getattr(d, "has_battery", False)),
            }
        )
    return json.dumps(out or "一个通道都没有，试试 channel_scan", ensure_ascii=False)


@mcp.tool(description="重新扫一遍通道。seconds 是扫描时长，默认 8 秒。")
@friendly
async def channel_scan(seconds: int = 8) -> str:
    need_link()
    seconds = max(1, min(60, int(seconds)))
    await link.client.start_scanning()
    await asyncio.sleep(seconds)
    await link.client.stop_scanning()
    return json.dumps({"scanned_for": seconds, "found": [d.name for d in link.devices().values()]}, ensure_ascii=False)


async def _set(dev: ButtplugDevice, otype: OutputType, val: float, sec: float) -> str:
    await dev.run_output(DeviceOutputCommand(otype, clamp(val)))
    if sec and sec > 0:
        asyncio.create_task(_auto_stop(dev, sec))
    return f"ok ch={dev.name} val={clamp(val) * 100:.0f}" + (f" sec={sec}" if sec else "")


@mcp.tool(description="设置 A 路输出。val 是 0-100，sec 秒后自动归零；不写 sec 就一直保持。device 填编号或名称，不填用第一个。")
@friendly
async def output_a(val: float, device: str = "", sec: float = 0) -> str:
    need_link()
    dev = link.pick(device.strip() or None)
    if not dev.has_output(OutputType.VIBRATE):
        raise ValueError(f"{dev.name} 不支持 A 路输出")
    return await _set(dev, OutputType.VIBRATE, val, sec)


@mcp.tool(description="设置 B 路输出。参数同 output_a：val 0-100，sec 秒，device 编号或名称。")
@friendly
async def output_b(val: float, device: str = "", sec: float = 0) -> str:
    need_link()
    dev = link.pick(device.strip() or None)
    if not dev.has_output(OutputType.OSCILLATE):
        raise ValueError(f"{dev.name} 不支持 B 路输出")
    return await _set(dev, OutputType.OSCILLATE, val, sec)


@mcp.tool(description="设置 C 路输出。参数同 output_a：val 0-100，sec 秒，device 编号或名称。")
@friendly
async def output_c(val: float, device: str = "", sec: float = 0) -> str:
    need_link()
    dev = link.pick(device.strip() or None)
    if not dev.has_output(OutputType.ROTATE):
        raise ValueError(f"{dev.name} 不支持 C 路输出")
    return await _set(dev, OutputType.ROTATE, val, sec)


@mcp.tool(description="设置行程位置。val 0-100，sec 是走完这段的秒数，device 编号或名称。")
@friendly
async def output_pos(val: float, device: str = "", sec: float = 1.0) -> str:
    need_link()
    dev = link.pick(device.strip() or None)
    if not dev.has_output(OutputType.POSITION_WITH_DURATION):
        raise ValueError(f"{dev.name} 不支持行程输出")
    ms = int(max(0.1, float(sec)) * 1000)
    await dev.run_output(DeviceOutputCommand(OutputType.POSITION_WITH_DURATION, clamp(val), duration=ms))
    return f"ok ch={dev.name} pos={clamp(val) * 100:.0f} sec={sec}"


@mcp.tool(
    description=(
        "按顺序放一串值。seq 是 0-100 的数组，比如 [20,60,20,80]；"
        "step 是每段几秒，默认 1 秒。放完自动归零。device 填编号或名称。"
    )
)
@friendly
async def output_seq(seq: list[int] | str, device: str = "", step: float = 1.0) -> str:
    need_link()
    if isinstance(seq, str):
        seq = [int(x) for x in seq.replace("，", ",").split(",") if x.strip()]
    if not seq:
        raise ValueError("seq 是空的")
    dev = link.pick(device.strip() or None)
    if not dev.has_output(OutputType.VIBRATE):
        raise ValueError(f"{dev.name} 不支持 A 路输出")
    step = max(0.2, float(step))
    for v in seq:
        await dev.run_output(DeviceOutputCommand(OutputType.VIBRATE, clamp(v)))
        await asyncio.sleep(step)
    await dev.stop()
    return f"ok ch={dev.name} seq={len(seq)} total={len(seq) * step:.1f}s"


@mcp.tool(description="立刻归零。device 不填就是所有通道全归零。")
@friendly
async def output_stop(device: str = "") -> str:
    need_link()
    if device.strip():
        dev = link.pick(device.strip())
        await dev.stop()
        return f"ok stopped {dev.name}"
    await link.client.stop_all_devices()
    return f"ok stopped all ({len(link.devices())})"


@mcp.tool(description="查电量，返回百分比整数。device 不填查第一个。")
@friendly
async def channel_level(device: str = "") -> str:
    need_link()
    dev = link.pick(device.strip() or None)
    if not dev.has_input(InputType.BATTERY):
        raise ValueError(f"{dev.name} 报不了电量")
    level = await dev.battery()
    return f"ok ch={dev.name} level={level * 100:.0f}%"


# ───────────────────────── 鉴权 ─────────────────────────


class TokenGate(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        # 本机来的（Claude Code 等）当自己人，不查钥匙；外面的（手机）必须带
        host = request.client.host if request.client else ""
        # 房间客户端转进来的请求（带 X-Room-Relay）看着也是 127.0.0.1，但其实来自公网，必须查钥匙
        local = host in ("127.0.0.1", "::1", "localhost") and not request.headers.get("x-room-relay")
        if self.token and not local and request.url.path not in ("/health", "/healthz"):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {self.token}":
                return JSONResponse({"error": "钥匙不对或没填"}, status_code=401)
        return await call_next(request)


class CallLogger:
    """把收到的 tools/call 原样记进 calls.log（只记工具名和参数，不记钥匙），查"角色到底发了什么"用。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/mcp":
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []

        async def tap():
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
            return msg

        try:
            return await self.app(scope, tap, send)
        finally:
            try:
                data = json.loads(b"".join(chunks) or b"{}")
                for m in data if isinstance(data, list) else [data]:
                    if m.get("method") == "tools/call":
                        who = (scope.get("client") or ("?",))[0]
                        line = json.dumps(m.get("params", {}), ensure_ascii=False)
                        with open(CALL_LOG, "a", encoding="utf-8") as f:
                            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {who} {line}\n")
            except Exception:
                pass


def build_app() -> Starlette:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", host=HOST)
    app.add_middleware(CallLogger)

    async def _health(request) -> JSONResponse:
        return JSONResponse(
            {"ok": True, "upstream": link.up, "channels": len(link.devices()), "auth": bool(TOKEN)}
        )

    app.router.add_route("/health", _health, methods=["GET"])
    if TOKEN:
        app.add_middleware(TokenGate, token=TOKEN)
    return app


def main() -> None:
    logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.info("桥启动：上游=%s，监听=%s:%s，鉴权=%s", INTIFACE_URL, HOST, PORT, "开" if TOKEN else "关")
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
