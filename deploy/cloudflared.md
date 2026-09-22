# 用 Cloudflare Tunnel 暴露到公网（不用服务器、不用域名）

## 临时隧道（测试用，地址每次都变）

```bash
# 安装
brew install cloudflared                                   # macOS
# Linux:
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -O cloudflared && chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/

# 服务已经在 8000 跑着，另开一个终端：
cloudflared tunnel --url http://localhost:8000
```

它会打印类似 `https://random-words-here.trycloudflare.com`。

**然后一定要回去改配置并重启服务：**

```bash
# .env 里
AUDIO_BASE_URL=https://random-words-here.trycloudflare.com/audio
```

```bash
curl https://random-words-here.trycloudflare.com/healthz
# asset_origin 必须是这个 https 地址，否则卡片一定播不出声
```

MCP 端点就是 `https://random-words-here.trycloudflare.com/mcp`。

---

## 命名隧道（长期用，地址固定）

```bash
cloudflared tunnel login                        # 浏览器里授权
cloudflared tunnel create voice-card            # 记下返回的 tunnel id
cloudflared tunnel route dns voice-card voice.你的域名.com
```

`~/.cloudflared/config.yml`：

```yaml
tunnel: <tunnel-id>
credentials-file: /root/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: voice.你的域名.com
    service: http://localhost:8000
  - service: http_status:404
```

```bash
cloudflared tunnel run voice-card
# 或装成服务：
sudo cloudflared service install
```

`AUDIO_BASE_URL` 设成 `https://voice.你的域名.com/audio`，重启服务。
