#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""MacBook 本地视频运行服务的受限状态与修复操作。"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any


LOCAL_SERVICE_LABELS = {
    "money_printer": "com.sg99.moneyprinter-video-worker.local",
}


def _run_launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def launchd_service_state(
    service: str,
    *,
    uid: int | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_launchctl,
) -> dict[str, Any]:
    """读取固定白名单 LaunchAgent 的状态，不接受任意 service label。"""

    label = LOCAL_SERVICE_LABELS.get(str(service or "").strip())
    if not label:
        raise ValueError("不支持的本地服务")

    target_uid = int(os.getuid() if uid is None else uid)
    result = runner(["print", f"gui/{target_uid}/{label}"])
    output = str(result.stdout or "")
    state_match = re.search(r"^\s*state\s*=\s*([^\n]+)", output, re.MULTILINE)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code\s*=\s*(-?\d+)", output, re.MULTILINE)
    loaded = result.returncode == 0
    state = state_match.group(1).strip() if state_match else ("loaded" if loaded else "unloaded")
    return {
        "service": service,
        "label": label,
        "loaded": loaded,
        "running": loaded and state == "running",
        "state": state,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
    }


def restart_launchd_service(
    service: str,
    *,
    uid: int | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_launchctl,
) -> dict[str, Any]:
    """仅重启白名单中的本地 LaunchAgent。"""

    label = LOCAL_SERVICE_LABELS.get(str(service or "").strip())
    if not label:
        raise ValueError("不支持的本地服务")

    target_uid = int(os.getuid() if uid is None else uid)
    result = runner(["kickstart", "-k", f"gui/{target_uid}/{label}"])
    if result.returncode != 0:
        raise RuntimeError("本地服务重启失败")
    return {"service": service, "label": label, "restarted": True}


def repair_money_printer(
    health_probe: Callable[[], dict[str, Any]],
    *,
    attempts: int = 12,
    interval_seconds: float = 0.5,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_launchctl,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """真实 API 不健康时重启制作端，并在有限时间内复验。"""

    before = dict(health_probe() or {})
    if before.get("ready"):
        return {
            "success": True,
            "action": "none",
            "message": "超级印钞机已经可用，无需重启",
            "health": before,
        }

    restart_launchd_service("money_printer", runner=runner)
    latest = before
    for _ in range(max(1, min(int(attempts), 30))):
        sleeper(max(0.0, float(interval_seconds)))
        latest = dict(health_probe() or {})
        if latest.get("ready"):
            return {
                "success": True,
                "action": "restart",
                "message": "超级印钞机已重启并通过真实 API 检查",
                "health": latest,
            }

    return {
        "success": False,
        "action": "restart",
        "message": "超级印钞机已重启，但真实 API 仍未恢复",
        "health": latest,
    }
