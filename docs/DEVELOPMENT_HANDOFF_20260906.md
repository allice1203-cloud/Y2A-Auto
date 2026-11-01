# 视频搬运通道：2026-09-06 开发交接

更新时间：2026-09-06 20:22 +08；Context-Version：2026-09-04.1。

用户因 Codex 额度将尽转 WorkBuddy，要求总结当前进展、开发框架与技术路线并写入知识底座。此次只整理文件及只读复核，不继续浏览器补测，不改账号/任务/自动化配置，不制作、不投稿、不向 115 上传，不操作 VPS。

## 接管入口与权威顺序

完整架构知识正文：[Obsidian 开发架构与 WorkBuddy 接管](</Users/allice/Library/Mobile Documents/iCloud~md~obsidian/Documents/杰哥的知识库/90-系统方案/视频搬运通道-开发架构与WorkBuddy接管-2026-09-06.md>)。

共享入口：`/Users/allice/Documents/系统开发/agent-hub/plans/video-transfer-workbuddy-handoff-20260906.md`；现场状态：同控制面 `status/video-transfer-current-20260906.json`。知识正文只有 Obsidian 一份；本文件保存项目具体进度和接续点。

优先当前用户范围、实时只读证据、专项状态，再看历史说明。旧 `docs/design-system.md` 浅色规范、预览文档“尚未启用”、旧生产版本号和早期 Mac/Win Mini/VPS 部署均不能覆盖最新启用记录。

## 正式代码与运行

- 仓库：`/Users/allice/Documents/系统开发/视频搬运通道`。
- 分支：`codex/video-transfer-channel-brand`；写文档前 HEAD `46458f29481d702ffc1494fee3132e48dde15f8b`。文档提交后以 Git HEAD 为准。
- 当前产品代码最后改动 `077fccc`；设计基线 `c6c3de9`；缺图修复 `08c75f5`。
- 本机 M1 Max 64 GiB，15188 视频服务 PID 87998；8080 制作端 PID 83451，均为 loopback。不是新福云或旧香港。
- 当前正式进程直接运行本仓库，无模板热更新保证；本轮不重启。
- 未跟踪 `modules/production_automation 2.py` 属用户，原样保留，不擅自合并或清除。
- 本轮不推 GitHub。远端存在旧进度不代表本地最新改动已同步。

## 框架速查

Flask/Jinja2 + Bootstrap/原生 JS，Python 模块化单体、SQLite、线程/APScheduler，launchd/Waitress 本机运行；独立 MPT API 负责配音与渲染，FFmpeg/VideoToolbox 负责媒体。新自动工坊、旧 YouTube 同步、人工发布与通知是分开的流程，不新增第二套定时执行器。

关键实现：
- `app.py` 路由/鉴权/启动；`modules/studio_overview.py` 只读合并新旧任务。
- `modules/transfer_center.py` 来源/任务/下载/制作端/发布/备份协调。
- `production_automation.py` 持久选题制作队列；`content_recreation.py` 脚本分镜与语言版本。
- `publication_ledger.py` 防重复与 unknown 恢复；`monetization.py` 实际账期收益。
- `media_workspace.py` 外盘 UUID/空间护栏；`final_media_qc.py` 音画检查。
- `openlist_backup.py` 本机 OpenList→115 上传及大小读回核验。
- `mlx_whisper_worker.py` 本机 ASR；`local_visual_analysis.py` 本机抽帧视觉。
- `static/css/ink-studio.css`、`static/js/ink-studio.js`、`templates/base.html` 与 `partials/studio_review_focus.html` 是当前墨色方向。

## 已完成与验收边界

已启用墨色界面，完成/待审汇总分离、历史缺失封面明确提示、缺媒体不抹掉发布历史，保留现有功能和人工确认。最近代码全量历史回归为 782 passed、21 subtests passed；页面/预览专项 51 passed，Python/JS/diff 检查通过，本次文档整理未重跑全量。

