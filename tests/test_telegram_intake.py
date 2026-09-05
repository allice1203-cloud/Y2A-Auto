from __future__ import annotations

import threading

import pytest

from modules.telegram_intake import (
    FileUpdateCheckpoint,
    HELP_TEXT,
    MemoryUpdateCheckpoint,
    TelegramBotApiClient,
    TelegramIntakeConfig,
    TelegramIntakeService,
    TelegramIntakeTransportError,
    TelegramIntakeValidationError,
    parse_telegram_message,
)


BOT_TOKEN = "FAKE-TELEGRAM-BOT-TOKEN"


def make_config(**overrides):
    values = {
        "bot_token": BOT_TOKEN,
        "allowed_chat_id": "987654321",
        "default_processing_mode": "professional",
        "default_target_platforms": ("bilibili",),
        "poll_timeout_seconds": 1,
        "backoff_seconds": (0, 0.01),
    }
    values.update(overrides)
    return TelegramIntakeConfig(**values)


class FakeClient:
    def __init__(self, batches=None, *, poll_error=None):
        self.batches = list(batches or [])
        self.poll_error = poll_error
        self.poll_calls = []
        self.sent = []

    def get_updates(self, *, offset, timeout_seconds):
        self.poll_calls.append({"offset": offset, "timeout_seconds": timeout_seconds})
        if self.poll_error is not None:
            raise self.poll_error
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), str(text)))


def update(update_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_config_and_client_repr_never_expose_bot_token():
    config = make_config()
    client = TelegramBotApiClient(BOT_TOKEN)

    assert BOT_TOKEN not in repr(config)
    assert BOT_TOKEN not in repr(client)
    assert config.default_target_platforms == ("bilibili",)


def test_start_and_single_link_use_safe_defaults():
    assert parse_telegram_message("/start@my_video_bot welcome").kind == "start"

    parsed = parse_telegram_message("https://x.com/demo/status/1")

    assert parsed.kind == "tasks"
    assert parsed.source_urls == ("https://x.com/demo/status/1",)
    assert parsed.processing_mode == "professional"
    assert parsed.target_platforms == ("bilibili",)


def test_multiple_links_are_deduplicated_and_tags_override_only_this_message():
    parsed = parse_telegram_message(
        "https://youtu.be/one\n"
        "https://x.com/demo/status/2\uff0c\n"
        "https://youtu.be/one/\n"
        "#quick #youtube #x"
    )

    assert parsed.source_urls == (
        "https://youtu.be/one",
        "https://x.com/demo/status/2",
    )
    assert parsed.processing_mode == "quick"
    assert parsed.target_platforms == ("x", "youtube")

    next_message = parse_telegram_message("https://www.bilibili.com/video/BV1demo")
    assert next_message.processing_mode == "professional"
    assert next_message.target_platforms == ("bilibili",)

    fragment_is_not_a_control_tag = parse_telegram_message("https://example.com/video#direct")
    assert fragment_is_not_a_control_tag.processing_mode == "professional"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("https://x.com/a/status/1 #direct #professional", "conflicting_modes"),
        ("http://127.0.0.1/private", "private_url"),
        ("http://192.168.1.10/video", "private_url"),
        ("https://example.com/video?access_token=sensitive", "credential_url"),
        ("/settings", "credentials_forbidden"),
        ("token=do-not-send", "credentials_forbidden"),
        ("/publish", "unknown_command"),
    ],
)
def test_parser_rejects_conflicts_private_links_credentials_and_unknown_commands(text, code):
    with pytest.raises(TelegramIntakeValidationError) as caught:
        parse_telegram_message(text)

    assert caught.value.code == code
    assert "sensitive" not in str(caught.value)
    assert "do-not-send" not in str(caught.value)


def test_authorization_is_exact_and_unauthorized_updates_are_silently_checkpointed():
    client = FakeClient(
        [[
            update(10, "not-allowed", "https://x.com/attacker/status/1"),
            update(11, "987654321", "https://x.com/allowed/status/2"),
        ]]
    )
    requests = []
    checkpoint = MemoryUpdateCheckpoint()
    service = TelegramIntakeService(
        make_config(),
        requests.append,
        client=client,
        checkpoint=checkpoint,
    )

    assert service.poll_once() == 2

    assert [request.source_url for request in requests] == ["https://x.com/allowed/status/2"]
    assert all(request.publish_after_prepare is False for request in requests)
    assert checkpoint.get() == 11
    assert len(client.sent) == 1
    assert "1 条" in client.sent[0][1]


