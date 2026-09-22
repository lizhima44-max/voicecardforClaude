#!/usr/bin/env python3
"""voice-card-mcp · 单文件版
===========================================================================
一个文件 = 一台完整的 MCP 服务器：把文字合成语音，渲染成微信风格的语音条卡片。
（卡片 HTML 也在这个文件里，往下翻到 VOICE_CARD_HTML。）

三步跑起来
    pip install "mcp>=1.9.0,<2" requests uvicorn
    export TTS_PROVIDER=elevenlabs ELEVENLABS_API_KEY=xxx ELEVENLABS_VOICE_ID=xxx
    export AUDIO_BASE_URL=https://你的域名/audio        # 必须公网 https，见下方说明
    python voice_card_mcp.py

然后在 claude.ai → 设置 → 连接器，填 https://你的域名/mcp，
Authorization 填 Bearer <MCP_AUTH_TOKEN>。

全部配置项都是环境变量，代码里没有任何真实密钥。
MIT License。致谢见文末 CREDITS。
===========================================================================
"""

from __future__ import annotations

import base64
import functools
import hashlib
import inspect
import os
import sys
import time
import uuid
from urllib.parse import urlparse

import anyio
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# ═══════════════════════════════════════════════════════════════════════
# 一、配置（全部来自环境变量）
# ═══════════════════════════════════════════════════════════════════════

TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "elevenlabs").strip().lower()

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

VOLCANO_APP_ID = os.environ.get("VOLCANO_APP_ID", "")
VOLCANO_ACCESS_TOKEN = os.environ.get("VOLCANO_ACCESS_TOKEN", "")
VOLCANO_VOICE_ID = os.environ.get("VOLCANO_VOICE_ID", "")
VOLCANO_CLUSTER = os.environ.get("VOLCANO_CLUSTER", "volcano_icl")

AUDIO_DIR = os.environ.get("AUDIO_DIR", "./public/audio")
# 🚨 全项目最容易配错的一项。要同时满足：
#    ① 公网可达（卡片跑在别人的网页里，localhost 它够不着）
#    ② https（宿主页面是 https，http 资源算混合内容，浏览器直接拦）
#    ③ 域名会被自动写进卡片的 CSP 白名单，填错就一定播不出声
AUDIO_BASE_URL = os.environ.get("AUDIO_BASE_URL", "http://127.0.0.1:8000/audio").rstrip("/")
AUDIO_KEEP_DAYS = int(os.environ.get("AUDIO_KEEP_DAYS", "7"))

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
# 客户端不回传 mcp-session-id 时才开（症状：永远 "Missing session ID"）
STATELESS = os.environ.get("MCP_STATELESS", "0") == "1"

_p = urlparse(AUDIO_BASE_URL)
ASSET_ORIGIN = f"{_p.scheme}://{_p.netloc}" if _p.netloc else ""


# ═══════════════════════════════════════════════════════════════════════
# 二、TTS：文字 → mp3 文件 → 公网 URL
# ═══════════════════════════════════════════════════════════════════════

def _save_mp3(audio_bytes: bytes, seed: str, prefix: str) -> str:
    os.makedirs(AUDIO_DIR, exist_ok=True)
    fname = f"{prefix}_{hashlib.md5(seed.encode()).hexdigest()[:12]}_{int(time.time())}.mp3"
    with open(os.path.join(AUDIO_DIR, fname), "wb") as f:
        f.write(audio_bytes)
    _cleanup()
    return f"{AUDIO_BASE_URL}/{fname}"


def _cleanup() -> None:
    """清掉过期生成物。只删自己的前缀——AUDIO_DIR 万一指到共用目录上，
    没这层判断就是一场事故。"""
    if AUDIO_KEEP_DAYS <= 0:
        return
    cutoff = time.time() - AUDIO_KEEP_DAYS * 86400
    try:
        for name in os.listdir(AUDIO_DIR):
            if not name.startswith(("el_", "vol_")):
                continue
            path = os.path.join(AUDIO_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def _tts_elevenlabs(text: str, voice_id: str = "") -> str:
    if not ELEVENLABS_API_KEY:
        return "ERROR: ELEVENLABS_API_KEY 未设置"
    vid = voice_id or ELEVENLABS_VOICE_ID
    if not vid:
        return "ERROR: ELEVENLABS_VOICE_ID 未设置"
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": text, "model_id": ELEVENLABS_MODEL,
                  "voice_settings": {"stability": 0.4, "similarity_boost": 0.75,
                                     "style": 0.4, "use_speaker_boost": True,
                                     "speed": 1.0}},
            timeout=60)
        if r.status_code == 429 or "quota_exceeded" in r.text:
            return "ERROR: ElevenLabs 额度用完了"
        if not r.ok:
            return f"FAIL {r.status_code}: {r.text[:200]}"
        return "OK: " + _save_mp3(r.content, text + vid, "el")
    except Exception as e:                                  # noqa: BLE001
        return f"ERROR: {e}"


