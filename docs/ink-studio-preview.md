# 墨色创作室方向：独立候选预览

2026-09-06，用户从 Product Design 的三个方向中选择第二个。产品名称仍为「视频搬运通道」；墨色创作室只是设计方向，不是新系统。

## 体验与边界

- 预览入口：http://127.0.0.1:15190/。只监听本机，不需要正式管理员账号。
- 每次启动创建全新的临时示例数据；页面上的两条任务、发布记录、城市画面均为设计示例。
- 示例视频为 AI 生成图片编码的 8 秒无声静帧，不是下载素材、真实二剪作品或发布凭证。
- 可以体验导航、播放、版本选择、展开编辑、草稿保存、制作设置与普通设置保存。
- 发布、下载、登录、网盘上传、真实通知和模型调用不会执行；不要在预览填写真实凭据。
- 保存只作用于隔离示例数据库或当前预览内存；重启会回到全新示例。
- 正式服务 http://127.0.0.1:15188/ 保持原状。本候选没有正式切换、远端推送或部署。

## 设计系统

沿用 Flask/Jinja、Bootstrap 与现有 Bootstrap Icons，不增加前端框架或依赖。

| 项目 | 约定 |
| --- | --- |
| 背景 | 墨黑 #111213；导航 #0c0d0f |
| 面板 | 石墨 #191a1d，细分隔线 #323238，减少大阴影与渐变 |
| 文字 | 主文字 #e6e0d5，成片标题暖米色 #e5d8c2，次要文字灰紫 |
| 强调 | 紫色 #b6a0ed，主按钮 #695095；不把每个操作做成高亮 |
| 字体 | 延续本机 PingFang / system sans；正文 15px，辅助文字通常 12–14px |
| 结构 | 大画面预览 + 右侧单条进度 + 下方下一步；制作详情默认折叠 |
| 小屏幕 | 手机、平板上下排列，导航收起；表格与状态摘要只在自身区域滚动 |
| 提示 | 常驻一句结果；相关时提示原因与下一步；技术说明按需展开 |

保留八个现有导航入口和全部制作表单，未假造完整时间轴编辑器，也未为追求参考图的五项导航而删掉旧功能。

## 业务状态口径

- 工作台合并旧 tasks 和新 transfer_jobs，人工审核加入新搬运任务，读取时不改写业务状态。
- 成片就绪、人工确认、提交平台、完成发布分别显示；提交中的任务不引导重复发布。
- 已完成任务的列表操作改为「查看结果」；正常清理本地成片不被解释成从未下载。
- 115 根据持久化的 pending/uploading/completed/failed/disabled/unavailable/skipped 显示。
- 设置中的 AUTO_MODE_ENABLED 明确叫「YouTube 同步流程自动投稿」，不代表自动工坊也已开启。
- 既有发布前确认、各平台结果核对与收益未登记显示未知的流程继续保留。

## 启动与验证

在此候选工作区使用现有虚拟环境：

```sh
'/Users/allice/Documents/系统开发/视频搬运通道/.venv/bin/python' scripts/preview_ink_studio.py --port 15190
```

脚本打印新的示例成片页地址。停止该预览进程不会停止 15188 正式服务。

```sh
'/Users/allice/Documents/系统开发/视频搬运通道/.venv/bin/python' scripts/preview_ink_studio.py --check
'/Users/allice/Documents/系统开发/视频搬运通道/.venv/bin/python' -m pytest -q
'/Users/allice/Documents/系统开发/视频搬运通道/.venv/bin/python' -m compileall -q app.py modules scripts/preview_ink_studio.py
node --check static/js/ink-studio.js
git diff --check
```

这是应用级预览隔离，不是操作系统沙箱。当前受限路由和 Python TCP/UDP/DNS 阻断已有测试；以后新增子进程或路由必须重新检查边界。曾在开发检查中发现默认监听全部地址，已立即停止并改为显式 127.0.0.1；最终实际监听已核验为 loopback。

## 视觉来源

- 选中的原始方向：`docs/design-captures/reference-option-2.png`。
- 成片页验收：`docs/design-captures/review-final.png`。
- 城市示例资产：内置 Image Gen 生成，参考选中方向中的河岸落日、城市天际线、暖金色光线；无文字、无 UI、无人物特写，用于横向视频槽位。
- 原始生成结果：`/Users/allice/.codex/generated_images/019fd9de-e4f0-7871-9d8b-46b933458fdd/exec-a31df1d9-8d39-4740-891a-2d664fef69c4.png`。
- 项目资产：`static/img/ink-demo-sunset.png`、`static/img/ink-demo.mp4`。只有 design_preview 页面使用演示海报；正式页面继续读取自己的任务媒体。

完整验收见项目根目录 `design-qa.md`。切换正式服务须另获用户批准，并先对照正式任务和平台状态验收，不把示例演示当作真实发布/备份成功。
