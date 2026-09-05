#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Telegram 本地快速入口。

该模块只使用 Telegram Bot API 的出站长轮询，不启动 Webhook
或本地监听端口。它把已授权会话中的公开视频链接转换为“创建并
准备”请求，不暴露任何自动发布接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

import requests


PROCESSING_MODES = ("direct", "quick", "professional")
TARGET_PLATFORMS = ("x", "youtube", "bilibili", "douyin", "tiktok")

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
_TAG_PATTERN = re.compile(r"(?<![\w#])#([a-z]+)\b", flags=re.IGNORECASE)
_COMMAND_PATTERN = re.compile(r"^/([a-z0-9_]+)(?:@[a-z0-9_]+)?(?:\s|$)", flags=re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff09\u3011\u300b\u300d\u300f"
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bot_token",
    "cookie",
    "credential",
    "key",
    "password",
    "passwd",
    "secret",
    "session",
    "token",
}
_SENSITIVE_COMMANDS = {
    "config",
    "configure",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "secret",
    "settings",
    "setup",
    "token",
}
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?:\b(?:access[_ -]?token|api[_ -]?key|bot[_ -]?token|cookie|credential|password|passwd|secret|token)\b|"
    r"(?:密码|密钥|凭据|\u4ee4牌))\s*[:=]",
    flags=re.IGNORECASE,
)

HELP_TEXT = (
    "Telegram 快速入口已连接。\n"
    "请发送一条或多条公开视频链接（可换行）。\n"
    "处理标签：#direct / #quick / #professional\n"
    "平台标签：#x / #youtube / #bilibili / #douyin / #tiktok\n"
    "每条消息的标签只覆盖本次任务。默认只创建和准备，不会自动发布。\n"
    "此入口不读取、修改或接收设置与凭据。"
)


class TelegramIntakeError(RuntimeError):
    """可安全展示的 Telegram 入口错误。"""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = str(code)
        self.public_message = str(public_message)
        super().__init__(self.public_message)


class TelegramIntakeValidationError(TelegramIntakeError):
    pass


class TelegramIntakeTransportError(TelegramIntakeError):
    def __init__(self, code: str = "telegram_api_unavailable") -> None:
        super().__init__(code, "Telegram 服务暂时不可用，请稍后重试")


def _normalize_mode(value: Any) -> str:
    normalized = str(value or "professional").strip().lower()
    if normalized not in PROCESSING_MODES:
        raise ValueError("默认处理模式无效")
    return normalized


def _normalize_targets(values: Iterable[Any]) -> tuple[str, ...]:
    requested = {str(value or "").strip().lower() for value in values}
    requested.discard("")
    unknown = requested.difference(TARGET_PLATFORMS)
    if unknown:
        raise ValueError("默认目标平台无效")
    normalized = tuple(target for target in TARGET_PLATFORMS if target in requested)
    if not normalized:
        raise ValueError("至少需要一个默认目标平台")
    return normalized


@dataclass(frozen=True)
class TelegramIntakeConfig:
    """Telegram 轮询与任务默认值。密钥不参与 repr。"""

    bot_token: str = field(repr=False)
    allowed_chat_id: str
    default_processing_mode: str = "professional"
    default_target_platforms: tuple[str, ...] = ("bilibili",)
    poll_timeout_seconds: int = 25
    connect_timeout_seconds: int = 10
    max_links_per_message: int = 20
    max_message_chars: int = 12_000
    max_consecutive_failures: int = 8
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
    discard_pending_on_start: bool = False

    def __post_init__(self) -> None:
        token = str(self.bot_token or "").strip()
        chat_id = str(self.allowed_chat_id or "").strip()
        if not token:
            raise ValueError("缺少 Telegram Bot Token")
        if not re.fullmatch(r"-?\d+", chat_id):
            raise ValueError("Telegram Chat ID 无效")
        if not 1 <= int(self.poll_timeout_seconds) <= 50:
            raise ValueError("长轮询时间需介于 1 到 50 秒")
        if not 1 <= int(self.connect_timeout_seconds) <= 60:
            raise ValueError("连接超时时间无效")
        if not 1 <= int(self.max_links_per_message) <= 100:
            raise ValueError("单次链接数上限无效")
        if not 256 <= int(self.max_message_chars) <= 100_000:
            raise ValueError("消息长度上限无效")
        if not 0 <= int(self.max_consecutive_failures) <= 100:
            raise ValueError("连续失败上限无效")
        delays = tuple(float(delay) for delay in self.backoff_seconds)
        if not delays or any(delay < 0 or delay > 300 for delay in delays):
            raise ValueError("重试退避参数无效")

        object.__setattr__(self, "bot_token", token)
        object.__setattr__(self, "allowed_chat_id", chat_id)
        object.__setattr__(self, "default_processing_mode", _normalize_mode(self.default_processing_mode))
        object.__setattr__(self, "default_target_platforms", _normalize_targets(self.default_target_platforms))
        object.__setattr__(self, "poll_timeout_seconds", int(self.poll_timeout_seconds))
        object.__setattr__(self, "connect_timeout_seconds", int(self.connect_timeout_seconds))
        object.__setattr__(self, "max_links_per_message", int(self.max_links_per_message))
        object.__setattr__(self, "max_message_chars", int(self.max_message_chars))
        object.__setattr__(self, "max_consecutive_failures", int(self.max_consecutive_failures))
        object.__setattr__(self, "backoff_seconds", delays)
        object.__setattr__(self, "discard_pending_on_start", bool(self.discard_pending_on_start))


