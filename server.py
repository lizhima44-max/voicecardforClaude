#!/usr/bin/env python3
"""voice-card-mcp —— 一个 MCP 服务器，把文字合成语音，并渲染成微信风格的语音条卡片。

跑起来：
    pip install -r requirements.txt
    cp .env.example .env && 填好密钥
    python server.py

默认监听 http://0.0.0.0:8000
    /mcp          MCP streamable HTTP 端点（claude.ai 连接器填这个）
    /audio/xxx.mp3 生成的音频（卡片会来 fetch）
    /healthz      健康检查

详细说明见 README.md。
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
from urllib.parse import urlparse

import anyio
import uvicorn

# 先把 .env 读进环境变量，再 import 任何读配置的模块（tts 在 import 时就取值了）。
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # 没装 python-dotenv 就只认系统环境变量，不影响运行
    pass

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import tts

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 配置 ────────────────────────────────────────────────────────────────
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))
MODE = os.environ.get("MCP_MODE", "streamable").lower()   # streamable | sse
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

# 无会话模式：客户端不回传 mcp-session-id 时必须开，否则每个请求都被
# "Missing session ID" 打回。claude.ai 官方连接器会正常管理会话，可以不开。
STATELESS = os.environ.get("MCP_STATELESS", "0") == "1"

# 卡片要 fetch 音频，宿主的 iframe CSP 得放行这个域名。
# 直接从 AUDIO_BASE_URL 推出 origin，省得两处配置写不一致。
_parsed = urlparse(tts.AUDIO_BASE_URL)
ASSET_ORIGIN = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed.netloc else ""


# ── MCP 实例 ────────────────────────────────────────────────────────────
# enable_dns_rebinding_protection=False：那是给本地 stdio/localhost 场景的防护，
# 公网部署时它只会让所有外部 Host 吃 421 Invalid Host。真正的门是下面的 token。
mcp = FastMCP(
    "voice-card",
    stateless_http=STATELESS,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ── 补丁一：同步工具丢线程池 ────────────────────────────────────────────
# FastMCP 对同步函数是直接 `return fn(...)`，也就是在 asyncio 事件循环的
# 主线程上跑。TTS 要等几秒网络 IO，这几秒里整台服务器对所有请求都装聋——
# 连 initialize / tools/list 都超时，表现成"工具明明在，却调不动"。
# 包一层，把同步工具自动搬到线程池里。functools.wraps 保住签名，
# FastMCP 生成的 inputSchema 不受影响。
_tool_orig = mcp.tool


def _tool_nonblocking(*d_args, **d_kwargs):
    deco = _tool_orig(*d_args, **d_kwargs)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            return deco(fn)

        @functools.wraps(fn)
        async def async_fn(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        return deco(async_fn)

    return wrap


mcp.tool = _tool_nonblocking


# ── 补丁二：声明 UI 扩展能力 ────────────────────────────────────────────
# 宿主要在 initialize 的 capabilities.experimental 里看到这个 key，
# 才会认这台服务器能给 HTML 卡片。SDK 还没有正式 API，只能打补丁；
# 整段 try 包住——声明失败最多是卡片不渲染，不该拖垮服务启动。
try:
    _orig_opts = mcp._mcp_server.create_initialization_options

    def _patched_opts():
        opts = _orig_opts()
        try:
            cap = opts.capabilities
            exp = cap.experimental if cap.experimental is not None else {}
            exp["io.modelcontextprotocol/ui"] = {}
            try:
                cap.experimental = exp
            except Exception:                       # pydantic 冻结模型的退路
                opts.capabilities = cap.model_copy(update={"experimental": exp})
        except Exception:
            pass
        return opts

    mcp._mcp_server.create_initialization_options = _patched_opts
except Exception as e:                              # noqa: BLE001
    print(f"⚠️  UI capability 声明失败（卡片可能不渲染）: {e}", flush=True)


# ── 卡片的 CSP 白名单 ───────────────────────────────────────────────────
# 卡片跑在宿主的沙箱 iframe 里，默认 CSP 会拦掉它对外的 fetch。
# 这里显式声明"我要连这个域名"，宿主才会把它加进 iframe 的 CSP。
#   ui.csp.*            → MCP UI / claude.ai 认这套
#   openai/widgetCSP.*  → OpenAI Apps SDK 认这套（下划线命名，别写错）
# 两套都写，一份卡片两边都能用。
#
# 🚨 千万不要加 "domain" / "openai/widgetDomain" 字段。那会让宿主拿它当
#    卡片自己的沙箱源，一旦跟实际不符，所有卡片直接 "Failed to load the
#    MCP app"。不声明 = 用宿主默认沙箱域，这才是能跑的配置。
WIDGET_META = {
    "ui": {
        "csp": {
            "resourceDomains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
            "connectDomains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
        },
    },
    "openai/widgetCSP": {
        "resource_domains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
        "connect_domains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
    },
}

with open(os.path.join(HERE, "voice_card.html"), encoding="utf-8") as f:
    VOICE_CARD_HTML = f.read()


# ── 注册卡片资源 ────────────────────────────────────────────────────────
# mime_type 里的 ;profile=mcp-app 是关键，宿主靠它认出"这是个卡片"。
#
# 💡 改了 HTML 又发现宿主还在渲染旧版？连接器会按 URI 缓存卡片壳子。
#    办法是升一个版本号 URI（voice-card-v2），**同时保留旧 URI 做别名**
#    返回同一份 HTML——已经缓存住旧地址的客户端照样能拿到修好的版本。
@mcp.resource("ui://voice-card-v1", mime_type="text/html;profile=mcp-app", meta=WIDGET_META)
def voice_card_ui() -> str:
    return VOICE_CARD_HTML


@mcp.resource("ui://voice-card", mime_type="text/html;profile=mcp-app", meta=WIDGET_META)
def voice_card_ui_legacy() -> str:
    """旧 URI 别名，返回同一份最新 HTML。"""
    return VOICE_CARD_HTML


# ── 注册工具 ────────────────────────────────────────────────────────────
# meta 里两种写法都给：ui/resourceUri（点分）和 ui.resourceUri（嵌套），
# 不同宿主读的字段不一样，都写上最省事。
# structured_output=True：让返回的 dict 进 structuredContent，卡片就是从
# 那里拿数据的。少了这个，卡片永远收不到东西。
@mcp.tool(
    meta={"ui/resourceUri": "ui://voice-card-v1",
          "ui": {"resourceUri": "ui://voice-card-v1"}},
    structured_output=True,
)
def voice_card(text: str, voice_id: str = "", label: str = "",
               provider: str = "", autoplay: bool = False) -> dict[str, str]:
    """把一句话合成语音，渲染成可播放的语音条卡片。

    text: 要念的文字
    voice_id: 指定音色，留空用环境变量里的默认音色
    label: 卡片上显示的标题，留空自动取文字开头
    provider: elevenlabs / volcano，留空用 TTS_PROVIDER
    autoplay: True 则卡片加载完自动播一次
    """
    r = tts.synthesize(text, voice_id, provider=provider, embed=False)
    if not r.startswith("OK: "):
        # 错误也回卡片，让用户在界面上直接看到原因，而不是一句干巴巴的失败
        return {"error": r[:160], "label": label or "合成失败"}
    url = r[4:].strip()

    # 🚨 只回 URL，绝对不要把 base64 音频塞进返回值。
    #    5 万+ 字符的响应体会把宿主的卡片加载器直接搞崩
    #    （"Failed to load the MCP app"），比播不出声更糟。
    #    音频由卡片自己 fetch —— 响应体永远保持轻量。
    return {
        "audio": url,
        "label": label or (text[:18] + ("…" if len(text) > 18 else "")),
        "autoplay": "1" if autoplay else "",
    }


@mcp.tool()
def voice_url(text: str, voice_id: str = "", provider: str = "") -> str:
    """只合成语音、返回 mp3 链接，不渲染卡片（给不支持 HTML 卡片的客户端用）。"""
    return tts.synthesize(text, voice_id, provider=provider, embed=False)


# ── 鉴权中间件 ──────────────────────────────────────────────────────────
class AuthMiddleware:
    """最朴素的 Bearer token 门。

    放行两类请求：
      · /healthz 和 /audio/*（音频得让卡片匿名 fetch，否则没法播）
      · Authorization: Bearer <MCP_AUTH_TOKEN> 正确的
    MCP_AUTH_TOKEN 留空 = 完全裸奔，只适合本机调试。
    """

    OPEN_PREFIXES = ("/audio", "/healthz")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not AUTH_TOKEN:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path.startswith(self.OPEN_PREFIXES):
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        got = headers.get("authorization", "")
        if got == f"Bearer {AUTH_TOKEN}":
            return await self.app(scope, receive, send)

        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


async def healthz(request: Request):
    return JSONResponse({
        "ok": True,
        "provider": tts.PROVIDER,
        "asset_origin": ASSET_ORIGIN,
        "mode": MODE,
    })


# ── 启动 ────────────────────────────────────────────────────────────────
def build_app():
    os.makedirs(tts.AUDIO_DIR, exist_ok=True)

    if MODE == "sse":
        # SSE 传输（老客户端、Claude Code 的 --transport sse 用这个）
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import Response

        sse = SseServerTransport("/messages/")
        server = mcp._mcp_server

        async def handle_sse(request: Request):
            async with sse.connect_sse(request.scope, request.receive,
                                       request._send) as streams:
                await server.run(streams[0], streams[1],
                                 server.create_initialization_options())
            # 必须返回一个 Response：SSE 的响应已经由 connect_sse 直接发完了，
            # 返回 None 会让 Starlette 去 await None(...) 而报 TypeError。
            return Response(status_code=204)

        return Starlette(routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/healthz", endpoint=healthz),
            Mount("/audio", app=StaticFiles(directory=tts.AUDIO_DIR), name="audio"),
            Mount("/messages", app=sse.handle_post_message),
        ])

    # streamable HTTP（默认，claude.ai 连接器走这条）
    app = mcp.streamable_http_app()
    # 插到路由表最前面：streamable app 自带顶层 lifespan（会话管理器住在里面），
    # 重新 Mount 进一个新的 Starlette 会把那个 lifespan 丢掉，服务直接起不来。
    app.router.routes.insert(0, Route("/healthz", endpoint=healthz))
    app.router.routes.insert(
        0, Mount("/audio", app=StaticFiles(directory=tts.AUDIO_DIR), name="audio"))
    return app


def main():
    if not AUTH_TOKEN:
        print("⚠️  MCP_AUTH_TOKEN 未设置：谁拿到地址都能调你的工具、烧你的 TTS 额度。",
              flush=True)
    if not ASSET_ORIGIN:
        print("⚠️  AUDIO_BASE_URL 解析不出域名，CSP 白名单会是空的，卡片取不到音频。",
              flush=True)
    if ASSET_ORIGIN.startswith("http://") and "127.0.0.1" not in ASSET_ORIGIN \
            and "localhost" not in ASSET_ORIGIN:
        print("⚠️  AUDIO_BASE_URL 是 http://：宿主页面是 https，混合内容会被浏览器拦。"
              "上公网请务必配 https。", flush=True)

    print(f"🔊 voice-card-mcp({MODE}) → http://{HOST}:{PORT}"
          f"{'/mcp' if MODE != 'sse' else '/sse'}", flush=True)
    print(f"   TTS provider = {tts.PROVIDER}", flush=True)
    print(f"   audio        = {tts.AUDIO_DIR}  →  {tts.AUDIO_BASE_URL}", flush=True)

    uvicorn.run(AuthMiddleware(build_app()), host=HOST, port=PORT)


if __name__ == "__main__":
    sys.exit(main())
