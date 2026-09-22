# 从零做一个「会说话的语音条」MCP 卡片

> 让 AI 助手回你一条**能点开播放的语音消息**——就是微信里那种条，
> 带播放按钮、波形、时长、下载。
>
> 这份教程假设你**完全没做过 MCP**，每一步都有可以直接复制的命令。
> 两个版本任选：
> - **单文件版**：一个 `.py` 跑起来就完事，适合先跑通看效果
> - **GitHub 版**：拆好的目录结构，适合长期维护、开源发布

---

## 目录

1. [先看明白它是怎么转的](#1-先看明白它是怎么转的)
2. [准备三样东西](#2-准备三样东西)
3. [路线 A：单文件版（15 分钟）](#3-路线-a单文件版15-分钟)
4. [路线 B：GitHub 版](#4-路线-bgithub-版)
5. [让它能被公网访问（关键一步）](#5-让它能被公网访问关键一步)
6. [接到 claude.ai / ChatGPT](#6-接到-claudeai--chatgpt)
7. [验收清单](#7-验收清单)
8. [报错对照表（踩过的坑都在这）](#8-报错对照表踩过的坑都在这)
9. [安全提醒](#9-安全提醒)
10. [想改成自己的样子](#10-想改成自己的样子)
11. [借鉴了谁](#11-借鉴了谁)
12. [传到你自己的 GitHub](#12-传到你自己的-github)

---

## 1. 先看明白它是怎么转的

一句话：**AI 调你的工具 → 你的服务器合成 mp3 → 返回一个链接 → AI 客户端把你的 HTML 卡片塞进一个小窗口 → 卡片自己去下载那段音频播出来。**

```
  你对 AI 说："用语音跟我说晚安"
        │
        ▼
  ┌─────────────────────┐   ① 调用工具 voice_card(text="晚安")
  │  claude.ai / ChatGPT │ ──────────────────────────────┐
  └─────────────────────┘                                │
        ▲                                                ▼
        │                                    ┌────────────────────────┐
        │  ③ 返回 {audio: "https://.../x.mp3"}│  你的 MCP 服务器        │
        │     （只回链接，不回音频本身！）      │  ② 调 TTS API 合成 mp3  │
        │                                    │     存盘，给个 URL      │
        │                                    └────────────────────────┘
        ▼                                                ▲
  ┌─────────────────────────────────┐                    │
  │ 客户端读出你的卡片 HTML，         │   ⑤ 卡片 fetch 音频 │
  │ 塞进一个沙箱 iframe，            │ ───────────────────┘
  │ 用 postMessage 把 ③ 的数据发进去  │
  │ ④                               │
  └─────────────────────────────────┘
                 │
                 ▼
        🔊 用户点播放，出声
```

三个关键概念，记住就够了：

| 名词 | 人话 |
|------|------|
| **MCP 服务器** | 一个网络服务，对 AI 暴露一组「工具」。AI 决定什么时候调。 |
| **工具（tool）** | 一个函数。AI 传参数进来，你返回结果。这里就是 `voice_card(text)`。 |
| **卡片（UI resource）** | 一份 HTML。AI 客户端把它渲染在对话里，用 `postMessage` 把工具的返回值递给它。 |

> **⚠️ 全篇最重要的一条**
> 卡片跑在客户端的**沙箱 iframe** 里，受一套严格的内容安全策略（CSP）管。
> 你必须在服务器里**显式声明**「我的卡片要去连某某域名」，客户端才会放行。
> 漏了这一步，卡片能出现，但永远播不出声——而且浏览器控制台里的报错还藏得很深。
> 这也是本教程唯一必须照抄、不能自由发挥的地方。

---

## 2. 准备三样东西

### 2.1 Python 3.10 以上

```bash
python3 --version        # 看到 3.10 / 3.11 / 3.12 都行
```

没有就去 [python.org](https://www.python.org/downloads/) 装，或者 `brew install python`（macOS）、
`sudo apt install python3 python3-pip python3-venv`（Ubuntu/Debian）。

### 2.2 一个 TTS（文字转语音）账号——二选一

| | **ElevenLabs** | **火山引擎 / 豆包声音复刻** |
|---|---|---|
| 适合 | 英文、多语种，克隆效果最像 | 中文自然度好，国内网络友好 |
| 免费额度 | 每月一万字符左右 | 有试用额度 |
| 拿什么 | `API Key` + `Voice ID` | `App ID` + `Access Token` + `音色 ID` |
| 在哪拿 | [elevenlabs.io](https://elevenlabs.io) → 头像 → API Keys；<br>Voices → 点进音色看 ID | [火山引擎控制台](https://console.volcengine.com/speech) → 语音技术 → 声音复刻 |

**想克隆自己（或某个人）的声音**：两家都支持，录 1～10 分钟干净人声上传，
几分钟后给你一个音色 ID，填进配置就行。

> 录音只要**安静、没有背景音乐、语速正常**，手机录都行。
> 别用有混响的房间，克隆出来会闷。

### 2.3 一个能被公网访问的地方

这一步很多人卡住，所以单独展开在 [第 5 节](#5-让它能被公网访问关键一步)。
先知道结论：**你的服务必须有一个 https 的公网地址**，因为
①AI 客户端要连你的 MCP 端点，②卡片要下载你的音频。

只是想在自己电脑上先看效果的话，可以跳过，用 `http://127.0.0.1:8000`，
但那样只能用支持本地连接的客户端测。

---

## 3. 路线 A：单文件版（15 分钟）

### 第 1 步：拿文件

把 [`single-file/voice_card_mcp.py`](single-file/voice_card_mcp.py) 下载到一个空文件夹。
**整个项目就这一个文件**，卡片 HTML 也内嵌在里面。

### 第 2 步：装依赖

```bash
mkdir voice-card && cd voice-card
# 把 voice_card_mcp.py 放进来

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install "mcp>=1.9.0,<2" requests uvicorn
```

> 🚨 `mcp` 那个版本号**必须带 `<2`**。
> MCP Python SDK 2.0 把 `FastMCP` 改名成了 `MCPServer`，
> 直接 `pip install mcp` 会装到 2.x，然后你会看到
> `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`。

### 第 3 步：填配置

```bash
# ── 用 ElevenLabs ──
export TTS_PROVIDER=elevenlabs
export ELEVENLABS_API_KEY=sk_你的key
export ELEVENLABS_VOICE_ID=你的音色id

# ── 或者用火山豆包 ──
# export TTS_PROVIDER=volcano
# export VOLCANO_APP_ID=你的appid
# export VOLCANO_ACCESS_TOKEN=你的token
# export VOLCANO_VOICE_ID=S_xxxxxxxx

# ── 公共 ──
export AUDIO_DIR=./public/audio
export AUDIO_BASE_URL=http://127.0.0.1:8000/audio    # 上公网后改成 https 域名
export MCP_AUTH_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
echo "你的 token: $MCP_AUTH_TOKEN"                    # 记下来，连接器要填
```

Windows PowerShell 把 `export A=B` 换成 `$env:A="B"`。

### 第 4 步：跑

```bash
python voice_card_mcp.py
```

看到这三行就是成了：

```
🔊 voice-card-mcp → http://0.0.0.0:8000/mcp
   TTS provider = elevenlabs
   audio        = ./public/audio  →  http://127.0.0.1:8000/audio
```

### 第 5 步：自测（不用 AI 也能测）

开另一个终端：

```bash
curl http://127.0.0.1:8000/healthz
# {"ok":true,"provider":"elevenlabs","asset_origin":"http://127.0.0.1:8000"}
```

再测一次真的合成（把 token 换成你自己的）：

```bash
pip install mcp                     # 客户端库，同一个包
python3 - <<'PY'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client(
        "http://127.0.0.1:8000/mcp",
        headers={"Authorization": "Bearer 把你的token贴这里"},
    ) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("工具:", [t.name for t in (await s.list_tools()).tools])
            out = await s.call_tool("voice_card", {"text": "喂，听得见吗"})
            print("返回:", out.structuredContent)

asyncio.run(main())
PY
```

看到 `{'audio': 'http://127.0.0.1:8000/audio/el_xxx.mp3', 'label': '喂，听得见吗', ...}`
就说明**合成链路完全通了**。把那个链接丢进浏览器，应该能听到声音。

到这里，服务端的活全干完了。剩下的是让 AI 能连上它。

---

## 4. 路线 B：GitHub 版

同样的功能，拆成正常的项目结构，方便改、方便发布：

```
仓库根/
├── server.py           # MCP 服务器：注册工具和卡片、鉴权、静态托管
├── tts.py              # TTS 引擎层：ElevenLabs / 火山，加一家就在这加
├── voice_card.html     # 卡片本体（改样式改这个）
├── requirements.txt
├── .env.example        # 复制成 .env 填密钥
├── .gitignore          # 已经把 .env 和 public/ 挡住了
├── LICENSE             # MIT，把 <YOUR NAME> 换成你的名字
├── CREDITS.md          # 致谢与借鉴说明
└── deploy/
    ├── systemd.service.example
    ├── nginx.conf.example
    └── cloudflared.md
```

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填密钥
python server.py
```

跟单文件版的唯一区别：配置写在 `.env` 文件里，不用每次 `export`。

**两版功能完全一致**，单文件版就是把这三个文件拼起来的。
先用单文件版跑通，再换 GitHub 版长期维护，是最省事的路径。

---

## 5. 让它能被公网访问（关键一步）

你需要一个 **https 地址**同时指向两样东西：

- `https://你的域名/mcp` → MCP 端点（AI 客户端连这里）
- `https://你的域名/audio/xxx.mp3` → 音频（卡片 fetch 这里）

三种方案，从易到难：

### 方案一：Cloudflare Tunnel（最傻瓜，不用服务器不用域名）

```bash
# macOS
brew install cloudflared
# Linux
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -O cloudflared && chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/

# 服务已经在 8000 跑着的前提下，另开一个终端：
cloudflared tunnel --url http://localhost:8000
```

它会打印一个 `https://随机名字.trycloudflare.com`。**这就是你的公网地址**，自带 https。

然后**改配置重启服务**（这步千万别忘）：

```bash
export AUDIO_BASE_URL=https://随机名字.trycloudflare.com/audio
python voice_card_mcp.py     # 重启
```

> 临时隧道的地址每次重启都会变，只适合测试。
> 长期用就登录 Cloudflare 账号建**命名隧道**，地址固定。

### 方案二：自己的服务器 + Nginx + Let's Encrypt

有 VPS 和域名的话，这是最稳的。见
[`deploy/nginx.conf.example`](deploy/nginx.conf.example)
和 [`systemd.service.example`](deploy/systemd.service.example)。

大致三步：

```bash
sudo certbot --nginx -d voice.你的域名.com     # 申请证书
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/voice-card
# 改里面的域名和端口，然后
sudo ln -s /etc/nginx/sites-available/voice-card /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> 💡 Nginx 方案里，音频文件可以**交给 Nginx 直接托管**（`location /audio` 指向
> `AUDIO_DIR`），比经过 Python 快得多。配置示例里两种写法都给了。

### 方案三：没有域名，用 IP + 免费泛域名证书

`sslip.io` 这类服务能把 `1.2.3.4.sslip.io` 解析到 `1.2.3.4`，
于是可以直接用 certbot 给它签证书，不用买域名。

```bash
sudo certbot certonly --standalone -d 你的IP.sslip.io
```

### 无论哪种方案，改完地址一定要做这件事

```bash
curl https://你的地址/healthz
```

返回里的 `asset_origin` **必须**是你的 https 域名。
如果它还是 `http://127.0.0.1:8000`，说明 `AUDIO_BASE_URL` 没改或者没重启，
卡片百分之百播不出声。

---

## 6. 接到 claude.ai / ChatGPT

### claude.ai

设置 → **连接器** → 添加自定义连接器：

| 字段 | 填什么 |
|------|--------|
| 名称 | 随便，比如 `语音条` |
| URL | `https://你的地址/mcp` |
| 请求头 | `Authorization: Bearer 你的MCP_AUTH_TOKEN` |

保存后连接器应该显示已连接，工具列表里能看到 `voice_card`。

然后在对话里说一句：

> 用语音跟我说句晚安

AI 会自己调 `voice_card`，对话里应该直接出现一条粉色的语音条。

### Claude Code（命令行）

```bash
claude mcp add --transport http 语音条 https://你的地址/mcp \
  --header "Authorization: Bearer 你的token"
```

### 客户端不支持 HTML 卡片怎么办

服务里还注册了一个 `voice_url` 工具，只返回 mp3 链接、不渲染卡片。
不支持卡片的客户端会自动用它，至少还能点链接听。

---

## 7. 验收清单

一条条过，全绿才算真跑通：

- [ ] `curl https://你的地址/healthz` 返回 `ok:true`，且 `asset_origin` 是 https 域名
- [ ] 浏览器直接打开一个 `https://你的地址/audio/xxx.mp3` 能听到声音
- [ ] 客户端的工具列表里能看到 `voice_card`
- [ ] 对话里**出现了卡片**（不是一段 JSON 文字）
- [ ] 卡片上显示了**时长**（比如 `0:00 / 0:03`）——说明音频已经下载并解码成功
- [ ] 点播放**出声**，波形随进度点亮
- [ ] 点波形中间能跳进度
- [ ] 点右上角下载图标，新标签页打开音频
- [ ] 不带 token 访问 `/mcp` 返回 401

---

## 8. 报错对照表（踩过的坑都在这）

> 这张表是最值钱的部分。每一条都是真的撞过才写下来的。

| 现象 | 真正的原因 | 怎么修 |
|------|-----------|--------|
| `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` | 装到了 MCP SDK 2.x，`FastMCP` 已改名 `MCPServer` | `pip install "mcp>=1.9.0,<2"` |
| 对话里只有一段 JSON，**没有卡片** | 客户端没认出这是卡片工具 | 三件事缺一不可：资源 `mime_type` 带 `;profile=mcp-app`、工具 `meta` 里有 `ui/resourceUri`、`initialize` 里声明了 `io.modelcontextprotocol/ui` 能力（代码里的「补丁二」） |
| **`Failed to load the MCP app`** | ①工具返回值太大——把 base64 音频塞进返回值，5 万+ 字符直接撑爆卡片加载器 ②`meta` 里加了 `domain` / `openai/widgetDomain` 字段 | ①返回值只放 URL，音频让卡片自己 fetch ②把 domain 字段删干净，让客户端用默认沙箱域 |
| 卡片出来了，但显示**「语音加载失败」/「语音下载失败」**，时长一直 `0:00` | 卡片的 fetch 被 iframe 的 CSP 拦了 | 检查 `AUDIO_BASE_URL` 的域名是否**一字不差**地出现在资源 `meta` 的 `connectDomains` 里。**子域名不算**：白名单写了 `example.com`，音频放在 `cdn.example.com` 一样被拦 |
| 同上，但控制台报 `Mixed Content` | `AUDIO_BASE_URL` 是 `http://` | 必须 https |
| 同上，控制台报 CORS | 音频服务器没给跨域头 | Nginx 加 `add_header Access-Control-Allow-Origin *;`（Python 自带的静态托管已经没这问题） |
| 时长显示正常，但**点播放没声音** | 少见。多半是音频编码浏览器不认 | 换成标准 mp3（本教程两家 TTS 默认都是） |
| 一直 `Missing session ID` | 客户端不回传 `mcp-session-id` | `export MCP_STATELESS=1` 重启 |
| `421 Invalid Host` | SDK 的 DNS-rebinding 防护把外部域名全拦了 | 代码里已经关掉（`enable_dns_rebinding_protection=False`）。自己改代码时别手贱加回去 |
| 工具**在列表里但调不动**、动不动超时，连 `tools/list` 都卡 | 同步工具直接跑在事件循环主线程上，TTS 等网络那几秒整台服务器对所有请求装聋 | 代码里的「补丁一」——把同步工具丢线程池。这是最难自己想到的一个坑 |
| 改了 `voice_card.html`，客户端还在渲染旧版 | 连接器按 URI 缓存了卡片壳子 | 升一个版本号 URI（`ui://voice-card-v2`），**同时保留旧 URI 做别名**返回同一份 HTML |
| 点下载图标**完全没反应** | 用 JS 造 blob + `a.click()` 触发下载，被沙箱 iframe 拦了（iframe 没有 `allow-downloads`） | 用裸 `<a href download target="_blank">`，最皮实 |
| 卡片里出现滚动条 / 高度被截断 | iframe 按内容高度给位置 | 卡片里的 `fit()` 已经处理：整体缩放 + 把 `body` 高度贴死 |
| 调用返回 401 | token 不对 | 请求头必须是 `Authorization: Bearer <token>`，注意 `Bearer` 后面一个空格 |
| 火山 TTS 报错码非 3000 | ①`Authorization` 少了那个**分号**（要 `Bearer;<token>`，不是 `Bearer <token>`） ②声音复刻 1.0 的音色不支持情感参数 ③集群填错（复刻音色用 `volcano_icl`，预置音色用 `volcano_tts`） | 按左边三条挨个查 |
| ElevenLabs 429 | 免费额度用完 | 等月度刷新，或者换 key |

### 自己排查的正确姿势

卡片出不来声的时候，**别猜**，打开浏览器开发者工具：

1. **Console** 标签——卡片里有 `console.warn`，会明确告诉你是 fetch 失败还是解码失败
2. **Network** 标签——看那条 mp3 请求：红色 `(blocked:csp)` 就是白名单没配对
3. 直接把 mp3 地址粘进地址栏——能播说明服务器没问题，纯粹是卡片被拦

---

## 9. 安全提醒

- **`MCP_AUTH_TOKEN` 一定要设**。不设等于把你的 TTS 额度挂在公网上让人随便烧。
- **密钥永远不要提交到 Git**。`.gitignore` 已经挡了 `.env`，
  但如果你习惯直接改代码里的默认值，就很容易一不小心推上去。
  真推上去了：**立刻去平台把那个 key 作废重发**，
  只删提交没用，历史里还在。
- **音频目录是公开的**。`/audio/` 下的文件谁拿到链接谁能听。
  文件名带了哈希不好猜，但别把敏感内容放这。
  默认 7 天自动清理（`AUDIO_KEEP_DAYS`）。
- **别把 `AUDIO_DIR` 指到一个有别的东西的目录上**。
  清理函数只删自己的前缀（`el_` / `vol_`），但何必冒这个险。

---

## 10. 想改成自己的样子

| 想改什么 | 改哪 |
|---------|------|
| 颜色 | `voice_card.html` 开头 `:root` 那几行。暗色模式在 `@media (prefers-color-scheme:dark)` |
| 波形条数 / 高低 | `var BARS=22` 和 `var HS=[...]` |
| 卡片宽度 | `.bar { width:320px }` |
| 加一家 TTS | `tts.py` 里照着 `_tts_elevenlabs` 写一个，在 `synthesize` 里加个分支 |
| 显示文字稿 | 工具返回值多加一个字段，卡片里 `render()` 接住它渲染 |
| 语速 / 情绪 | `tts.py` 里 `voice_settings`（ElevenLabs）或 `audio`（火山）。<br>⚠️ 火山的情感参数只有复刻 2.0 和多情感音色认 |

**想做真波形**（按音频响度画高低不同的竖条）：
卡片里已经拿到 `audioBuffer` 了，`getChannelData(0)` 分段算 RMS 就行。
这份实现故意用了固定高度的装饰条——小卡片上看不出区别，还省一次遍历。

---

## 11. 借鉴了谁

完整版见 [`CREDITS.md`](CREDITS.md)，这里说清楚最关键的：

### garan0613/voice-mcp（MIT）— 最主要的参考

<https://github.com/garan0613/voice-mcp>

一个用 TypeScript + Cloudflare Workers + MiniMax TTS 写的语音 MCP 服务器。
本项目从它那里借了两样东西：

1. **产品形态**——「TTS 服务器 + 微信风格内嵌语音条」这个组合，
   包括播放按钮 + 波形 + 时长 + 暗色模式的交互设计，是它先做出来的。
2. **一个救命的发现**——卡片必须在资源 `meta` 里**显式声明**
   `resourceDomains` / `connectDomains`，客户端才会在 iframe 的 CSP 里放行。
   这一条当时卡了很久：服务器 CORS 配得好好的，音频用浏览器打开也没问题，
   就是卡片里 fetch 不到。是读了它的源码才定位到的。

**没有复制它的代码。** 本项目是 Python / FastMCP / ElevenLabs·火山 的独立实现，
播放层的做法也不一样（见下）。它是 MIT 许可，本项目同样 MIT。

### 自己趟出来的部分

- **三档降级播放**：Web Audio 解码播放 → `<audio>` + blob URL → 直连 URL。
  因为某些客户端（尤其手机端）的 `media-src` 比 `connect-src` 严得多，
  跨域 mp3 直接赋给 `<audio src>` 会静默失败。
  先 `fetch` 拿字节、再用 Web Audio 播，就完全绕开了 `media-src`。
- **同步工具丢线程池**：见报错表里那条，FastMCP 的同步工具会堵死整个事件循环。
- **卡片 URI 升版 + 旧 URI 留别名**：解连接器缓存。
- **`domain` 字段不能加**：加了全线 `Failed to load the MCP app`。

### 其他上游

- **MCP 协议与 Python SDK** — [modelcontextprotocol](https://github.com/modelcontextprotocol)（MIT）
- **`openai/widgetCSP` 字段命名** — 沿用 OpenAI Apps SDK 的约定，
  这样同一份卡片在两家客户端都能用
- **TTS 服务** — ElevenLabs / 火山引擎，都是商业 API，按各自条款使用

---

## 12. 传到你自己的 GitHub

`` 整个目录就是一个完整的仓库，可以直接推：

```bash
# 1. 复制出来（别把 .env 带上！）
cp -r github-version ~/voice-card-mcp
cd ~/voice-card-mcp

# 2. 改两处署名
#    LICENSE 里的 <YOUR NAME>
#    README.md 里的仓库地址

# 3. 确认没有密钥混进去
grep -rn "sk_\|Bearer \|APP_ID=.\|ACCESS_TOKEN=." . --exclude-dir=.git | grep -v example
#    应该什么都不输出（.env.example 里全是空值）

# 4. 推
git init
git add .
git commit -m "feat: voice card MCP server"
git branch -M main
git remote add origin https://github.com/你的用户名/voice-card-mcp.git
git push -u origin main
```

推之前**务必**做第 3 步。密钥一旦进了 git 历史，删文件是删不掉的。

发布时请保留 `CREDITS.md`——这是对上游作者最基本的尊重，也是 MIT 许可的要求。
MIT 允许你商用、改名、闭源，**唯一的硬性要求是保留版权声明**。

---

**License:** MIT · 教程和代码都可以随便拿去用、改、发布。
