#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""本地制作端的受控健康监视与有限自修复。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


MIN_REPAIR_COOLDOWN_SECONDS = 30 * 60
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_CHECK_INTERVAL_SECONDS = 60.0


def _health_result_is_ready(result: Any) -> bool:
    """兼容布尔值和现有 ``money_printer_health`` 字典。"""

    if isinstance(result, Mapping):
        for key in ("ready", "healthy", "success"):
            if key in result:
                return bool(result[key])
    return bool(result)


def _repair_result_succeeded(result: Any) -> bool:
    """修复回调应明确返回成功状态，不把无返回值当作成功。"""

    if isinstance(result, Mapping):
        return bool(result.get("success"))
    return bool(result)


class HealthWatchdog:
    """连续失败到阈值后有限修复的本地健康 watchdog。

    ``health_probe`` 每次都应执行真实服务健康检查。字典结果优先读取
    ``ready``、``healthy`` 或 ``success``。``repair_callback`` 需返回布尔值，
    或返回含 ``success`` 的字典。
    """

    def __init__(
        self,
        health_probe: Callable[[], Any],
        repair_callback: Callable[[], Any],
        *,
        notification_callback: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        repair_cooldown_seconds: float = MIN_REPAIR_COOLDOWN_SECONDS,
    ) -> None:
        if not callable(health_probe):
            raise TypeError("health_probe 必须可调用")
        if not callable(repair_callback):
            raise TypeError("repair_callback 必须可调用")
        if notification_callback is not None and not callable(notification_callback):
            raise TypeError("notification_callback 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")

        self._health_probe = health_probe
        self._repair_callback = repair_callback
        self._notification_callback = notification_callback
        self._clock = clock
        self._check_interval_seconds = max(1.0, float(check_interval_seconds))
        self._failure_threshold = max(
            DEFAULT_FAILURE_THRESHOLD, int(failure_threshold)
        )
        self._repair_cooldown_seconds = max(
            float(MIN_REPAIR_COOLDOWN_SECONDS), float(repair_cooldown_seconds)
        )

        self._state_lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "healthy": None,
            "consecutive_failures": 0,
            "checks_total": 0,
            "repairs_attempted": 0,
            "repairs_succeeded": 0,
            "repairs_failed": 0,
            "failure_threshold": self._failure_threshold,
            "repair_cooldown_seconds": self._repair_cooldown_seconds,
            "last_check_at": None,
            "last_success_at": None,
            "last_failure_at": None,
            "last_repair_attempt_at": None,
            "last_repair_completed_at": None,
            "last_repair_status": "never",
            "last_event": "idle",
        }

    def _snapshot_locked(self) -> dict[str, Any]:
        snapshot = dict(self._state)
        last_attempt = snapshot.get("last_repair_attempt_at")
        if last_attempt is None:
            remaining = 0.0
        else:
            elapsed = max(0.0, float(self._clock()) - float(last_attempt))
            remaining = max(0.0, self._repair_cooldown_seconds - elapsed)
        snapshot["repair_cooldown_remaining_seconds"] = remaining
        return snapshot

    def get_status(self) -> dict[str, Any]:
        """返回不含异常明细和凭据的当前状态快照。"""

        with self._state_lock:
            return self._snapshot_locked()

    def status(self) -> dict[str, Any]:
        """``get_status`` 的服务层简写。"""

        return self.get_status()

    def _notify(self, message: str) -> None:
        if self._notification_callback is None:
            return
        try:
            self._notification_callback(message)
        except Exception:
            # 通知不得影响健康检查与冷却状态。
            return

    def check_once(self) -> dict[str, Any]:
        """执行一次真实健康检查，必要时最多触发一次修复。"""

        with self._check_lock:
            checked_at = float(self._clock())
            try:
                healthy = _health_result_is_ready(self._health_probe())
            except Exception:
                healthy = False

            with self._state_lock:
                self._state["checks_total"] += 1
                self._state["last_check_at"] = checked_at
                self._state["healthy"] = healthy
                if healthy:
                    self._state["consecutive_failures"] = 0
                    self._state["last_success_at"] = checked_at
                    self._state["last_event"] = "healthy"
                    return self._snapshot_locked()

                self._state["consecutive_failures"] += 1
                self._state["last_failure_at"] = checked_at
                self._state["last_event"] = "probe_failed"
                if self._state["consecutive_failures"] < self._failure_threshold:
                    return self._snapshot_locked()

                last_attempt = self._state.get("last_repair_attempt_at")
                cooldown_ready = (
                    last_attempt is None
                    or checked_at - float(last_attempt)
                    >= self._repair_cooldown_seconds
                )
                if not cooldown_ready:
                    self._state["last_event"] = "repair_cooldown"
                    return self._snapshot_locked()

                # 先记录修复尝试：即使回调抛异常，冷却也会立即生效。
                self._state["last_repair_attempt_at"] = checked_at
                self._state["last_repair_status"] = "running"
                self._state["last_event"] = "repair_started"
                self._state["repairs_attempted"] += 1

            try:
                repair_succeeded = _repair_result_succeeded(self._repair_callback())
            except Exception:
                repair_succeeded = False

            completed_at = float(self._clock())
            with self._state_lock:
                # 每次修复后重新计数，不会因一次失败进入循环重启。
                self._state["consecutive_failures"] = 0
                self._state["last_repair_completed_at"] = completed_at
                if repair_succeeded:
                    self._state["repairs_succeeded"] += 1
                    self._state["last_repair_status"] = "succeeded"
                    self._state["last_event"] = "repair_succeeded"
                    notification = "本地制作端自动修复已执行，后续将继续健康检查。"
                else:
                    self._state["repairs_failed"] += 1
                    self._state["last_repair_status"] = "failed"
                    self._state["last_event"] = "repair_failed"
                    notification = "本地制作端自动修复未完成，请在 MacBook 检查服务状态。"
                snapshot = self._snapshot_locked()

            self._notify(notification)
            return snapshot

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.check_once()
                if self._stop_event.wait(self._check_interval_seconds):
                    break
        finally:
            with self._state_lock:
                self._state["running"] = False

    def start(self) -> bool:
        """启动单一后台检查线程；已启动时不重复创建。"""

        with self._state_lock:
            if self._state["running"]:
                return False
            self._stop_event.clear()
            self._state["running"] = True
            self._thread = threading.Thread(
                target=self._run,
                name="local-health-watchdog",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """请求后台线程停止，超时时返回 ``False``。"""

        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not bool(thread and thread.is_alive())
        if stopped:
            with self._state_lock:
                self._state["running"] = False
        return stopped