def test_multiple_links_create_individual_prepare_requests_without_publish():
    client = FakeClient(
        [[
            update(
                20,
                987654321,
                "https://x.com/a/status/1\nhttps://youtu.be/two #direct #x #tiktok",
            )
        ]]
    )
    requests = []
    service = TelegramIntakeService(make_config(), requests.append, client=client)

    service.poll_once()

    assert [item.source_url for item in requests] == [
        "https://x.com/a/status/1",
        "https://youtu.be/two",
    ]
    assert {item.processing_mode for item in requests} == {"direct"}
    assert {item.target_platforms for item in requests} == {("x", "tiktok")}
    assert all(item.publish_after_prepare is False for item in requests)
    assert "不会自动发布" in client.sent[-1][1]


def test_update_id_checkpoint_deduplicates_repeated_poll_results():
    repeated = update(30, 987654321, "https://x.com/a/status/30")
    client = FakeClient([[repeated], [repeated]])
    requests = []
    checkpoint = MemoryUpdateCheckpoint()
    service = TelegramIntakeService(
        make_config(),
        requests.append,
        client=client,
        checkpoint=checkpoint,
    )

    assert service.poll_once() == 1
    assert service.poll_once() == 0

    assert len(requests) == 1
    assert client.poll_calls == [
        {"offset": None, "timeout_seconds": 1},
        {"offset": 31, "timeout_seconds": 1},
    ]


def test_start_sends_help_and_unknown_command_never_reaches_task_callback():
    client = FakeClient(
        [[
            update(40, 987654321, "/start"),
            update(41, 987654321, "/delete_all"),
        ]]
    )
    requests = []
    service = TelegramIntakeService(make_config(), requests.append, client=client)

    service.poll_once()

    assert requests == []
    assert client.sent[0][1] == HELP_TEXT
    assert "不支持此命令" in client.sent[1][1]


def test_task_exception_and_reporter_receive_only_sanitized_messages():
    raw_secret = "raw-secret-from-worker"
    client = FakeClient([[update(50, 987654321, "https://x.com/a/status/50")]])
    error_codes = []

    def broken_prepare(_request):
        raise RuntimeError(raw_secret)

    service = TelegramIntakeService(
        make_config(),
        broken_prepare,
        client=client,
        error_reporter=error_codes.append,
    )

    service.poll_once()

    assert error_codes == ["telegram_task_create_failed"]
    assert raw_secret not in " ".join(message for _chat_id, message in client.sent)
    assert "失败 1 条" in client.sent[-1][1]


class FakeResponse:
    def __init__(self, payload, *, ok=True, json_error=None):
        self.ok = ok
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class RecordingSession:
    def __init__(self, response):
        self.response = response
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.response


def test_bot_client_uses_outbound_post_long_polling():
    session = RecordingSession(FakeResponse({"ok": True, "result": []}))
    client = TelegramBotApiClient(BOT_TOKEN, session=session, connect_timeout_seconds=3)

    assert client.get_updates(offset=88, timeout_seconds=25) == []

    assert len(session.post_calls) == 1
    url, kwargs = session.post_calls[0]
    assert url.startswith("https://api.telegram.org/bot")
    assert url.endswith("/getUpdates")
    assert kwargs["json"] == {
        "timeout": 25,
        "limit": 100,
        "allowed_updates": ["message"],
        "offset": 88,
    }
    assert kwargs["timeout"] == (3, 28)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"ok": False, "description": "raw-api-secret"}),
        FakeResponse({}, ok=False),
        FakeResponse({}, json_error=ValueError("raw-json-secret")),
    ],
)
def test_bot_client_transport_errors_are_generic(response):
    client = TelegramBotApiClient(BOT_TOKEN, session=RecordingSession(response))

    with pytest.raises(TelegramIntakeTransportError) as caught:
        client.get_updates(offset=None, timeout_seconds=1)

    message = str(caught.value)
    assert BOT_TOKEN not in message
    assert "raw-api-secret" not in message
    assert "raw-json-secret" not in message


class RecordingStopSignal:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.stopped


