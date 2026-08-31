import subprocess

import pytest

from modules.local_control import (
    launchd_service_state,
    repair_money_printer,
    restart_launchd_service,
)


def _completed(arguments, *, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout=stdout, stderr=stderr)


def test_launchd_state_parses_only_allowlisted_service():
    calls = []

    def runner(arguments):
        calls.append(arguments)
        return _completed(
            arguments,
            stdout="""
                state = running
                pid = 4321
                last exit code = 1
            """,
        )

    state = launchd_service_state("money_printer", uid=501, runner=runner)

    assert calls == [
        ["print", "gui/501/com.sg99.moneyprinter-video-worker.local"]
    ]
    assert state["loaded"] is True
    assert state["running"] is True
    assert state["pid"] == 4321
    assert state["last_exit_code"] == 1

    with pytest.raises(ValueError, match="不支持的本地服务"):
        launchd_service_state("arbitrary-label", runner=runner)


def test_restart_uses_fixed_launchd_target():
    calls = []

    def runner(arguments):
        calls.append(arguments)
        return _completed(arguments)

    result = restart_launchd_service("money_printer", uid=501, runner=runner)

    assert result["restarted"] is True
    assert calls == [
        ["kickstart", "-k", "gui/501/com.sg99.moneyprinter-video-worker.local"]
    ]


def test_repair_skips_restart_when_authenticated_probe_is_ready():
    calls = []
    result = repair_money_printer(
        lambda: {"ready": True},
        runner=lambda arguments: calls.append(arguments),
        sleeper=lambda _: None,
    )

    assert result["success"] is True
    assert result["action"] == "none"
    assert calls == []


def test_repair_restarts_and_waits_for_authenticated_probe():
    checks = iter(
        [
            {"ready": False},
            {"ready": False},
            {"ready": True, "message": "ok"},
        ]
    )
    calls = []

    def runner(arguments):
        calls.append(arguments)
        return _completed(arguments)

    result = repair_money_printer(
        lambda: next(checks),
        attempts=3,
        interval_seconds=0,
        runner=runner,
        sleeper=lambda _: None,
    )

    assert result["success"] is True
    assert result["action"] == "restart"
    assert calls[0][:2] == ["kickstart", "-k"]


def test_repair_reports_failed_authenticated_probe_after_restart():
    result = repair_money_printer(
        lambda: {"ready": False},
        attempts=2,
        interval_seconds=0,
        runner=lambda arguments: _completed(arguments),
        sleeper=lambda _: None,
    )

    assert result["success"] is False
    assert "仍未恢复" in result["message"]