@dataclass(frozen=True)
class ParsedTelegramMessage:
    kind: str
    source_urls: tuple[str, ...] = ()
    processing_mode: str = "professional"
    target_platforms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramTaskRequest:
    """交给现有任务中心的单条请求。发布开关不可由消息设置。"""

    source_url: str
    processing_mode: str
    target_platforms: tuple[str, ...]
    publish_after_prepare: bool = field(default=False, init=False)
    origin: str = field(default="telegram", init=False)


def _validate_public_url(candidate: str) -> str:
    normalized = str(candidate or "").strip().rstrip(_TRAILING_URL_PUNCTUATION)
    try:
        parsed = urlsplit(normalized)
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        _ = parsed.port
    except (TypeError, ValueError):
        raise TelegramIntakeValidationError("invalid_url", "链接格式无法识别") from None

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise TelegramIntakeValidationError("invalid_url", "仅支持公开的 HTTP/HTTPS 视频链接")
    if parsed.username is not None or parsed.password is not None:
        raise TelegramIntakeValidationError("credential_url", "链接不能包含账号或凭据")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise TelegramIntakeValidationError("private_url", "仅支持公开视频链接")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise TelegramIntakeValidationError("private_url", "仅支持公开视频链接") from None
    else:
        if not address.is_global:
            raise TelegramIntakeValidationError("private_url", "仅支持公开视频链接")

    try:
        query_keys = {
            key.strip().lower().replace("-", "_")
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        }
    except ValueError:
        raise TelegramIntakeValidationError("invalid_url", "链接格式无法识别") from None
    if query_keys.intersection(_SENSITIVE_QUERY_KEYS):
        raise TelegramIntakeValidationError("credential_url", "链接不能包含访问凭据")
    return normalized