def test_run_forever_has_bounded_backoff_and_exits_after_failure_limit():
    raw_secret = "network-error-with-private-data"
    client = FakeClient(poll_error=RuntimeError(raw_secret))
    stop_signal = RecordingStopSignal()
    error_codes = []
    service = TelegramIntakeService(
        make_config(max_consecutive_failures=3, backoff_seconds=(0.25, 0.5)),
        lambda _request: None,
        client=client,
        stop_signal=stop_signal,
        error_reporter=error_codes.append,
    )

    service.run_forever()

    assert len(client.poll_calls) == 3
    assert stop_signal.waits == [0.25, 0.5]
    assert service.running is False
    assert service.last_error_code == "telegram_poll_exhausted"
    assert error_codes == ["telegram_poll_failed"] * 3
    assert raw_secret not in " ".join(error_codes)


def test_run_forever_can_retry_forever_until_stopped():
    client = FakeClient(poll_error=TelegramIntakeTransportError())
    stop_signal = RecordingStopSignal()

    def stop_after_second_wait(timeout=None):
        stop_signal.waits.append(timeout)
        if len(stop_signal.waits) >= 2:
            stop_signal.set()
        return stop_signal.stopped

    stop_signal.wait = stop_after_second_wait
    service = TelegramIntakeService(
        make_config(max_consecutive_failures=0, backoff_seconds=(0.25, 0.5)),
        lambda _request: None,
        client=client,
        stop_signal=stop_signal,
    )

    service.run_forever()

    assert len(client.poll_calls) == 2
    assert stop_signal.waits == [0.25, 0.5]
    assert service.running is False
    assert service.last_error_code == "telegram_poll_failed"


def test_initial_discard_retries_in_background_before_normal_polling():
    stop_signal = RecordingStopSignal()

    class RecoveringClient(FakeClient):
        def get_updates(self, *, offset, timeout_seconds):
            self.poll_calls.append({"offset": offset, "timeout_seconds": timeout_seconds})
            if len(self.poll_calls) == 1:
                raise TelegramIntakeTransportError()
            if len(self.poll_calls) == 2:
                return [update(70, 987654321, "https://x.com/old/status/70")]
            stop_signal.set()
            return []

    client = RecoveringClient()
    requests = []
    checkpoint = MemoryUpdateCheckpoint()
    service = TelegramIntakeService(
        make_config(
            discard_pending_on_start=True,
            max_consecutive_failures=0,
            backoff_seconds=(0.01,),
        ),
        requests.append,
        client=client,
        checkpoint=checkpoint,
        stop_signal=stop_signal,
    )

    service.run_forever()

    assert client.poll_calls == [
        {"offset": -1, "timeout_seconds": 1},
        {"offset": -1, "timeout_seconds": 1},
        {"offset": 71, "timeout_seconds": 1},
    ]
    assert checkpoint.get() == 70
    assert requests == []


def test_stop_is_cooperative_and_prevents_new_poll():
    client = FakeClient()
    service = TelegramIntakeService(make_config(), lambda _request: None, client=client)

    service.stop()
    service.run_forever()

    assert client.poll_calls == []
    assert service.running is False


def test_memory_checkpoint_only_moves_forward_and_is_thread_safe():
    checkpoint = MemoryUpdateCheckpoint(5)
    threads = [threading.Thread(target=checkpoint.save, args=(value,)) for value in (3, 9, 7, 11)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert checkpoint.get() == 11


def test_file_checkpoint_is_atomic_private_and_contains_only_update_id(tmp_path):
    path = tmp_path / "state" / "telegram.json"
    checkpoint = FileUpdateCheckpoint(path)

    assert checkpoint.get() is None
    checkpoint.save(12)
    checkpoint.save(8)

    assert checkpoint.get() == 12
    assert path.read_text(encoding="utf-8") == '{"last_update_id": 12}'
    assert path.stat().st_mode & 0o777 == 0o600
    assert BOT_TOKEN not in path.read_text(encoding="utf-8")


def test_discard_pending_updates_checkpoints_without_creating_tasks():
    client = FakeClient([[update(70, 987654321, "https://x.com/old/status/70")]])
    requests = []
    checkpoint = MemoryUpdateCheckpoint()
    service = TelegramIntakeService(
        make_config(),
        requests.append,
        client=client,
        checkpoint=checkpoint,
    )

    assert service.discard_pending_updates() == 1
    assert checkpoint.get() == 70
    assert requests == []
    assert client.sent == []
    assert client.poll_calls == [{"offset": -1, "timeout_seconds": 1}]
    assert service.discard_pending_updates() == 0
