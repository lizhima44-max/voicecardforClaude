#!/usr/bin/env python3
"""TTS 引擎层：把一段文字变成一个 mp3 文件，返回可公网访问的 URL。

支持两家，用环境变量 TTS_PROVIDER 切：
    elevenlabs  —— 国际，声音克隆效果好，英文/多语都强
    volcano     —— 火山引擎（豆包）声音复刻，中文自然度好，国内网络友好

所有密钥、音色 ID、路径都来自环境变量，代码里没有任何真实凭据。
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid

import requests

# ── 配置（全部来自环境变量，见 .env.example）────────────────────────────
PROVIDER = os.environ.get("TTS_PROVIDER", "elevenlabs").strip().lower()

# 生成的 mp3 存哪儿、用什么 URL 前缀对外提供
AUDIO_DIR = os.environ.get("AUDIO_DIR", "./public/audio")
# ⚠️ 必须是卡片能 fetch 到的地址，而且必须跟 server.py 里 CSP 白名单的域名一致
AUDIO_BASE_URL = os.environ.get("AUDIO_BASE_URL", "http://127.0.0.1:8000/audio").rstrip("/")

# 生成物保留天数，过期自动清（0 = 不清）
AUDIO_KEEP_DAYS = int(os.environ.get("AUDIO_KEEP_DAYS", "7"))

# ElevenLabs
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# 火山引擎 / 豆包声音复刻（openspeech）
VOLCANO_APP_ID = os.environ.get("VOLCANO_APP_ID", "")
VOLCANO_ACCESS_TOKEN = os.environ.get("VOLCANO_ACCESS_TOKEN", "")
VOLCANO_VOICE_ID = os.environ.get("VOLCANO_VOICE_ID", "")
# 声音复刻用 volcano_icl；用平台预置音色时改成 volcano_tts
VOLCANO_CLUSTER = os.environ.get("VOLCANO_CLUSTER", "volcano_icl")


# ── 存盘 + 清理 ─────────────────────────────────────────────────────────

def _save_mp3(audio_bytes: bytes, seed: str, prefix: str) -> str:
    """写盘并返回公网 URL。文件名 = 前缀 + 内容哈希 + 时间戳，天然不撞。"""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]
    fname = f"{prefix}_{digest}_{int(time.time())}.mp3"
    with open(os.path.join(AUDIO_DIR, fname), "wb") as f:
        f.write(audio_bytes)
    _cleanup()
    return f"{AUDIO_BASE_URL}/{fname}"


def _cleanup() -> None:
    """清掉过期的生成物。每次合成后顺手扫一遍，不用另起定时任务。

    只删自己生成的前缀，别人的文件一律不碰——这条不要省，
    AUDIO_DIR 万一被指到一个共用目录上，没这层判断就是一场事故。
    """
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


def _as_data_uri(audio_bytes: bytes) -> str:
    return "data:audio/mpeg;base64," + base64.b64encode(audio_bytes).decode()


# ── ElevenLabs ─────────────────────────────────────────────────────────

def _tts_elevenlabs(text: str, voice_id: str = "", *, embed: bool = False,
                    speed: float = 1.0, stability: float = 0.4,
                    style: float = 0.4, similarity_boost: float = 0.75,
                    use_speaker_boost: bool = True) -> str:
    if not ELEVENLABS_API_KEY:
        return "ERROR: ELEVENLABS_API_KEY 未设置"
    vid = voice_id or ELEVENLABS_VOICE_ID
    if not vid:
        return "ERROR: ELEVENLABS_VOICE_ID 未设置（去 ElevenLabs 后台复制音色 ID）"
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY,
                     "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": ELEVENLABS_MODEL,
                "voice_settings": {
                    # 都夹一下范围，传越界值 API 会直接 422
                    "stability": max(0.0, min(1.0, float(stability))),
                    "similarity_boost": max(0.0, min(1.0, float(similarity_boost))),
                    "style": max(0.0, min(1.0, float(style))),
                    "use_speaker_boost": bool(use_speaker_boost),
                    "speed": max(0.7, min(1.2, float(speed))),
                },
            },
            timeout=60,
        )
        if r.status_code == 429 or "quota_exceeded" in r.text:
            return "ERROR: ElevenLabs 额度用完了（免费额度按月刷新）"
        if not r.ok:
            return f"FAIL {r.status_code}: {r.text[:200]}"
        if embed:
            return "OK: " + _as_data_uri(r.content)
        return "OK: " + _save_mp3(r.content, text + vid, "el")
    except Exception as e:                      # noqa: BLE001 —— 网络层什么都可能炸
        return f"ERROR: {e}"


# ── 火山引擎 / 豆包声音复刻 ────────────────────────────────────────────

def _tts_volcano(text: str, voice_id: str = "", *, embed: bool = False,
                 speed_ratio: float = 1.0, emotion: str = "",
                 emotion_scale: float = 4.0) -> str:
    if not VOLCANO_APP_ID or not VOLCANO_ACCESS_TOKEN:
        return "ERROR: VOLCANO_APP_ID / VOLCANO_ACCESS_TOKEN 未设置"
    vid = voice_id or VOLCANO_VOICE_ID
    if not vid:
        return "ERROR: VOLCANO_VOICE_ID 未设置（声音复刻完成后平台会给一个 S_xxx 的 ID）"

    audio: dict = {
        "voice_type": vid,
        "encoding": "mp3",
        "rate": 24000,
        "speed_ratio": max(0.2, min(3.0, float(speed_ratio))),
    }
    if emotion:
        # ⚠️ 情感字段只有"多情感音色"和声音复刻 2.0 认；1.0 复刻音色传了会报错。
        # 所以只在显式要求时才加，保证默认路径永远安全。
        audio["emotion"] = emotion
        audio["enable_emotion"] = True
        audio["emotion_scale"] = max(1.0, min(5.0, float(emotion_scale)))

    payload = {
        "app": {
            "appid": VOLCANO_APP_ID,
            "token": VOLCANO_ACCESS_TOKEN,
            "cluster": VOLCANO_CLUSTER,
        },
        "user": {"uid": "voice-card"},
        "audio": audio,
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "text_type": "plain",
            "operation": "query",
        },
    }
    try:
        r = requests.post(
            "https://openspeech.bytedance.com/api/v1/tts",
            # ⚠️ 分号不是笔误：火山这个接口要 "Bearer;<token>"
            headers={"Authorization": f"Bearer;{VOLCANO_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if not r.ok:
            return f"FAIL {r.status_code}: {r.text[:200]}"
        data = r.json()
        if data.get("code") != 3000:            # 3000 才是成功
            return f"TTS ERROR code={data.get('code')}: {data.get('message', '')}"
        audio_bytes = base64.b64decode(data["data"])
        if embed:
            return "OK: " + _as_data_uri(audio_bytes)
        return "OK: " + _save_mp3(audio_bytes, text + vid, "vol")
    except Exception as e:                      # noqa: BLE001
        return f"ERROR: {e}"


# ── 统一入口 ───────────────────────────────────────────────────────────

def synthesize(text: str, voice_id: str = "", *, provider: str = "",
               embed: bool = False, **kwargs) -> str:
    """合成语音。

    返回值是一行字符串，成功时形如 ``OK: https://.../el_ab12_1700000000.mp3``
    （embed=True 时是 ``OK: data:audio/mpeg;base64,...``），
    失败时以 ERROR / FAIL 开头。
    调用方只需判断 ``startswith("OK: ")``，省掉一层异常处理。
    """
    text = (text or "").strip()
    if not text:
        return "ERROR: text 不能为空"
    if len(text) > 2000:
        return "ERROR: text 太长了（>2000 字），分段合成吧"

    p = (provider or PROVIDER).lower()
    if p in ("volcano", "doubao", "bytedance"):
        return _tts_volcano(text, voice_id, embed=embed, **kwargs)
    return _tts_elevenlabs(text, voice_id, embed=embed, **kwargs)
