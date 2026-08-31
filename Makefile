.PHONY: help test status restart logs moneyprinter-status moneyprinter-restart legacy-release-test

PYTHON := .venv/bin/python
VIDEO_SERVICE := com.sg99.video-transfer-channel.local
MONEYPRINTER_SERVICE := com.sg99.moneyprinter-video-worker.local
USER_DOMAIN := gui/$(shell id -u)

help:
	@echo "MacBook 本地视频搬运管理："
	@echo "  make test                 运行完整测试"
	@echo "  make status               检查视频工作台真实入口"
	@echo "  make restart              重启视频工作台"
	@echo "  make logs                 查看视频工作台最近日志"
	@echo "  make moneyprinter-status  检查超级印钞机 LaunchAgent"
	@echo "  make moneyprinter-restart 重启超级印钞机"

test:
	$(PYTHON) -m pytest -q

status:
	@launchctl print $(USER_DOMAIN)/$(VIDEO_SERVICE) >/dev/null
	@curl --fail --silent --show-error http://127.0.0.1:15188/login >/dev/null
	@echo "视频工作台运行正常：http://127.0.0.1:15188"

restart:
	launchctl kickstart -k $(USER_DOMAIN)/$(VIDEO_SERVICE)

logs:
	@tail -n 120 "$(HOME)/Library/Logs/视频搬运通道/video-transfer.err.log"

moneyprinter-status:
	@launchctl print $(USER_DOMAIN)/$(MONEYPRINTER_SERVICE) >/dev/null
	@echo "超级印钞机 LaunchAgent 已加载；真实 API 状态请在快速配置页检查。"

moneyprinter-restart:
	launchctl kickstart -k $(USER_DOMAIN)/$(MONEYPRINTER_SERVICE)

# 仅保留历史 Docker 发布包的隔离回归，不属于当前运行方式。
legacy-release-test:
	./scripts/run_release_tests.sh
