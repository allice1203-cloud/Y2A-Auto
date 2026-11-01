from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from ..utils import get_app_subdir
from .adapters import (
    ALL_CHANNELS,
    CHANNEL_LABELS,
    NotificationSendError,
    build_notifier_registry,
    iter_enabled_channel_ids,
)
from .models import EVENT_CONFIG_KEY_MAP, NotificationEvent, NotificationMessage
from .renderers import build_notification_message


logger = logging.getLogger("notifications")

DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 30000
DEFAULT_RETRY_SCHEDULE_SECONDS = (30, 120, 600, 1800, 3600)
# 通知在队列里最多允许滞留多久。超过这个年龄就不再补发：发送渠道长时间不可用时，
# 旧告警会在恢复瞬间成批涌入，用户既用不上（事态早已变化）又被刷屏。
DEFAULT_MAX_QUEUE_AGE_SECONDS = 1800
MAX_QUEUE_AGE_CONFIG_KEY = "NOTIFY_MAX_QUEUE_AGE_SECONDS"

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_SENDING = "sending"
OUTBOX_STATUS_SENT = "sent"
OUTBOX_STATUS_FAILED = "failed"
OUTBOX_STATUS_EXPIRED = "expired"

_global_notification_service = None
_global_notification_lock = threading.Lock()


def _current_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("true", "1", "on", "yes")


def _notification_db_path() -> str:
    db_dir = get_app_subdir("db")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "tasks.db")


def is_notification_enabled(config: dict[str, Any] | None) -> bool:
    normalized = dict(config or {})
    return _as_bool(normalized.get("NOTIFY_ENABLED", False))


def is_notification_event_enabled(config: dict[str, Any] | None, event_type: str) -> bool:
    normalized = dict(config or {})
    if not is_notification_enabled(normalized):
        return False
    config_key = EVENT_CONFIG_KEY_MAP.get(event_type)
    if not config_key:
        return False
    return _as_bool(normalized.get(config_key, False))