def _tts_volcano(text: str, voice_id: str = "") -> str:
    if not VOLCANO_APP_ID or not VOLCANO_ACCESS_TOKEN:
        return "ERROR: VOLCANO_APP_ID / VOLCANO_ACCESS_TOKEN 未设置"
    vid = voice_id or VOLCANO_VOICE_ID
    if not vid:
        return "ERROR: VOLCANO_VOICE_ID 未设置"
    try:
        r = requests.post(
            "https://openspeech.bytedance.com/api/v1/tts",
            # ⚠️ 分号不是笔误：火山这个接口要 "Bearer;<token>"
            headers={"Authorization": f"Bearer;{VOLCANO_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json={"app": {"appid": VOLCANO_APP_ID, "token": VOLCANO_ACCESS_TOKEN,
                          "cluster": VOLCANO_CLUSTER},
                  "user": {"uid": "voice-card"},
                  "audio": {"voice_type": vid, "encoding": "mp3",
                            "rate": 24000, "speed_ratio": 1.0},
                  "request": {"reqid": str(uuid.uuid4()), "text": text,
                              "text_type": "plain", "operation": "query"}},
            timeout=60)
        if not r.ok:
            return f"FAIL {r.status_code}: {r.text[:200]}"
        data = r.json()
        if data.get("code") != 3000:                        # 3000 才是成功
            return f"TTS ERROR code={data.get('code')}: {data.get('message','')}"
        return "OK: " + _save_mp3(base64.b64decode(data["data"]), text + vid, "vol")
    except Exception as e:                                  # noqa: BLE001
        return f"ERROR: {e}"


def synthesize(text: str, voice_id: str = "", provider: str = "") -> str:
    """成功返回 "OK: <url>"，失败返回 ERROR/FAIL 开头的一行字。"""
    text = (text or "").strip()
    if not text:
        return "ERROR: text 不能为空"
    if len(text) > 2000:
        return "ERROR: text 太长了（>2000 字），分段合成吧"
    p = (provider or TTS_PROVIDER).lower()
    if p in ("volcano", "doubao", "bytedance"):
        return _tts_volcano(text, voice_id)
    return _tts_elevenlabs(text, voice_id)


# ═══════════════════════════════════════════════════════════════════════
# 三、卡片 HTML（整份内嵌在这里）
# ═══════════════════════════════════════════════════════════════════════

