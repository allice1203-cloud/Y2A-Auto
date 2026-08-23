# 公网访问运维说明

## 当前生产入口

- 产品名称：视频搬运通道
- 公网地址：`https://transfer.sg99.online`
- 本地源站：`http://127.0.0.1:15188`
- 传输方式：香港 VPS 上的独立 Cloudflare Tunnel
- 安全边界：源站只监听回环地址，VPS 安全组不开放应用端口

公网入口必须同时满足：

1. HTTPS 由 Cloudflare 提供；
2. 应用的 `password_protection_enabled` 已启用；
3. `config/config.json`、Cookie、OAuth Token 和隧道凭据权限为 `600`；
4. 密码、Cookie、OAuth Token、Tunnel ID 凭据不进入 Git。

## 开机恢复

视频搬运通道的公网入口、日区出口和模型桥接由香港 VPS 的 PM2 进程托管：

```text
video-transfer-tunnel
video-jp-egress
video-jp-docker-bridge
video-jp-http-docker-bridge
video-hermes-codex
```

PM2 由 `pm2-ubuntu.service` 随系统启动，Tunnel 异常退出时由 PM2 自动恢复。
Docker 容器由 Docker 服务自动恢复应用。

容器的通用外网流量通过 `video-jp-http-docker-bridge` 转到日本 VPS；
`video-hermes-codex` 仅在 Docker 内网地址 `172.26.0.1:18317` 提供
OpenAI 兼容接口，复用香港 VPS 上 Hermes 的 Codex 登录，当前模型为
`gpt-5.6-luna`。两个内网端口都由防火墙限制为仅允许视频搬运容器所在网段访问。

## 无敏感信息健康检查

```bash
curl -I https://transfer.sg99.online/transfer-center
```

未登录时预期返回 `302`，并跳转到 `/login`。登录后搬运中心应返回 `200`。

本机检查：

```bash
docker inspect -f '{{.State.Status}}' video-transfer-channel
pm2 describe video-transfer-tunnel
pm2 describe video-jp-egress
pm2 describe video-jp-docker-bridge
pm2 describe video-jp-http-docker-bridge
pm2 describe video-hermes-codex
cloudflared tunnel info video-transfer-channel
```

## 故障定位顺序

1. 确认 Docker 容器为 `running`；
2. 确认 `http://127.0.0.1:15188/login` 本机可访问；
3. 确认 PM2 中 `video-transfer-tunnel` 为 `online`；
4. 确认 Cloudflare Tunnel 至少有一个已连接 Connector；
5. AI 故障时确认 `video-hermes-codex` 和 `video-jp-http-docker-bridge` 为 `online`；
6. 最后通过对应 PM2 进程日志定位故障，日志中不得记录 API Key 或 Cookie。

## 回滚

需要临时关闭公网入口时，只停止 Tunnel PM2 进程，不停止本地应用：

```bash
pm2 stop video-transfer-tunnel
```

恢复时重新加载：

```bash
pm2 start video-transfer-tunnel
```

删除 DNS 或 Tunnel 属于外部状态变更，应在确认不再使用后执行。
