import threading

from modules.health_watchdog import (
    MIN_REPAIR_COOLDOWN_SECONDS,
    HealthWatchdog,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def test_repair_runs_only_after_three_consecutive_real_probe_failures():
    probe_results = iter(
        [
            {"ready": False},
            {"ready": False},
            {"ready": True},
            {"ready": False},
            {"ready": False},
            {"ready": False},
        ]
    )
    repairs = []
    watchdog = HealthWatchdog(
        lambda: next(probe_results),
        lambda: repairs.append("repair") or {"success": True},
    )

    for _ in range(5):
        watchdog.check_once()
    assert repairs == []
    assert watchdog.get_status()["consecutive_failures"] == 2

    status = watchdog.check_once()

    assert repairs == ["repair"]
    assert status["repairs_attempted"] == 1
    assert status["repairs_succeeded"] == 1
    assert status["consecutive_failures"] == 0
    assert status["last_repair_status"] == "succeeded"


def test_failed_repair_does_not_loop_and_cooldown_is_at_least_thirty_minutes():
    clock = FakeClock()
    repairs = []
    watchdog = HealthWatchdog(
        lambda: {"ready": False},
        lambda: repairs.append(clock()) or {"success": False},
        clock=clock,
        repair_cooldown_seconds=5,
    )

    for _ in range(3):
        watchdog.check_once()
    assert repairs == [0.0]
    assert watchdog.get_status()["repair_cooldown_seconds"] == (
        MIN_REPAIR_COOLDOWN_SECONDS
    )

    for _ in range(10):
        clock.advance(60)
        watchdog.check_once()
    assert repairs == [0.0]
    assert watchdog.get_status()["last_repair_status"] == "failed"

    clock.advance(MIN_REPAIR_COOLDOWN_SECONDS - clock())
    watchdog.check_once()

    assert repairs == [0.0, float(MIN_REPAIR_COOLDOWN_SECONDS)]
    assert watchdog.get_status()["repairs_failed"] == 2


def test_probe_exception_counts_as_failure_but_success_clears_the_streak():
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("probe failed with token=secret")
        return {"ready": True}

    watchdog = HealthWatchdog(probe, lambda: {"success": True})

    failed = watchdog.check_once()
    recovered = watchdog.check_once()

    assert failed["healthy"] is False
    assert failed["consecutive_failures"] == 1
    assert recovered["healthy"] is True
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_event"] == "healthy"


def test_repair_failure_notification_and_status_never_expose_exception_details():
    notifications = []

    def failed_repair():
        raise RuntimeError("Authorization: Bearer credential-secret")

    watchdog = HealthWatchdog(
        lambda: False,
        failed_repair,
        notification_callback=notifications.append,
    )

    for _ in range(3):
        status = watchdog.check_once()

    serialized = f"{notifications!r} {status!r}"
    assert len(notifications) == 1
    assert "MacBook" in notifications[0]
    assert "credential-secret" not in serialized
    assert "Authorization" not in serialized
    assert "RuntimeError" not in serialized
    assert status["last_repair_status"] == "failed"


def test_status_returns_a_copy():
    watchdog = HealthWatchdog(lambda: True, lambda: True)

    snapshot = watchdog.check_once()
    snapshot["healthy"] = False

    assert watchdog.status()["healthy"] is True


def test_background_service_starts_once_and_stops_cleanly():
    probed = threading.Event()
    watchdog = HealthWatchdog(
        lambda: probed.set() or True,
        lambda: True,
    )

    assert watchdog.start() is True
    assert watchdog.start() is False
    assert probed.wait(timeout=1.0) is True
    assert watchdog.get_status()["running"] is True

    assert watchdog.stop(timeout=1.0) is True
    assert watchdog.get_status()["running"] is False
