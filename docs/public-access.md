# 公网访问运维说明

## 当前生产入口

- 产品名称：视频搬运通道
- 公网地址：`https://transfer.sg99.online`
- 本地源站：`http://127.0.0.1:5188`
- 传输方式：Mac mini 上的独立 Cloudflare Tunnel
- 安全边界：源站只监听回环地址，家庭路由器和 Mac mini 均不开放公网端口

公网入口必须同时满足：

1. HTTPS 由 Cloudflare 提供；
2. 应用的 `password_protection_enabled` 已启用；
3. `config/config.json`、Cookie、OAuth Token 和隧道凭据权限为 `600`；
4. 密码、Cookie、OAuth Token、Tunnel ID 凭据不进入 Git。

## 开机恢复

Cloudflare Tunnel 由当前 macOS 用户的 LaunchAgent 托管：

```text
~/Library/LaunchAgents/com.video-transfer-channel.cloudflared.plist
```

它使用 `RunAtLoad` 和 `KeepAlive`，用户登录后自动启动并在异常退出时自动恢复。
Docker 容器使用 `restart: unless-stopped`，Docker Desktop 启动后自动恢复应用。

## 无敏感信息健康检查

```bash
curl -I https://transfer.sg99.online/transfer-center
```

未登录时预期返回 `302`，并跳转到 `/login`。登录后搬运中心应返回 `200`。

本机检查：

```bash
docker inspect -f '{{.State.Health.Status}}' y2a-auto
launchctl print gui/$(id -u)/com.video-transfer-channel.cloudflared
cloudflared tunnel info video-transfer-channel
```

## 故障定位顺序

1. 确认 Docker 容器为 `healthy`；
2. 确认 `http://127.0.0.1:5188/login` 本机可访问；
3. 确认 LaunchAgent 为 `running`；
4. 确认 Cloudflare Tunnel 至少有一个已连接 Connector；
5. 最后检查 `logs/cloudflared.log` 与 `logs/cloudflared-error.log`。

## 回滚

需要临时关闭公网入口时，只停止 Tunnel LaunchAgent，不停止本地应用：

```bash
launchctl bootout gui/$(id -u) \
  ~/Library/LaunchAgents/com.video-transfer-channel.cloudflared.plist
```

恢复时重新加载：

```bash
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.video-transfer-channel.cloudflared.plist
```

删除 DNS 或 Tunnel 属于外部状态变更，应在确认不再使用后执行。