def parse_telegram_message(
    text: Any,
    *,
    default_processing_mode: str = "professional",
    default_target_platforms: Sequence[str] = ("bilibili",),
    max_links: int = 20,
    max_chars: int = 12_000,
) -> ParsedTelegramMessage:
    """将 Telegram 文本解析为帮助或任务消息。

    失败时只抛出可直接回复用户的脱敏文案，不回显原文或链接。
    """

    normalized_text = str(text or "").strip()
    if not normalized_text:
        raise TelegramIntakeValidationError("empty_message", "请发送至少一条公开视频链接")
    if len(normalized_text) > max(256, int(max_chars)):
        raise TelegramIntakeValidationError("message_too_long", "消息过长，请分批发送链接")

    command_match = _COMMAND_PATTERN.match(normalized_text)
    if command_match:
        command = command_match.group(1).lower()
        if command == "start":
            return ParsedTelegramMessage(kind="start")
        if command in _SENSITIVE_COMMANDS:
            raise TelegramIntakeValidationError(
                "credentials_forbidden",
                "此入口不读取、修改或接收设置与凭据",
            )
        raise TelegramIntakeValidationError("unknown_command", "不支持此命令；发送 /start 查看用法")

    # URL 中的查询键由 _validate_public_url 单独检查；这里只识别
    # 用户在普通文本中尝试投递的凭据，避免两种拒绝原因混在一起。
    non_url_text = _URL_PATTERN.sub("", normalized_text)
    if _SENSITIVE_TEXT_PATTERN.search(non_url_text):
        raise TelegramIntakeValidationError(
            "credentials_forbidden",
            "此入口不接收设置或凭据，请在本地系统设置页完成配置",
        )

    default_mode = _normalize_mode(default_processing_mode)
    default_targets = _normalize_targets(default_target_platforms)
    tags = [match.group(1).lower() for match in _TAG_PATTERN.finditer(non_url_text)]
    selected_modes = {tag for tag in tags if tag in PROCESSING_MODES}
    if len(selected_modes) > 1:
        raise TelegramIntakeValidationError("conflicting_modes", "每次只能选择一种处理模式")
    processing_mode = next(iter(selected_modes), default_mode)
    selected_targets = {tag for tag in tags if tag in TARGET_PLATFORMS}
    target_platforms = (
        tuple(target for target in TARGET_PLATFORMS if target in selected_targets)
        if selected_targets
        else default_targets
    )

    raw_urls = _URL_PATTERN.findall(normalized_text)
    urls: list[str] = []
    seen: set[str] = set()
    normalized_limit = max(1, min(int(max_links), 100))
    for raw_url in raw_urls:
        url = _validate_public_url(raw_url)
        dedupe_key = url.rstrip("/")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        urls.append(url)
        if len(urls) > normalized_limit:
            raise TelegramIntakeValidationError(
                "too_many_links",
                f"单次最多接收 {normalized_limit} 条链接，请分批发送",
            )
    if not urls:
        raise TelegramIntakeValidationError("missing_url", "未找到公开视频链接；发送 /start 查看用法")

    return ParsedTelegramMessage(
        kind="tasks",
        source_urls=tuple(urls),
        processing_mode=processing_mode,
        target_platforms=target_platforms,
    )


class TelegramBotApiClient:
    """Bot API 出站客户端。"""

    def __init__(
        self,
        bot_token: str,
        *,
        session: Any | None = None,
        api_base: str = "https://api.telegram.org",
        connect_timeout_seconds: int = 10,
    ) -> None:
        token = str(bot_token or "").strip()
        if not token:
            raise ValueError("缺少 Telegram Bot Token")
        self._bot_token = token
        self._session = session or requests.Session()
        self._api_base = str(api_base or "https://api.telegram.org").rstrip("/")
        self._connect_timeout_seconds = max(1, int(connect_timeout_seconds))

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(api_base={self._api_base!r}, "
            f"connect_timeout_seconds={self._connect_timeout_seconds!r})"
        )

    def _post(self, method: str, payload: dict[str, Any], *, read_timeout_seconds: int) -> Any:
        try:
            response = self._session.post(
                f"{self._api_base}/bot{self._bot_token}/{method}",
                json=payload,
                timeout=(self._connect_timeout_seconds, max(1, int(read_timeout_seconds))),
            )
            if not bool(getattr(response, "ok", False)):
                raise TelegramIntakeTransportError()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise TelegramIntakeTransportError()
            return data.get("result")
        except TelegramIntakeTransportError:
            raise
        except Exception:
            raise TelegramIntakeTransportError() from None

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": max(1, int(timeout_seconds)),
            "limit": 100,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._post(
            "getUpdates",
            payload,
            read_timeout_seconds=max(1, int(timeout_seconds)) + self._connect_timeout_seconds,
        )
        if not isinstance(result, list):
            raise TelegramIntakeTransportError()
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, chat_id: str, text: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": str(text)[:4096],
                "disable_web_page_preview": True,
            },
            read_timeout_seconds=self._connect_timeout_seconds,
        )


class UpdateCheckpoint(Protocol):
    def get(self) -> int | None: ...

    def save(self, update_id: int) -> None: ...


class MemoryUpdateCheckpoint:
    """内存去重点。生产接入时可注入本地持久化实现。"""

    def __init__(self, initial_update_id: int | None = None) -> None:
        self._value = int(initial_update_id) if initial_update_id is not None else None
        self._lock = threading.Lock()

    def get(self) -> int | None:
        with self._lock:
            return self._value

    def save(self, update_id: int) -> None:
        normalized = int(update_id)
        with self._lock:
            if self._value is None or normalized > self._value:
                self._value = normalized