VOICE_CARD_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  /* ── 配色：亮色 / 暗色两套，跟随系统，也支持宿主用 data-theme 强制 ── */
  :root{
    --card1:#ffe3da; --card2:#f9c7ba; --edge:#f2b3a3; --ink:#7a3b2e;
    --btn1:#ffb3a0; --btn2:#f0876c; --rose:#e8836a; --sub:#b47c6c;
    --track:rgba(255,255,255,.55);
  }
  @media (prefers-color-scheme:dark){
    :root{--card1:#3d1f18;--card2:#2e1710;--edge:#5a2f24;--ink:#f6d0c4;
      --btn1:#c96b52;--btn2:#a3492f;--rose:#f0a58e;--sub:#c79a8c;--track:rgba(255,255,255,.15);}
  }
  :root[data-theme="dark"]{--card1:#3d1f18;--card2:#2e1710;--edge:#5a2f24;--ink:#f6d0c4;
    --btn1:#c96b52;--btn2:#a3492f;--rose:#f0a58e;--sub:#c79a8c;--track:rgba(255,255,255,.15);}
  :root[data-theme="light"]{--card1:#ffe3da;--card2:#f9c7ba;--edge:#f2b3a3;--ink:#7a3b2e;
    --btn1:#ffb3a0;--btn2:#f0876c;--rose:#e8836a;--sub:#b47c6c;--track:rgba(255,255,255,.55);}

  *{box-sizing:border-box;margin:0;padding:0}
  html,body{overflow:hidden}                 /* 卡片在 iframe 里，绝不能出滚动条 */
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       background:transparent}               /* 透明背景，融进宿主的气泡 */

  .bar{display:flex;align-items:center;gap:13px;width:320px;max-width:100%;margin:0 auto;
    padding:12px 16px;border-radius:22px;overflow:hidden;
    background:linear-gradient(150deg,var(--card1),var(--card2));border:1px solid var(--edge);
    box-shadow:0 10px 26px rgba(180,90,60,.22),inset 0 1px 0 rgba(255,255,255,.4)}

  /* 播放按钮：CSS 三角形当"播放"，两根竖条当"暂停"，不用图标字体也不用图片 */
  .play{flex:none;width:46px;height:46px;border-radius:50%;border:0;cursor:pointer;position:relative;
    background:radial-gradient(120% 120% at 50% 30%,var(--btn1),var(--btn2));
    box-shadow:0 4px 10px rgba(180,90,60,.28);transition:transform .12s}
  .play:active{transform:scale(.92)}
  .play::before{content:"";position:absolute;top:50%;left:53%;transform:translate(-50%,-50%);
    width:0;height:0;border-left:15px solid #fff;border-top:9px solid transparent;border-bottom:9px solid transparent}
  .play.playing::before{border:0;width:13px;height:13px;left:50%;
    background:linear-gradient(90deg,#fff 0 34%,transparent 34% 66%,#fff 66%)}

  .mid{flex:1;min-width:0}
  .lab{font-size:13.5px;color:var(--ink);font-weight:700;letter-spacing:.2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .wave{display:flex;align-items:center;gap:3px;height:20px;margin:7px 0 5px;cursor:pointer}
  .wave i{flex:1;background:var(--track);border-radius:2px;transition:background .1s;min-width:2px}
  .wave i.on{background:var(--rose)}
  .time{font-size:11px;color:var(--sub);font-variant-numeric:tabular-nums}
  .labRow{display:flex;align-items:center;gap:6px}
  .labRow .lab{flex:1}
  .dl{flex:none;width:19px;height:19px;border:0;border-radius:50%;cursor:pointer;
    background:var(--track);color:var(--ink);text-decoration:none;
    display:flex;align-items:center;justify-content:center;transition:transform .12s}
  .dl:active{transform:scale(.88)}
  .dl svg{width:11px;height:11px;display:block}
  .dl.open{background:var(--rose);color:#fff}
  /* 展开的链接行：沙箱拦下载时的兜底出口 */
  .urlRow{display:none;align-items:center;gap:5px;margin-top:6px}
  .urlRow.show{display:flex}
  .urlRow input{flex:1;min-width:0;font-size:10px;padding:3px 6px;border-radius:7px;
    border:1px solid var(--edge);background:var(--track);color:var(--ink);
    font-family:inherit;outline:none}
  .urlRow button{flex:none;font-size:10px;padding:3px 8px;border:0;border-radius:7px;
    cursor:pointer;background:var(--rose);color:#fff;font-family:inherit;white-space:nowrap}
  .urlRow button:active{transform:scale(.92)}
</style>
</head>
<body>
<div class="bar" id="bar">
  <button class="play" id="play" aria-label="播放"></button>
  <div class="mid">
    <div class="labRow">
      <div class="lab" id="lab">语音消息</div>
      <a class="dl" id="dl" aria-label="下载" title="保存到本地" target="_blank"
         rel="noopener" download><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/>
         <path d="m6 11 6 6 6-6"/><path d="M5 21h14"/></svg></a>
    </div>
    <div class="wave" id="wave"></div>
    <div class="time" id="time">0:00 / 0:00</div>
    <div class="urlRow" id="urlRow">
      <input id="urlIn" readonly aria-label="语音链接">
      <button id="cp" type="button">复制</button>
    </div>
  </div>
</div>
<!-- 降级用的 <audio>：Web Audio 不可用时才会被启用 -->
<audio id="au" preload="metadata" style="display:none"></audio>
<script>
/* ===========================================================================
 * 语音条卡片（MCP UI / ext-apps widget）
 *
 * 宿主（claude.ai / ChatGPT 等）把这个文件塞进一个沙箱 iframe，然后用
 * postMessage 把工具返回的 structuredContent 发进来。我们要做的事只有三件：
 *   1) 握手：告诉宿主"我准备好了"（ui/initialize + initialized）
 *   2) 收数据：监听 message，拿到 {audio, label, autoplay}
 *   3) 放声音：三档降级去播（见下面 loadAudio 的注释）
 *
 * ⚠️ 为什么不直接 <audio src="https://.../x.mp3"> ？
 *    沙箱 iframe 的 CSP 常把 media-src 掐死，跨域 mp3 直接赋给 <audio src>
 *    会静默失败（表现为"加载失败"、点了没反应）。而 connect-src（fetch）
 *    通常更宽松。所以正确姿势是：fetch 拿字节 → 本地播。
 * =========================================================================== */

var btn=document.getElementById("play"),lab=document.getElementById("lab"),
    wave=document.getElementById("wave"),timeEl=document.getElementById("time"),
    dlBtn=document.getElementById("dl"),au=document.getElementById("au");

var BARS=22,bars=[];
/* 静态"波形"：高低固定的一排竖条，播放进度点亮。
   真波形要解码后算 RMS，成本高、在小卡片上也看不出差别——这排装饰够用。 */
var HS=[9,14,7,17,11,20,8,15,10,18,6,13,16,9,19,12,7,15,10,14,8,17];
for(var i=0;i<BARS;i++){var b=document.createElement("i");
  b.style.height=HS[i%HS.length]+"px";wave.appendChild(b);bars.push(b);}

/* ── 播放状态机 ─────────────────────────────────────────────
   两种后端：
     mode="webaudio" → AudioContext + BufferSource（首选，完全不碰 media-src）
     mode="element"  → <audio> + blob: URL（降级）
   统一暴露 duration() / current() / startPlayback() / pausePlayback()      */
var mode="webaudio";
var audioCtx=null,audioBuffer=null,audioBytes=null,source=null;
var playing=false,startedAt=0,offset=0,raf=0,currentUrl="",stopping=false;

function fmt(s){s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+":"+("0"+(s%60)).slice(-2);}

function duration(){
  if(mode==="element")return isFinite(au.duration)?au.duration:0;
  return audioBuffer?audioBuffer.duration:0;
}
function current(){
  if(mode==="element")return au.currentTime||0;
  var c=playing&&audioCtx?offset+(audioCtx.currentTime-startedAt):offset,d=duration();
  return d?Math.min(c,d):c;
}
function paint(){
  var d=duration(),c=current(),p=d?c/d:0,lit=Math.round(p*BARS);
  for(var i=0;i<BARS;i++)bars[i].className=i<lit?"on":"";
  timeEl.textContent=fmt(c)+" / "+fmt(d);
  /* Web Audio 没有 timeupdate 事件，进度得自己用 rAF 推 */
  if(playing&&mode==="webaudio")raf=requestAnimationFrame(paint);
}

function stopSource(){if(source){stopping=true;try{source.stop();}catch(e){}source=null;}}
function finish(){playing=false;offset=0;source=null;btn.className="play";
  cancelAnimationFrame(raf);paint();}

/* 解码：decodeAudioData 不需要用户手势，所以拿到字节就先解，
   这样时长能立刻显示出来，而不是等第一次点播放。
   （只有 AudioContext.resume() 需要手势，那步留到点播放时做。）   */
function ensureDecoded(){
  if(audioBuffer)return Promise.resolve(audioBuffer);
  if(!audioBytes)return Promise.reject(new Error("audio not loaded"));
  var AC=window.AudioContext||window.webkitAudioContext;
  if(!AC)return Promise.reject(new Error("Web Audio unsupported"));
  if(!audioCtx)audioCtx=new AC();
  /* slice(0)：decodeAudioData 会"吃掉"(detach) 传进去的 ArrayBuffer，
     留个副本才能重复解码 */
  return audioCtx.decodeAudioData(audioBytes.slice(0)).then(function(buf){
    audioBuffer=buf;paint();return buf;
  });
}

function startPlayback(){
  if(mode==="element"){
    var pr=au.play();
    return (pr&&pr.then)?pr:Promise.resolve();
  }
  return ensureDecoded().then(function(){
    return audioCtx.resume();          /* 用户点了按钮，此时 resume 合法 */
  }).then(function(){
    stopSource();stopping=false;
    source=audioCtx.createBufferSource();
    source.buffer=audioBuffer;
    source.connect(audioCtx.destination);
    startedAt=audioCtx.currentTime;
    source.onended=function(){if(stopping){stopping=false;return;}finish();};
    source.start(0,Math.min(offset,Math.max(0,audioBuffer.duration-.01)));
    playing=true;btn.className="play playing";paint();
  });
}
function pausePlayback(){
  if(mode==="element"){au.pause();return;}
  if(!playing)return;
  offset=current();playing=false;btn.className="play";
  cancelAnimationFrame(raf);stopSource();paint();
}

btn.addEventListener("click",function(){
  if(playing){pausePlayback();return;}
  startPlayback().catch(function(e){
    lab.textContent="语音播放失败";btn.className="play";
    console.warn("[voice-card] play failed:",e);
  });
});

/* 点波形跳进度 */
wave.addEventListener("click",function(ev){
  var d=duration();if(!d)return;
  var r=wave.getBoundingClientRect(),p=Math.max(0,Math.min(1,(ev.clientX-r.left)/r.width));
  if(mode==="element"){au.currentTime=p*d;paint();return;}
  var was=playing;if(was)pausePlayback();
  offset=p*d;paint();
  if(was)startPlayback().catch(function(){lab.textContent="语音播放失败";});
});

/* <audio> 降级模式的事件（webaudio 模式下这些不会触发） */
au.addEventListener("play",function(){playing=true;btn.className="play playing";});
au.addEventListener("pause",function(){playing=false;btn.className="play";});
au.addEventListener("ended",function(){playing=false;btn.className="play";au.currentTime=0;paint();});
au.addEventListener("timeupdate",paint);
au.addEventListener("loadedmetadata",paint);

/* ── 下载：宿主 iframe 的 sandbox 决定了你能做什么 ──────────────
   实测四种组合（结论别靠猜，自己也可以复现）：

     sandbox                                    点下载的结果
     allow-scripts                              什么都不发生，连报错都没有
     + allow-popups                             开新标签页（不是下载）
     + allow-same-origin（无 downloads）         静默失败
     + allow-downloads                          才真的下载

   为什么两头堵：跨域时浏览器**忽略** download 属性，退化成普通导航，
   要 allow-popups；同源时 download 属性生效、走下载路径，要 allow-downloads。
   JS 造 blob + a.click() 更早就被拦，那条路根本不用试。

   所以这里做双保险：原生 <a> 的行为照留（宿主给了权限就正常开新标签），
   同时**就地把完整链接展开出来**，一键复制——粘到浏览器里打开谁都拦不住。 */
var urlRow=document.getElementById("urlRow"),urlIn=document.getElementById("urlIn"),
    cpBtn=document.getElementById("cp"),cpTimer=0;

dlBtn.addEventListener("click",function(ev){
  ev.stopPropagation();
  var href=dlBtn.getAttribute("href");
  if(!href){ev.preventDefault();return;}
  urlIn.value=href;
  urlRow.className=urlRow.className.indexOf("show")<0?"urlRow show":"urlRow";
  dlBtn.className=urlRow.className.indexOf("show")<0?"dl":"dl open";
  fit();                       /* 高度变了，重算一次，别出滚动条 */
  /* 故意不 preventDefault：宿主若允许弹窗，新标签页照开 */
});

function flash(msg){
  cpBtn.textContent=msg;
  clearTimeout(cpTimer);
  cpTimer=setTimeout(function(){cpBtn.textContent="复制";},1600);
}

cpBtn.addEventListener("click",function(ev){
  ev.stopPropagation();
  urlIn.focus();urlIn.select();
  try{urlIn.setSelectionRange(0,99999);}catch(e){}   /* iOS 要这句才真选中 */
  var ok=false;
  try{ok=document.execCommand("copy");}catch(e){}
  if(ok){flash("已复制");return;}
  /* execCommand 被禁时再试异步 API。它在 iframe 里常被 permissions policy
     拦掉，所以放第二位而不是第一位。 */
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(urlIn.value).then(
      function(){flash("已复制");},function(){flash("请长按复制");});
    return;
  }
  flash("请长按复制");
});

/* ── 取音频：三档降级 ─────────────────────────────────────────
   ① fetch → ArrayBuffer → Web Audio 解码播放   ← 首选，不碰 media-src
   ② fetch → blob: URL → <audio src>            ← Web Audio 不可用/解码失败
   ③ 直接把远程 URL 赋给 <audio src>            ← 连 fetch 都被拦时的最后一搏
   data: URI（base64 内嵌）走 ② 就够，不必 fetch。                        */
function loadAudio(url,autoplay){
  stopSource();playing=false;offset=0;audioBuffer=null;audioBytes=null;
  mode="webaudio";btn.className="play";paint();

  if(/^data:/i.test(url)){useElement(url,autoplay);return;}

  fetch(url).then(function(r){
    if(!r.ok)throw new Error("http "+r.status);
    return r.arrayBuffer();
  }).then(function(buf){
    audioBytes=buf;
    return ensureDecoded().then(function(){
      if(autoplay)startPlayback().catch(function(){});
    }).catch(function(e){
      /* 解码失败（编码不被支持 / 没有 Web Audio）→ 降级 ② */
      console.warn("[voice-card] decode failed, fallback to <audio>:",e);
      var blobUrl=URL.createObjectURL(new Blob([buf],{type:"audio/mpeg"}));
      useElement(blobUrl,autoplay);
    });
  }).catch(function(e){
    /* fetch 被 CSP/CORS 拦 → 降级 ③ 直连 */
    console.warn("[voice-card] fetch failed, fallback to direct src:",e);
    useElement(url,autoplay);
  });
}

function useElement(src,autoplay){
  mode="element";
  if(au.dataset.blobUrl){URL.revokeObjectURL(au.dataset.blobUrl);au.dataset.blobUrl="";}
  if(/^blob:/i.test(src))au.dataset.blobUrl=src;
  au.src=src;
  au.onerror=function(){lab.textContent="语音加载失败";};
  if(autoplay){var pr=au.play();if(pr&&pr.catch)pr.catch(function(){});}
  paint();
}

/* ── 渲染：把工具返回的 structuredContent 铺到界面上 ── */
function render(d){
  if(!d)return;
  if(d.label)lab.textContent=d.label;
  if(d.audio&&/^https?:/i.test(d.audio)){dlBtn.href=d.audio;dlBtn.style.display="";}
  else if(d.audio)dlBtn.style.display="none";     /* data: URI 没法当下载链接 */
  if(d.audio&&d.audio!==urlIn.value){             /* 换了新语音就收起旧链接 */
    urlRow.className="urlRow";dlBtn.className="dl";
  }
  if(d.audio&&d.audio!==currentUrl){
    currentUrl=d.audio;
    loadAudio(d.audio,!!d.autoplay);
  }
  if(d.error)lab.textContent="✗ "+d.error;
  fit();
}

/* 自适应：flex 条自己会伸缩，再按视口宽度整体缩放兜底，并把 body 高度
   贴着卡片高度设死——宿主按内容高度给 iframe 留位置，永不出滚动条。 */
function fit(){
  var c=document.getElementById("bar");
  c.style.transform="none";
  var vw=document.documentElement.clientWidth||320;
  var W=c.offsetWidth||320;
  var s=Math.min(1.15,Math.max(0.5,(vw-6)/W));
  c.style.transformOrigin="top center";
  c.style.transform="scale("+s+")";
  var h=Math.ceil(c.offsetHeight*s+8)+"px";
  document.body.style.height=h;
  document.documentElement.style.height=h;
}
window.addEventListener("resize",fit);

/* ── 跟宿主的 postMessage 契约 ──
   收：ui/notifications/tool-result 里的 params.structuredContent
       （有的宿主直接发 {structuredContent:...}，两种都接）
   发：ui/initialize（带 id，算一次请求）+ ui/notifications/initialized  */
window.addEventListener("message",function(ev){
  var msg=ev.data;if(!msg||typeof msg!=="object")return;
  if(msg.jsonrpc==="2.0"&&msg.method==="ui/notifications/tool-result")
    render(msg.params&&msg.params.structuredContent);
  if(msg.structuredContent)render(msg.structuredContent);
});
function send(method,params,id){
  var m={jsonrpc:"2.0",method:method,params:params||{}};
  if(id!==undefined)m.id=id;
  window.parent.postMessage(m,"*");
}
paint();fit();
window.addEventListener("load",fit);
setTimeout(fit,120);
send("ui/initialize",{name:"voice-card",version:"1.0.0"},1);
setTimeout(function(){send("ui/notifications/initialized",{});},50);
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════
# 四、MCP 服务器
# ═══════════════════════════════════════════════════════════════════════

# enable_dns_rebinding_protection=False：那层防护是给本地 localhost 场景的，
# 公网部署时它只会让所有外部 Host 吃 421 Invalid Host。真正的门是下面的 token。
mcp = FastMCP("voice-card", stateless_http=STATELESS,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=False))

# ── 补丁一：同步工具丢线程池 ──
# FastMCP 对同步函数是直接在事件循环主线程上跑。TTS 要等几秒网络 IO，
# 这几秒整台服务器对所有请求装聋——连 tools/list 都超时，
# 表现成"工具明明在，却调不动"。
_tool_orig = mcp.tool


def _tool_nonblocking(*d_args, **d_kwargs):
    deco = _tool_orig(*d_args, **d_kwargs)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            return deco(fn)

        @functools.wraps(fn)                # 保住签名，inputSchema 才不受影响
        async def async_fn(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        return deco(async_fn)

    return wrap


mcp.tool = _tool_nonblocking

# ── 补丁二：声明 UI 扩展能力 ──
# 宿主要在 initialize 的 capabilities.experimental 里看到这个 key，
# 才认这台服务器能给 HTML 卡片。SDK 还没有正式 API，只能打补丁。
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


# ── 卡片的 CSP 白名单 ──
# 卡片跑在宿主的沙箱 iframe 里，默认 CSP 会拦掉它对外的 fetch。
# 这里显式声明"我要连这个域名"，宿主才会放行。两套字段都写：
#   ui.csp.*           → MCP UI / claude.ai
#   openai/widgetCSP.* → OpenAI Apps SDK（下划线命名，别写错）
#
# 🚨 千万不要加 "domain" / "openai/widgetDomain"。那会让宿主拿它当卡片自己的
#    沙箱源，一旦跟实际不符，所有卡片直接 "Failed to load the MCP app"。
WIDGET_META = {
    "ui": {"csp": {"resourceDomains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
                   "connectDomains": [ASSET_ORIGIN] if ASSET_ORIGIN else []}},
    "openai/widgetCSP": {"resource_domains": [ASSET_ORIGIN] if ASSET_ORIGIN else [],
                         "connect_domains": [ASSET_ORIGIN] if ASSET_ORIGIN else []},
}


# mime_type 里的 ;profile=mcp-app 是关键，宿主靠它认出"这是个卡片"。
# 💡 改了 HTML 宿主还在渲染旧版？连接器按 URI 缓存卡片壳子。办法是升一个
#    版本号 URI，**同时保留旧 URI 做别名**返回同一份 HTML。
@mcp.resource("ui://voice-card-v1", mime_type="text/html;profile=mcp-app", meta=WIDGET_META)
def voice_card_ui() -> str:
    return VOICE_CARD_HTML


@mcp.resource("ui://voice-card", mime_type="text/html;profile=mcp-app", meta=WIDGET_META)
def voice_card_ui_legacy() -> str:
    return VOICE_CARD_HTML


# structured_output=True 必须有：返回的 dict 才会进 structuredContent，
# 卡片就是从那里拿数据的。
@mcp.tool(meta={"ui/resourceUri": "ui://voice-card-v1",
                "ui": {"resourceUri": "ui://voice-card-v1"}},
          structured_output=True)
def voice_card(text: str, voice_id: str = "", label: str = "",
               provider: str = "", autoplay: bool = False) -> dict[str, str]:
    """把一句话合成语音，渲染成可播放的语音条卡片。

    text: 要念的文字
    voice_id: 指定音色，留空用环境变量里的默认音色
    label: 卡片标题，留空自动取文字开头
    provider: elevenlabs / volcano，留空用 TTS_PROVIDER
    autoplay: True 则卡片加载完自动播一次
    """
    r = synthesize(text, voice_id, provider)
    if not r.startswith("OK: "):
        return {"error": r[:160], "label": label or "合成失败"}
    # 🚨 只回 URL，绝不把 base64 音频塞进返回值：5 万+ 字符的响应体会把
    #    宿主的卡片加载器直接搞崩（"Failed to load the MCP app"）。
    #    音频由卡片自己 fetch，响应体永远保持轻量。
    return {"audio": r[4:].strip(),
            "label": label or (text[:18] + ("…" if len(text) > 18 else "")),
            "autoplay": "1" if autoplay else ""}


@mcp.tool()
def voice_url(text: str, voice_id: str = "", provider: str = "") -> str:
    """只合成语音、返回 mp3 链接，不渲染卡片（给不支持 HTML 卡片的客户端）。"""
    return synthesize(text, voice_id, provider)


# ── 鉴权：最朴素的 Bearer token 门 ──
class AuthMiddleware:
    """/audio 和 /healthz 免鉴权（音频必须让卡片匿名 fetch，否则播不出来），
    其余一律要 Authorization: Bearer <MCP_AUTH_TOKEN>。
    token 留空 = 完全裸奔，只适合本机调试。"""

    OPEN_PREFIXES = ("/audio", "/healthz")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not AUTH_TOKEN:
            return await self.app(scope, receive, send)
        if scope.get("path", "").startswith(self.OPEN_PREFIXES):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if headers.get("authorization", "") == f"Bearer {AUTH_TOKEN}":
            return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


async def healthz(request: Request):
    return JSONResponse({"ok": True, "provider": TTS_PROVIDER, "asset_origin": ASSET_ORIGIN})


def main():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    if not AUTH_TOKEN:
        print("⚠️  MCP_AUTH_TOKEN 未设置：谁拿到地址都能调你的工具、烧你的 TTS 额度。", flush=True)
    if not ASSET_ORIGIN:
        print("⚠️  AUDIO_BASE_URL 解析不出域名，CSP 白名单是空的，卡片取不到音频。", flush=True)
    elif ASSET_ORIGIN.startswith("http://") and "127.0.0.1" not in ASSET_ORIGIN \
            and "localhost" not in ASSET_ORIGIN:
        print("⚠️  AUDIO_BASE_URL 是 http://：宿主页面是 https，混合内容会被浏览器拦。", flush=True)

    app = mcp.streamable_http_app()
    # 插到路由表最前面：streamable app 自带顶层 lifespan（会话管理器住在里面），
    # 重新 Mount 进一个新的 Starlette 会把那个 lifespan 丢掉，服务直接起不来。
    app.router.routes.insert(0, Route("/healthz", endpoint=healthz))
    app.router.routes.insert(0, Mount("/audio", app=StaticFiles(directory=AUDIO_DIR),
                                      name="audio"))

    print(f"🔊 voice-card-mcp → http://{HOST}:{PORT}/mcp", flush=True)
    print(f"   TTS provider = {TTS_PROVIDER}", flush=True)
    print(f"   audio        = {AUDIO_DIR}  →  {AUDIO_BASE_URL}", flush=True)
    uvicorn.run(AuthMiddleware(app), host=HOST, port=PORT)


if __name__ == "__main__":
    sys.exit(main())

# ═══════════════════════════════════════════════════════════════════════
# CREDITS
#   · 语音条形态（微信风格播放条 + ext-apps 内嵌卡片）与「卡片必须显式声明
#     CSP resourceDomains / connectDomains」这个关键发现，参考自
#     garan0613/voice-mcp（MIT）https://github.com/garan0613/voice-mcp
#     ——那是一份 TypeScript / Cloudflare Workers / MiniMax 的实现；
#     本文件是独立重写的 Python / FastMCP / ElevenLabs·火山 版本。
#   · MCP 协议与 Python SDK：modelcontextprotocol（MIT）
#   · widgetCSP 字段命名沿用 OpenAI Apps SDK 的约定，以便一份卡片两边都能用。
# ═══════════════════════════════════════════════════════════════════════
