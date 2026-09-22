# voice-card-mcp

给 AI 助手加一条**能点开播放的语音消息**——微信里那种条：播放按钮、波形、时长、下载。

一个 MCP 服务器：AI 调用工具 → 合成语音 → 在对话里渲染成可播放的卡片。

- 🔊 两家 TTS 可选：**ElevenLabs**（多语种/克隆效果好）或 **火山引擎豆包声音复刻**（中文自然）
- 🎨 微信风格语音条，跟随系统的亮色/暗色主题
- 🛡️ 三档降级播放，绕开各家客户端千奇百怪的 CSP 限制
- 🔑 Bearer token 鉴权，生成物定期自动清理
- 📦 四个文件，没有前端构建，没有数据库

> **第一次做这个？** 去看
> [完整的傻瓜教程](TUTORIAL.md)，从装 Python 讲起，带报错对照表。

---

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，至少填：TTS 密钥 + AUDIO_BASE_URL + MCP_AUTH_TOKEN

python server.py
```

看到这几行就是起来了：

```
🔊 voice-card-mcp(streamable) → http://0.0.0.0:8000/mcp
   TTS provider = elevenlabs
   audio        = ./public/audio  →  https://your-domain.example.com/audio
```

自测：

```bash
curl http://127.0.0.1:8000/healthz
```

接到 claude.ai：设置 → 连接器 → 添加自定义连接器，
URL 填 `https://你的域名/mcp`，请求头 `Authorization: Bearer <你的token>`。

---

## 配置

全部走环境变量（`.env` 会被自动读取），完整清单见 `.env.example`。

| 变量 | 说明 |
|------|------|
| `TTS_PROVIDER` | `elevenlabs` 或 `volcano` |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | ElevenLabs 密钥与音色 |
| `VOLCANO_APP_ID` / `VOLCANO_ACCESS_TOKEN` / `VOLCANO_VOICE_ID` | 火山引擎三件套 |
| `AUDIO_DIR` | 生成的 mp3 存哪（默认 `./public/audio`） |
| `AUDIO_BASE_URL` | 🚨 音频的**公网 https 地址**，域名会自动进卡片 CSP 白名单 |
| `AUDIO_KEEP_DAYS` | 生成物保留天数，默认 7，`0` = 不清 |
| `MCP_AUTH_TOKEN` | 🚨 访问令牌，留空 = 裸奔 |
| `MCP_MODE` | `streamable`（默认）或 `sse` |
| `MCP_STATELESS` | 客户端不回传 session id 时设 `1` |

### `AUDIO_BASE_URL` 是最容易配错的一项

它必须同时满足：

1. **公网可达**——卡片跑在别人的网页里，`localhost` 它够不着
2. **https**——宿主页面是 https，http 资源算混合内容，浏览器直接拦
3. **域名一字不差**——这个域名会被自动写进卡片的 CSP 白名单。
   白名单写了 `example.com`，音频放在 `cdn.example.com`，照样被拦

配完用 `curl https://你的域名/healthz` 确认返回的 `asset_origin` 是对的。

---

## 提供的工具

| 工具 | 作用 |
|------|------|
| `voice_card(text, voice_id, label, provider, autoplay)` | 合成语音并渲染成卡片 |
| `voice_url(text, voice_id, provider)` | 只返回 mp3 链接（给不支持卡片的客户端） |

---

## 文件结构

```
server.py         MCP 服务器：注册工具和卡片、鉴权、静态托管
tts.py            TTS 引擎层：加一家就在这里加一个函数
voice_card.html   卡片本体：改样式改这个
deploy/           systemd / nginx / cloudflared 部署示例
```

---

## 部署

- **最快**：`cloudflared tunnel --url http://localhost:8000`，见 [`deploy/cloudflared.md`](deploy/cloudflared.md)
- **最稳**：systemd + Nginx + Let's Encrypt，见
  [`deploy/systemd.service.example`](deploy/systemd.service.example) 和
  [`deploy/nginx.conf.example`](deploy/nginx.conf.example)

---

## 常见问题

| 现象 | 修法 |
|------|------|
| `No module named 'mcp.server.fastmcp'` | 装到 SDK 2.x 了，`pip install "mcp>=1.9.0,<2"` |
| 对话里只有 JSON，没有卡片 | 客户端不支持 HTML 卡片，或 UI 能力声明失败（看启动日志的警告） |
| `Failed to load the MCP app` | 返回值太大，或 `meta` 里混进了 `domain` 字段 |
| 卡片显示「语音下载失败」，时长一直 0:00 | `AUDIO_BASE_URL` 的域名没进 CSP 白名单，或不是 https |
| 一直 `Missing session ID` | `MCP_STATELESS=1` |
| 工具在列表里但调不动、超时 | 确认 `_tool_nonblocking` 那段补丁没被删掉 |
| 改了 HTML 不生效 | 连接器缓存了卡片 URI，升版并保留旧 URI 别名 |

完整的表（十几条，每条都注明真正的原因）在
[教程第 8 节](TUTORIAL.md#8-报错对照表踩过的坑都在这)。

---

## 安全

- `MCP_AUTH_TOKEN` 一定要设，否则谁拿到地址都能烧你的 TTS 额度
- `.env` 已经在 `.gitignore` 里；推之前再 `grep` 一遍别把密钥带上去
- `/audio/` 下的文件是公开的，谁拿到链接谁能听
- 密钥万一推上去了：**立刻去平台作废重发**，删提交没用，历史里还在

---

## 致谢

语音条这个形态、以及"卡片必须显式声明 CSP 白名单"这个关键发现，
参考自 [garan0613/voice-mcp](https://github.com/garan0613/voice-mcp)（MIT）。
逐条说明见 [CREDITS.md](CREDITS.md)。

## License

MIT —— 署名写在 `LICENSE` 第 3 行，想换成真名直接改那一行。