class FileUpdateCheckpoint:
    """Only persist the latest Telegram update ID locally.

    The file contains no Bot Token, Chat ID, message body, or source URL. Writes
    use a same-directory temporary file and atomic replacement so an abrupt Mac
    shutdown does not rewind the checkpoint and recreate tasks.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()

    def _read_unlocked(self) -> int | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            value = payload.get("last_update_id") if isinstance(payload, dict) else None
            if isinstance(value, bool):
                return None
            normalized = int(value)
            return normalized if normalized >= 0 else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def get(self) -> int | None:
        with self._lock:
            return self._read_unlocked()

    def save(self, update_id: int) -> None:
        normalized = int(update_id)
        if normalized < 0:
            raise ValueError("Telegram update ID 无效")
        with self._lock:
            current = self._read_unlocked()
            if current is not None and normalized <= current:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as file_obj:
                    json.dump({"last_update_id": normalized}, file_obj)
                    file_obj.flush()
                    os.fsync(file_obj.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
                os.chmod(self._path, 0o600)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class TelegramIntakeService:
    """长轮询编排器：授权、去重、解析、创建并准备。"""

    def __init__(
        self,
        config: TelegramIntakeConfig,
        create_and_prepare_task: Callable[[TelegramTaskRequest], Any],
        *,
        client: Any | None = None,
        checkpoint: UpdateCheckpoint | None = None,
        stop_signal: StopSignal | None = None,
        error_reporter: Callable[[str], Any] | None = None,
    ) -> None:
        if not callable(create_and_prepare_task):
            raise TypeError("create_and_prepare_task 必须可调用")
        self.config = config
        self._create_and_prepare_task = create_and_prepare_task
        self._client = client or TelegramBotApiClient(
            config.bot_token,
            connect_timeout_seconds=config.connect_timeout_seconds,
        )
        self._checkpoint = checkpoint or MemoryUpdateCheckpoint()
        self._stop_signal = stop_signal or threading.Event()
        self._error_reporter = error_reporter
        self._state_lock = threading.Lock()
        self._running = False
        self.last_error_code: str | None = None

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    def stop(self) -> None:
        self._stop_signal.set()

    def _report_error(self, code: str) -> None:
        self.last_error_code = str(code)
        if self._error_reporter is None:
            return
        try:
            self._error_reporter(str(code))
        except Exception:
            return

    def _send_reply(self, text: str) -> None:
        try:
            self._client.send_message(self.config.allowed_chat_id, text)
        except Exception:
            self._report_error("telegram_reply_failed")

    @staticmethod
    def _update_id(update: dict[str, Any]) -> int | None:
        value = update.get("update_id")
        if isinstance(value, bool):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized >= 0 else None

    def _handle_authorized_text(self, text: Any) -> None:
        try:
            parsed = parse_telegram_message(
                text,
                default_processing_mode=self.config.default_processing_mode,
                default_target_platforms=self.config.default_target_platforms,
                max_links=self.config.max_links_per_message,
                max_chars=self.config.max_message_chars,
            )
        except TelegramIntakeValidationError as exc:
            self._send_reply(exc.public_message)
            return
        except Exception:
            self._report_error("telegram_parse_failed")
            self._send_reply("消息无法处理，请发送 /start 查看用法")
            return

        if parsed.kind == "start":
            self._send_reply(HELP_TEXT)
            return

        created_count = 0
        failed_count = 0
        for source_url in parsed.source_urls:
            request = TelegramTaskRequest(
                source_url=source_url,
                processing_mode=parsed.processing_mode,
                target_platforms=parsed.target_platforms,
            )
            try:
                self._create_and_prepare_task(request)
                created_count += 1
            except Exception:
                failed_count += 1
                self._report_error("telegram_task_create_failed")

        target_text = " / ".join(parsed.target_platforms)
        if failed_count:
            self._send_reply(
                f"已创建并进入准备 {created_count} 条，失败 {failed_count} 条；"
                f"处理模式 {parsed.processing_mode}，目标 {target_text}。不会自动发布。"
            )
            return
        self._send_reply(
            f"已创建并进入准备 {created_count} 条；处理模式 {parsed.processing_mode}，"
            f"目标 {target_text}。不会自动发布。"
        )

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if chat_id is None or str(chat_id).strip() != self.config.allowed_chat_id:
            return
        self._handle_authorized_text(message.get("text"))

    def poll_once(self) -> int:
        """执行一次长轮询，返回已确认的新 update 数。"""

        if self._stop_signal.is_set():
            return 0
        last_update_id = self._checkpoint.get()
        offset = last_update_id + 1 if last_update_id is not None else None
        try:
            updates = self._client.get_updates(
                offset=offset,
                timeout_seconds=self.config.poll_timeout_seconds,
            )
        except TelegramIntakeTransportError:
            raise
        except Exception:
            raise TelegramIntakeTransportError("telegram_poll_failed") from None

        ordered: list[tuple[int, dict[str, Any]]] = []
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            update_id = self._update_id(update)
            if update_id is None:
                continue
            ordered.append((update_id, update))
        ordered.sort(key=lambda item: item[0])

        acknowledged = 0
        for update_id, update in ordered:
            current = self._checkpoint.get()
            if current is not None and update_id <= current:
                continue
            try:
                self._handle_update(update)
            except Exception:
                self._report_error("telegram_update_failed")
            finally:
                self._checkpoint.save(update_id)
                acknowledged += 1
        return acknowledged

    def discard_pending_updates(self) -> int:
        """Discard messages that predate the first local service startup.

        Telegram's negative offset requests the latest pending update and
        forgets earlier pending updates. The newest ID is checkpointed without
        parsing or creating a task. Call this only when no checkpoint exists.
        """

        if self._checkpoint.get() is not None:
            return 0
        try:
            updates = self._client.get_updates(offset=-1, timeout_seconds=1)
        except TelegramIntakeTransportError:
            raise
        except Exception:
            raise TelegramIntakeTransportError("telegram_prime_failed") from None
        update_ids = [
            update_id
            for update_id in (
                self._update_id(update)
                for update in updates if isinstance(update, dict)
            )
            if update_id is not None
        ]
        if update_ids:
            self._checkpoint.save(max(update_ids))
        return len(update_ids)

    def run_forever(self) -> None:
        """运行可停止的长轮询，并在网络恢复后自动继续。

        ``max_consecutive_failures=0`` 表示常驻服务不因临时网络故障
        永久退出。首次启用时可把历史消息丢弃动作放进同一重试循环，
        避免电脑启动早于 VPN 时整个 Telegram 入口直接失效。
        """

        with self._state_lock:
            if self._running:
                raise RuntimeError("Telegram 接入服务已在运行")
            self._running = True

        failures = 0
        pending_initial_discard = bool(
            self.config.discard_pending_on_start and self._checkpoint.get() is None
        )
        try:
            while not self._stop_signal.is_set():
                try:
                    if pending_initial_discard:
                        self.discard_pending_updates()
                        pending_initial_discard = False
                    else:
                        self.poll_once()
                    failures = 0
                    self.last_error_code = None
                except Exception:
                    failures += 1
                    self._report_error(
                        "telegram_prime_failed"
                        if pending_initial_discard
                        else "telegram_poll_failed"
                    )
                    if (
                        self.config.max_consecutive_failures > 0
                        and failures >= self.config.max_consecutive_failures
                    ):
                        self.last_error_code = "telegram_poll_exhausted"
                        break
                    delay = self.config.backoff_seconds[
                        min(failures - 1, len(self.config.backoff_seconds) - 1)
                    ]
                    if self._stop_signal.wait(delay):
                        break
        finally:
            with self._state_lock:
                self._running = False


__all__ = [
    "FileUpdateCheckpoint",
    "HELP_TEXT",
    "MemoryUpdateCheckpoint",
    "PROCESSING_MODES",
    "ParsedTelegramMessage",
    "TARGET_PLATFORMS",
    "TelegramBotApiClient",
    "TelegramIntakeConfig",
    "TelegramIntakeError",
    "TelegramIntakeService",
    "TelegramIntakeTransportError",
    "TelegramIntakeValidationError",
    "TelegramTaskRequest",
    "parse_telegram_message",
]