class NotificationService:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db_path: str | None = None,
        retry_schedule_seconds: tuple[int, ...] = DEFAULT_RETRY_SCHEDULE_SECONDS,
        start_worker: bool = True,
    ) -> None:
        self.config = dict(config or {})
        self.db_path = db_path or _notification_db_path()
        self.retry_schedule_seconds = tuple(retry_schedule_seconds or DEFAULT_RETRY_SCHEDULE_SECONDS)
        self.max_attempts = len(self.retry_schedule_seconds)
        self.max_queue_age_seconds = self._resolve_max_queue_age(self.config)
        self._registry = build_notifier_registry()
        self._db_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread = None

        self.init_db()
        if start_worker:
            self._start_worker()

    @staticmethod
    def _resolve_max_queue_age(config: dict[str, Any]) -> int:
        raw = config.get(MAX_QUEUE_AGE_CONFIG_KEY)
        try:
            value = int(raw) if raw is not None else DEFAULT_MAX_QUEUE_AGE_SECONDS
        except (TypeError, ValueError):
            value = DEFAULT_MAX_QUEUE_AGE_SECONDS
        return max(0, value)

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_retry "
                "ON notification_outbox(status, next_retry_at, created_at)"
            )
            conn.execute(
                "UPDATE notification_outbox SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE status = ?",
                (OUTBOX_STATUS_PENDING, OUTBOX_STATUS_SENDING),
            )
            conn.commit()

    def refresh_config(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_queue_age_seconds = self._resolve_max_queue_age(self.config)
        self._wake_event.set()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """退出时提交/回滚，并**始终关闭**连接。

        注意：sqlite3.Connection 直接用于 ``with`` 时只处理事务、**不关闭连接**
        （Python 官方语义）。历史实现返回裸连接，7 处
        ``with self._get_connection()`` 每次调用泄漏文件描述符，累积到进程
        上限后报 "unable to open database file"。2026-09-12 修复。
        """
        conn = sqlite3.connect(self.db_path, timeout=DB_CONNECT_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            pass
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def emit(self, event: NotificationEvent) -> int:
        config = dict(self.config or {})
        if not is_notification_event_enabled(config, event.event_type):
            return 0

        now_text = _current_ts()
        payload_json = json.dumps(event.as_payload(), ensure_ascii=False)
        created = 0

        with self._get_connection() as conn:
            for channel_id in iter_enabled_channel_ids(config):
                notifier = self._registry.get(channel_id)
                if not notifier:
                    continue
                missing_fields = notifier.validate_config(config)
                if missing_fields:
                    logger.warning("通知渠道 %s 已启用但配置不完整，跳过事件 %s", channel_id, event.event_type)
                    continue
                conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, channel, event_type, payload_json, status, attempts,
                        next_retry_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        channel_id,
                        event.event_type,
                        payload_json,
                        OUTBOX_STATUS_PENDING,
                        0,
                        now_text,
                        None,
                        now_text,
                        now_text,
                    ),
                )
                created += 1
            conn.commit()

        if created:
            self._wake_event.set()
        return created

    def send_test_message(self, channel_id: str) -> None:
        config = dict(self.config or {})
        notifier = self._registry.get(channel_id)
        if not notifier:
            raise ValueError(f"不支持的通知渠道: {channel_id}")

        missing_fields = notifier.validate_config(config)
        if missing_fields:
            readable = "、".join(missing_fields)
            raise ValueError(f"{CHANNEL_LABELS.get(channel_id, channel_id)} 配置不完整：缺少 {readable}")

        now_text = _current_ts()
        channel_label = CHANNEL_LABELS.get(channel_id, channel_id)
        message = NotificationMessage(
            title=f"Y2A-Auto 🔔 测试消息",
            summary=f"{channel_label} 测试发送成功触发",
            markdown="\n".join(
                (
                    "**🔔 Y2A-Auto 测试消息**",
                    "",
                    f"> **推送渠道：**{channel_label}",
                    f"> **发送时间：**{now_text}",
                    f"> **说明：**如果你收到这条消息，说明当前渠道配置可用。",
                )
            ),
        )
        notifier.send(message, config)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    def _start_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="notification-outbox-worker",
            daemon=True,
        )
        self._worker_thread.start()
        self._wake_event.set()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self._process_due_messages()
            if processed > 0:
                continue

            wait_seconds = self._seconds_until_next_due()
            self._wake_event.wait(timeout=wait_seconds)
            self._wake_event.clear()

    def _process_due_messages(self) -> int:
        processed = 0
        while not self._stop_event.is_set():
            row = self._claim_next_due_row()
            if row is None:
                break
            processed += 1
            self._deliver_row(row)
        return processed

    def _seconds_until_next_due(self) -> float:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT next_retry_at
                FROM notification_outbox
                WHERE status = ?
                ORDER BY COALESCE(next_retry_at, created_at) ASC
                LIMIT 1
                """,
                (OUTBOX_STATUS_PENDING,),
            ).fetchone()
        if not row or not row["next_retry_at"]:
            return 30.0
        try:
            next_due = datetime.strptime(str(row["next_retry_at"]), "%Y-%m-%d %H:%M:%S")
            delay = (next_due - datetime.now()).total_seconds()
        except Exception:
            delay = 0.0
        return max(1.0, min(30.0, delay))

    def _claim_next_due_row(self):
        """领取一条到期通知；顺手淘汰滞留过久的行，避免恢复后成批补发旧告警。"""
        now_text = _current_ts()
        with self._db_lock:
            # 每轮最多淘汰 50 条过期行，防止极端积压时长时间占锁。
            for _ in range(50):
                with self._get_connection() as conn:
                    row = conn.execute(
                        """
                        SELECT *
                        FROM notification_outbox
                        WHERE status = ?
                          AND (next_retry_at IS NULL OR next_retry_at <= ?)
                        ORDER BY COALESCE(next_retry_at, created_at) ASC, created_at ASC
                        LIMIT 1
                        """,
                        (OUTBOX_STATUS_PENDING, now_text),
                    ).fetchone()
                    if row is None:
                        return None

                    if self._is_stale(row):
                        conn.execute(
                            """
                            UPDATE notification_outbox
                            SET status = ?, last_error = ?, updated_at = ?
                            WHERE id = ? AND status = ?
                            """,
                            (
                                OUTBOX_STATUS_EXPIRED,
                                f"队列滞留超过 {self.max_queue_age_seconds} 秒，不再补发过期告警",
                                _current_ts(),
                                row["id"],
                                OUTBOX_STATUS_PENDING,
                            ),
                        )
                        conn.commit()
                        continue

                    updated_at = _current_ts()
                    cursor = conn.execute(
                        """
                        UPDATE notification_outbox
                        SET status = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (OUTBOX_STATUS_SENDING, updated_at, row["id"], OUTBOX_STATUS_PENDING),
                    )
                    conn.commit()
                    if cursor.rowcount != 1:
                        return None
                    return dict(row)
        return None

    def _is_stale(self, row: Any) -> bool:
        """队列中滞留过久的通知不再补发（0 表示不设上限）。"""
        if self.max_queue_age_seconds <= 0:
            return False
        try:
            created_text = str(row["created_at"] or "")
        except (KeyError, IndexError, TypeError):
            return False
        created_at = _parse_ts(created_text)
        if created_at is None:
            return False
        return (datetime.now() - created_at).total_seconds() > self.max_queue_age_seconds

    def _deliver_row(self, row: dict[str, Any]) -> None:
        channel_id = str(row.get("channel") or "").strip()
        event_type = str(row.get("event_type") or "").strip()
        notifier = self._registry.get(channel_id)
        if not notifier:
            self._mark_failed(row["id"], int(row.get("attempts") or 0) + 1, f"未知通知渠道: {channel_id}")
            return

        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except Exception as exc:
            self._mark_failed(row["id"], int(row.get("attempts") or 0) + 1, f"通知载荷解析失败: {exc}")
            return

        event = NotificationEvent(event_type=event_type, payload=payload, occurred_at=str(payload.get("occurred_at") or _current_ts()))
        message = build_notification_message(event)
        next_attempt = int(row.get("attempts") or 0) + 1

        try:
            notifier.send(message, dict(self.config or {}))
            self._mark_sent(row["id"], next_attempt)
        except Exception as exc:
            error_text = str(exc)
            if next_attempt >= self.max_attempts:
                self._mark_failed(row["id"], next_attempt, error_text)
            else:
                retry_seconds = self.retry_schedule_seconds[next_attempt - 1]
                self._mark_retry(row["id"], next_attempt, retry_seconds, error_text)

    def _mark_sent(self, row_id: str, attempts: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_SENT, attempts, _current_ts(), row_id),
            )
            conn.commit()

    def _mark_retry(self, row_id: str, attempts: int, retry_seconds: int, last_error: str) -> None:
        next_retry_at = datetime.fromtimestamp(time.time() + retry_seconds).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_PENDING, attempts, next_retry_at, last_error, _current_ts(), row_id),
            )
            conn.commit()
        self._wake_event.set()

    def _mark_failed(self, row_id: str, attempts: int, last_error: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_FAILED, attempts, last_error, _current_ts(), row_id),
            )
            conn.commit()


def get_global_notification_service(config: dict[str, Any] | None = None) -> NotificationService:
    global _global_notification_service
    with _global_notification_lock:
        if _global_notification_service is None:
            if config is None:
                from ..config_manager import load_config
                config = load_config()
            _global_notification_service = NotificationService(config=config)
        elif config is not None:
            _global_notification_service.refresh_config(config)
        return _global_notification_service


def shutdown_global_notification_service() -> None:
    global _global_notification_service
    with _global_notification_lock:
        if _global_notification_service is not None:
            _global_notification_service.shutdown()
            _global_notification_service = None


def emit_notification_event(event: NotificationEvent) -> int:
    try:
        service = get_global_notification_service()
        return service.emit(event)
    except Exception as exc:
        logger.warning("写入通知队列失败: %s", exc)
        return 0