此前真实 Chrome 已验首页、搜索、弹窗取消、设置/手机导航；实际待审视频 465.81551 秒且播放推进，B站版本选择正常。最后缺媒体修复加载后补录遇 Chrome 控制通信超时，因此 `design-qa.md` 最终仍 blocked。晚间用户给出首页已登录截图，不替代剩余详情验收；不再询问原有登录授权。

三项接续验收：
1. `/transfer-center/jobs/7837145a-efee-4d8e-be71-7774c8a64b7f/review`：保留已完成发布，显示本地媒体不可用，无死播放器/重复发布诱导。
2. `/transfer-center/jobs/9c67b0f9-553a-4711-964c-dbdb702c2449/review`：有效视频播放/暂停、B站与抖音版本切换无误拦。
3. 展开详情：媒体依赖按钮与状态一致、历史镜头表和发布记录保留；MPT 已有项目同步是独立恢复入口。

实际捕获后按原参考图比较，再更新 QA 结果和正式验收回执，恢复临时视口；不靠测试数量宣布视觉通过。WorkBuddy 使用自己的合规浏览器能力，不需复制 Codex Node 运行环境。

## 本次独立只读复核

两任务仍是 1 completed/approved、1 review/draft；待审三条外盘路径可读，已完成四条旧 Docker 路径切换前已缺。配置 8 份、LaunchAgent、用户未跟踪文件摘要与 16:50:55 备份一致，数据库完整性通过。

不能继续写“所有业务表完全不变”：旧扫描规则仅 last_scan_at/updated_at 更新至 19:42:28；三个指标检查点中一项尝试 7→8，最后尝试 19:52:27；登录成功通知基线 5→8，既有发布通知 sending→sent。没有新增任务/制作队列/发布尝试/发布事件。这是既有后台记录变化，不是此次交接发起业务操作。

新工坊 enabled=false、auto_render=false；队列 0、发布尝试 0；既有来源规则 enabled=true、auto_prepare=true、auto_publish=false、require_review=true。旧 YouTube 同步 AUTO_MODE_ENABLED=true 的既有记录保持，接手前只读复核，不把新工坊关闭说成全系统自动化关闭。

## 媒体、备份与安全边界

媒体在 `/Volumes/资料盘4T/视频搬运`，卷 UUID `DE6F13E2-6341-4ECB-93AD-BF41EFDB2E6B`。只路由 downloads/temp 等媒体，数据库/凭据不放外盘；外盘掉线、错盘、空间不足停止写入，不回退挤满内盘。视频/制作端保留线分别 60/96 GiB。

UI 备份：`/Users/allice/Library/Application Support/视频搬运通道/backups/20260906-165055-ink-activation`，只读检查用该目录 `verify-snapshot.py verify`。
媒体切换证据：`docs/receipts/media-cutover-20260906.json` 与 `docs/production-automation.md`。不覆盖运行 SQLite/WAL，不删原媒体，不用退役主机回滚。

此次文件交接并未核验当前模型推理、英文声线、真实新样片/投稿/115/收益闭环。优先补 UI，再中文样片、逐平台实际回执与云备份，再低限额定时；具体外部写入按已有对象/阶段批准执行，范围扩大另行确认。新自动工坊 worker 不直接发布；禁止解除 unknown 重试锁来“试试看”。

## 开发命令与记录

```sh
cd '/Users/allice/Documents/系统开发/视频搬运通道'
git status --short
git log -5 --oneline
make status
.venv/bin/python -m pytest -q tests/test_ink_studio.py tests/test_ink_preview.py tests/test_studio_thumbnail.py
.venv/bin/python -m compileall -q app.py modules
node --check static/js/ink-studio.js
git diff --check
```

全量 `make test` 用隔离工作区/数据库；避免临时脚本 import 正式 app.py 触发启动副作用。不重做技术栈，不与其他 Agent 同写，不改全局 Hermes/SSH/SMB，不复制凭据到知识库。已授权相同范围的小修复/登录/补测无需反复询问；真实发布、数据恢复、删除等仍遵循具体批准。修改验证后本地 commit，推送与发布单独按阶段处理。

