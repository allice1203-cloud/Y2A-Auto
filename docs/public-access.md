# 当前本地运行方式

视频搬运通道已经迁移到当前 M1 Max MacBook，只提供本机回环访问：

- 视频工作台：`http://127.0.0.1:15188`
- 超级印钞机：`http://127.0.0.1:8080/app/`
- OpenList：`http://127.0.0.1:5245`
- 运行方式：macOS LaunchAgent，原生 arm64
- 公网域名：无

当前边界：

1. 不向香港或日本 VPS 同步代码、Cookie、数据库或视频文件；
2. 不恢复旧 Cloudflare Tunnel、Docker 视频容器或远程 Cookie 同步；
3. MacBook 合盖、断电或断网时，本地服务会暂停；
4. 账号凭据继续保存在各应用自己的私有目录，不写入 Git；
5. 115 OAuth 只由本机 OpenList 保存，视频系统只调用回环接口。

## 日常入口

登录视频工作台后优先打开“快速配置”：

1. 选择个人稳妥、热点快速或多平台增长；
2. 运行一键体检；
3. 只处理页面列出的必需项；
4. 在任务中心粘贴一条或多条链接；
5. 成片经人工确认后发布，并自动备份到 115。

已连接的 Telegram Bot 同时是快速收件箱：可直接发送单条或多条公开视频链接，用
`#direct` / `#quick` / `#professional` 和平台标签覆盖本次默认值。该入口只准备任务，
不触发自动发布，也不接收密码、Token、Cookie 或设置命令。

## 本地服务验收

不能只看进程或端口。有效验收应同时满足：

- `com.sg99.video-transfer-channel.local` 为 running，登录页 HTTP 200；
- `com.sg99.moneyprinter-video-worker.local` 为 running，受保护 projects API HTTP 200；
- 一键体检中的二剪制作端和 115 备份均显示可用；
- 视频数据库完整性检查通过；
- 可用磁盘高于系统保护线。
- Telegram 快速入口为 running，且制作端 watchdog 正在定时检查。

外网请求会在启动时跟随 macOS “网络→代理”中的当前回环端口，不再写死某个代理应用端口。

## 恢复原则

设置保存前会创建不含密码、Token、Cookie 和 API Key 的本地快照；快速配置页可撤销非敏感设置。数据库、账号凭据和 OpenList 数据应进入单独的加密备份与恢复演练，不能依赖同一个 115 账号作为唯一备份。
