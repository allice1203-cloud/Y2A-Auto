import json
from unittest.mock import Mock, patch

import pytest
import requests

import modules.config_manager as config_module
from modules.config_manager import DEFAULT_CONFIG
from modules.notifications.adapters import (
    CHANNEL_TELEGRAM,
    NotificationSendError,
    TelegramNotifier,
    build_notifier_registry,
    detect_latest_telegram_chat,
    iter_enabled_channel_ids,
    validate_channel_config_fields,
)
from modules.notifications.models import NotificationMessage
from modules.notifications import (
    EVENT_SYSTEM_WATCHDOG,
    NotificationEvent,
    build_notification_message,
)


def _message():
    return NotificationMessage(
        title="搬运任务完成",
        summary="视频已进入人工确认",
        markdown="**任务：** demo",
    )


def test_telegram_channel_defaults_to_disabled_and_requires_two_credentials():
    assert DEFAULT_CONFIG["NOTIFY_TELEGRAM_ENABLED"] is False
    assert DEFAULT_CONFIG["NOTIFY_TELEGRAM_BOT_TOKEN"] == ""
    assert DEFAULT_CONFIG["NOTIFY_TELEGRAM_CHAT_ID"] == ""
    assert DEFAULT_CONFIG["NOTIFY_EVENT_TRANSFER_REVIEW_READY"] is True
    assert DEFAULT_CONFIG["NOTIFY_EVENT_TRANSFER_PUBLISHED"] is True
    assert DEFAULT_CONFIG["NOTIFY_EVENT_TRANSFER_FAILED"] is True
    assert DEFAULT_CONFIG["NOTIFY_EVENT_SYSTEM_WATCHDOG"] is True
    assert DEFAULT_CONFIG["TRANSFER_TELEGRAM_INTAKE_ENABLED"] is True
    assert validate_channel_config_fields(CHANNEL_TELEGRAM, {}) == [
        "NOTIFY_TELEGRAM_BOT_TOKEN",
        "NOTIFY_TELEGRAM_CHAT_ID",
    ]


def test_transfer_notification_event_flags_are_persisted(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config_module,
        "get_app_subdir",
        lambda name: str(tmp_path / name),
    )

    updated = config_module.update_config(
        {
            "NOTIFY_EVENT_TRANSFER_REVIEW_READY": "off",
            "NOTIFY_EVENT_TRANSFER_PUBLISHED": "on",
            "NOTIFY_EVENT_TRANSFER_FAILED": "off",
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert updated["NOTIFY_EVENT_TRANSFER_REVIEW_READY"] is False
    assert updated["NOTIFY_EVENT_TRANSFER_PUBLISHED"] is True
    assert updated["NOTIFY_EVENT_TRANSFER_FAILED"] is False
    assert saved["NOTIFY_EVENT_TRANSFER_REVIEW_READY"] is False
    assert saved["NOTIFY_EVENT_TRANSFER_PUBLISHED"] is True
    assert saved["NOTIFY_EVENT_TRANSFER_FAILED"] is False


def test_watchdog_notification_is_fixed_and_points_to_local_quick_setup():
    message = build_notification_message(
        NotificationEvent(EVENT_SYSTEM_WATCHDOG, {"status": "needs_attention"})
    )

    assert "本地制作端自检" in message.title
    assert "MacBook" in message.markdown
    assert "快速配置" in message.markdown
    assert "http" not in message.markdown


def test_telegram_channel_is_registered_and_can_be_enabled():
    assert isinstance(build_notifier_registry()[CHANNEL_TELEGRAM], TelegramNotifier)
    assert iter_enabled_channel_ids({"NOTIFY_TELEGRAM_ENABLED": "on"}) == [CHANNEL_TELEGRAM]


@patch("modules.notifications.adapters.requests.post")
def test_telegram_send_uses_bot_api_without_markdown_parse_mode(mock_post):
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 1}}
    mock_post.return_value = response

    TelegramNotifier().send(
        _message(),
        {
            "NOTIFY_TELEGRAM_BOT_TOKEN": "123456:test-token",
            "NOTIFY_TELEGRAM_CHAT_ID": "987654321",
        },
    )

    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.telegram.org/bot123456:test-token/sendMessage"
    assert payload["chat_id"] == "987654321"
    assert "parse_mode" not in payload
    assert mock_post.call_args.kwargs["timeout"] == 10


@patch("modules.notifications.adapters.requests.post")
def test_telegram_network_error_does_not_expose_bot_token(mock_post):
    mock_post.side_effect = requests.RequestException(
        "failed for https://api.telegram.org/bot123456:secret/sendMessage"
    )

    with pytest.raises(NotificationSendError) as exc_info:
        TelegramNotifier().send(
            _message(),
            {
                "NOTIFY_TELEGRAM_BOT_TOKEN": "123456:secret",
                "NOTIFY_TELEGRAM_CHAT_ID": "987654321",
            },
        )

    assert str(exc_info.value) == "Telegram 网络请求失败"
    assert "secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@patch("modules.notifications.adapters.requests.post")
def test_telegram_send_rejects_unsuccessful_api_response(mock_post):
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {"ok": False, "description": "Bad Request: chat not found"}
    mock_post.return_value = response

    with pytest.raises(NotificationSendError, match="chat not found"):
        TelegramNotifier().send(
            _message(),
            {
                "NOTIFY_TELEGRAM_BOT_TOKEN": "123456:test-token",
                "NOTIFY_TELEGRAM_CHAT_ID": "987654321",
            },
        )


@patch("modules.notifications.adapters.requests.get")
def test_detect_latest_telegram_chat_uses_most_recent_message(mock_get):
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 100, "first_name": "旧会话"}}},
            {"message": {"chat": {"id": 200, "first_name": "Alice"}}},
        ],
    }
    mock_get.return_value = response

    assert detect_latest_telegram_chat("123456:test-token") == {
        "chat_id": "200",
        "display_name": "Alice",
    }


@patch("modules.notifications.adapters.requests.get")
def test_detect_latest_telegram_chat_requires_started_conversation(mock_get):
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {"ok": True, "result": []}
    mock_get.return_value = response

    with pytest.raises(ValueError, match="/start"):
        detect_latest_telegram_chat("123456:test-token")
