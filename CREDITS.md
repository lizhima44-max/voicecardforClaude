# 借鉴与致谢

这份文件逐条说明：**哪些东西是从别人那里学来的，学的是哪一部分，
有没有复制代码，以及许可证怎么处理。**

写它不是走形式。做这类"客户端内嵌卡片"的东西，规范文档很薄，
大量关键细节只存在于别人的源码里。谁帮你省了几天，就该写清楚。

---

## 一、garan0613/voice-mcp —— 最主要的参考

- 仓库：<https://github.com/garan0613/voice-mcp>
- 许可：**MIT**
- 技术栈：TypeScript / Cloudflare Workers / MiniMax TTS
- 它是什么：一个 MCP 服务器，给 AI 助手加上克隆声音，
  并用 ext-apps 在对话里内嵌一个微信风格的播放条（带波形、文字稿开关、暗色模式）

### 借了什么

**1. 产品形态**

"TTS 服务器 + 对话内嵌语音条"这个组合，以及播放按钮 + 一排波形竖条 +
时长 + 跟随系统的暗色模式这套交互，是它先做出来的。本项目的卡片在视觉上
是同类设计（配色、圆角、尺寸都是自己重新调的，但形态上确实同源）。

**2. 一个救命的发现：卡片必须显式声明 CSP 白名单**

这是真正帮到大忙的一条。

当时的症状：卡片能加载、能显示，但里面的 `fetch()` 永远拿不到音频。
排查过服务器 CORS（配的是对的）、直接用浏览器打开音频地址（能播）、
换域名、换编码——全都不是。

最后是读它的源码才发现：客户端会把卡片放进一个带严格 CSP 的沙箱 iframe，
服务器必须在资源的 `meta` 里**显式声明**

```
ui.csp.resourceDomains / ui.csp.connectDomains
```

客户端才会把这些域名写进 iframe 的 CSP。不声明 = 默认全拦。
这件事在当时的规范文档里找不到。

对应到本项目：`server.py` 的 `WIDGET_META`（单文件版同名常量）。

### 没借什么

- **没有复制任何代码。** 语言、框架、TTS 厂商、播放实现全都不同。
- 播放层做法不一样：它用 `<audio>`，本项目默认走
  **Web Audio `decodeAudioData` + `BufferSource`**（原因见下面第四节）。
- 部署形态不一样：它是 Cloudflare Workers（serverless），
  本项目是一个自己托管的 Python 进程 + 静态音频目录。

### 许可怎么办

它是 MIT，本项目也是 MIT。因为没有复制代码，严格说没有"必须保留其版权声明"
的义务；但 CREDITS 里这一节请不要删——该有的尊重。

---

## 二、MCP 协议与 Python SDK

- 仓库：<https://github.com/modelcontextprotocol>
- 许可：MIT

`FastMCP`、`SseServerTransport`、`streamable_http_app()` 都来自官方 Python SDK。

两处对 SDK 打了补丁，都在代码注释里写明了原因：

| 补丁 | 为什么 | 位置 |
|------|--------|------|
| 同步工具丢线程池 | SDK 对同步工具是直接在事件循环主线程上执行，一个慢 IO 会让整台服务器对所有请求失去响应 | `server.py` 的 `_tool_nonblocking` |
| 声明 `io.modelcontextprotocol/ui` 能力 | SDK 还没有公开 API 来声明 UI 扩展能力，只能改 `create_initialization_options` | `server.py` 的 `_patched_opts` |

两处都用 `try/except` 全包——补丁失败最多是功能降级，不该让服务起不来。
SDK 将来提供正式 API 了，这两段就可以删掉。

> ⚠️ SDK 2.0 起 `FastMCP` 改名为 `MCPServer`，API 也变了。
> 本项目代码基于 1.x，`requirements.txt` 里钉了 `mcp>=1.9.0,<2`。

---

## 三、OpenAI Apps SDK 的字段约定

`WIDGET_META` 里除了 `ui.csp.*`，还写了一份下划线命名的
`openai/widgetCSP.resource_domains` / `connect_domains`。

这是 OpenAI Apps SDK 的字段命名。两套都写，**一份卡片在两家客户端都能用**。

> 🚨 同时踩到的坑：不要加 `domain` / `openai/widgetDomain` 字段。
> 加上之后所有卡片直接 `Failed to load the MCP app`。
> 不声明，让客户端用默认沙箱域，才是能跑的配置。

---

## 四、自己趟出来的部分

这些没有现成参考，是撞出来的：

### 1. 三档降级播放

```
① fetch → ArrayBuffer → Web Audio 解码播放    ← 首选，完全不碰 media-src
② fetch → blob: URL → <audio src>            ← Web Audio 不可用/解码失败
③ 远程 URL 直接给 <audio src>                 ← 连 fetch 都被拦时的最后一搏
```

起因：某些客户端（尤其手机端）的 `media-src` 比 `connect-src` 严得多。
跨域 mp3 直接赋给 `<audio src>` 会**静默失败**——不报错，就是不响。
而 `fetch` 是走 `connect-src` 的，通常放行。

所以正确姿势是：**先把字节 fetch 回来，再在本地播**，全程不出现"媒体 URL"。

### 2. 返回值里绝不放 base64 音频

曾经为了绕过 CSP，把音频 base64 塞进工具返回值。结果响应体涨到 5 万+ 字符，
直接把客户端的卡片加载器搞崩（`Failed to load the MCP app`）——比播不出声更糟。

结论：**返回值只放 URL，音频让卡片自己 fetch。**

### 3. 卡片 URI 升版 + 旧 URI 留别名

连接器会按 URI 缓存卡片壳子，改了 HTML 也不生效。
升一个版本号 URI，**同时保留旧 URI 返回同一份 HTML**，
已经缓存住旧地址的客户端也能拿到修好的版本。

### 4. 下载按钮必须是裸 `<a>`

用 JS 造 blob + `a.click()` 触发下载，会被沙箱 iframe 拦掉
（iframe 没有 `allow-downloads`），点了完全没反应。
裸 `<a href download target="_blank">` 最皮实。

### 5. 自适应高度

iframe 按内容高度给位置，卡片得自己把 `body` 高度贴死，
再按视口宽度整体缩放兜底，才不会出现滚动条或被截断。

---

## 五、外部服务

| 服务 | 用途 | 说明 |
|------|------|------|
| [ElevenLabs](https://elevenlabs.io) | TTS / 声音克隆 | 商业 API，按其条款使用 |
| [火山引擎语音技术](https://www.volcengine.com/product/voice-tech) | TTS / 声音复刻 | 商业 API，按其条款使用 |
| [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-tunnel/) | 公网暴露（教程里推荐） | 免费 |

这些是 API 调用方，不是代码来源。

---

## 六、本项目自己的许可

**MIT。** 随便拿去用、改、商用、闭源都行，唯一要求是保留版权声明。

如果你 fork 了去发布，请：

1. 把 `LICENSE` 里的 `<YOUR NAME>` 换成你自己的名字
2. **保留这份 CREDITS.md**，尤其是第一节
