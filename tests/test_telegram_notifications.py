from unittest.mock import Mock, patch

import pytest
import requests

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
    assert validate_channel_config_fields(CHANNEL_TELEGRAM, {}) == [
        "NOTIFY_TELEGRAM_BOT_TOKEN",
        "NOTIFY_TELEGRAM_CHAT_ID",
    ]


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
