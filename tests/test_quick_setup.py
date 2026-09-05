from pathlib import Path

import pytest

from modules.quick_setup import (
    QuickSetupHealthService,
    aggregate_health_results,
    build_quick_setup_context,
    build_wizard_state,
    list_presets,
    preset_changes,
    preview_changes,
    validate_changes,
)


ROOT = Path(__file__).resolve().parents[1]


def test_quick_setup_has_exactly_three_credential_free_presets():
    presets = list_presets()

    assert [item["id"] for item in presets] == [
        "personal_stable",
        "trend_fast",
        "multi_platform_growth",
    ]
    sensitive_markers = ("password", "secret", "token", "cookie", "api_key", "access_key")
    for preset in presets:
        assert preset["target_platforms"]
        assert preset["processing_mode"] in {"quick", "professional"}
        assert not any(
            marker in key.lower()
            for key in preset["changes"]
            for marker in sensitive_markers
        )
        assert preset["changes"]["TRANSFER_MPT_WATCHDOG_ENABLED"] is True
        assert preset["changes"]["TRANSFER_TELEGRAM_INTAKE_ENABLED"] is True
        assert preset["changes"]["TRANSFER_TELEGRAM_INTAKE_DEFAULT_MODE"] == preset["processing_mode"]


def test_presets_return_defensive_copies():
    first = list_presets()
    first[0]["changes"]["MAX_CONCURRENT_TASKS"] = 99

    assert list_presets()[0]["changes"]["MAX_CONCURRENT_TASKS"] == 1


def test_validate_changes_normalizes_safe_values_and_rejects_credentials():
    result = validate_changes({
        "MAX_CONCURRENT_TASKS": "2",
        "AUTO_MODE_ENABLED": "off",
        "OPENAI_API_KEY": "must-not-be-accepted",
    })

    assert result["valid"] is False
    assert result["normalized_changes"] == {
        "MAX_CONCURRENT_TASKS": 2,
        "AUTO_MODE_ENABLED": False,
    }
    assert result["errors"] == [
        {"key": "OPENAI_API_KEY", "message": "快速配置不允许修改凭据"}
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"MAX_CONCURRENT_TASKS": 99}, "不能大于 8"),
        ({"VIDEO_ENCODER": "apple_magic"}, "不在允许范围内"),
        ({"UNKNOWN_FIELD": "x"}, "不支持通过快速配置修改"),
    ],
)
def test_validate_changes_reports_invalid_values(changes, message):
    result = validate_changes(changes)

    assert result["valid"] is False
    assert result["errors"][0]["message"] == message


def test_preview_only_returns_changed_safe_settings():
    result = preview_changes(
        {"MAX_CONCURRENT_TASKS": 1, "VIDEO_ENCODER": "cpu"},
        {"MAX_CONCURRENT_TASKS": 1, "VIDEO_ENCODER": "auto"},
    )

    assert result["valid"] is True
    assert result["changed_count"] == 1
    assert result["items"][0] == {
        "key": "VIDEO_ENCODER",
        "label": "视频编码器",
        "before": "cpu",
        "after": "auto",
        "category": "media",
    }


def test_preview_does_not_report_string_and_integer_forms_as_changes():
    result = preview_changes(
        {"MAX_CONCURRENT_UPLOADS": "1", "AUTO_MODE_ENABLED": "off"},
        {"MAX_CONCURRENT_UPLOADS": 1, "AUTO_MODE_ENABLED": False},
    )

    assert result["valid"] is True
    assert result["changed_count"] == 0


def test_health_aggregation_keeps_required_and_optional_results_separate():
    result = aggregate_health_results(
        {
            "bilibili_publish": {"connected": True, "message": "已连接"},
            "money_printer": {"ready": False, "message": "未启动"},
            "telegram": {"status": "warning", "message": "可选"},
        },
        ("bilibili_publish", "money_printer"),
        ("telegram",),
    )

    assert result["status"] == "blocked"
    assert result["score"] == 50
    assert result["required_ready"] == 1
    assert result["required_error"] == 1
    assert result["items"][-1]["required"] is False


def test_health_service_contains_runner_errors_and_never_exposes_secret_details():
    def broken_check():
        raise RuntimeError("secret backend detail")

    service = QuickSetupHealthService({
        "bilibili_publish": lambda: {
            "connected": True,
            "message": "已连接",
            "access_token": "must-not-leak",
            "channel": "demo",
        },
        "money_printer": broken_check,
    })

    result = service.run(("bilibili_publish", "money_printer"))

    first, second = result["items"]
    assert first["details"] == {"channel": "demo"}
    assert second["status"] == "error"
    assert "secret backend detail" not in second["message"]


def test_four_step_wizard_advances_from_goal_to_connections_to_apply_to_verify():
    preset_id = "personal_stable"
    initial = build_wizard_state({}, preset_id=None, health_results={})
    assert len(initial["steps"]) == 4
    assert initial["current_step"] == "goal"

    selected = build_wizard_state({}, preset_id=preset_id, health_results={})
    assert selected["current_step"] == "connections"

    changes = preset_changes(preset_id)
    all_ready = {
        component_id: {"ready": True}
        for component_id in (
            "bilibili_source", "bilibili_publish", "ai", "money_printer", "backup_115", "telegram"
        )
    }
    connected = build_wizard_state({}, preset_id=preset_id, health_results=all_ready)
    assert connected["current_step"] == "processing"

    verified = build_wizard_state(changes, preset_id=preset_id, health_results=all_ready)
    assert verified["current_step"] == "verify"
    assert verified["complete"] is True
    assert all(step["complete"] for step in verified["steps"])


def test_context_and_template_expose_four_step_non_secret_workflow():
    context = build_quick_setup_context({}, "trend_fast", {})
    template = (ROOT / "templates" / "quick_setup.html").read_text(encoding="utf-8")

    assert len(context["wizard"]["steps"]) == 4
    assert template.count("步骤 ") == 4
    assert "个人稳妥" not in template  # 预设内容由模块数据渲染
    assert "API Key" in template
    assert 'name="OPENAI_API_KEY"' not in template
    assert 'name="password"' not in template


def test_quick_setup_route_renders_instead_of_treating_items_as_dict_method(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "load_config", lambda: {})
    monkeypatch.setattr(app_module, "list_config_snapshots", lambda: [])
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/quick-setup")

    assert response.status_code == 200
    assert "快速配置" in response.get_data(as_text=True)
