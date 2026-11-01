"""Local, restart-safe discovery -> production queue. This worker never publishes.

Counters are observations, not promises of popularity or monetization. A candidate
needs two comparable observations before unattended selection; missing counters do
not become zero. Remote publishing remains in the existing reviewed workflow.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULTS = {
    "enabled": False,
    "auto_render": False,
    "daily_limit": 2,
    "interval_minutes": 60,
    "sources": ["bilibili"],
    "targets": ["bilibili", "youtube"],
    "topics": "",
    "excluded_topics": "",
    "minimum_growth_per_hour": 100,
    "max_age_hours": 48,
}
LOCALES = {
    "bilibili": "zh-CN",
    "douyin": "zh-CN",
    "youtube": "en-US",
    "tiktok": "en-US",
    "x": "en-US",
}
SOURCE_PLATFORMS = frozenset({"bilibili", "youtube", "douyin"})
REMOTE_OR_REVIEW_STATES = frozenset(
    {
        "publishing",
        "submitted",
        "processing",
        "public",
        "unknown",
        "completed",
        "rejected",
        "restricted",
    }
)
ACTIVE_QUEUE_STATES = frozenset({"claimed", "preparing", "drafting", "rendering"})
TERMINAL_QUEUE_STATES = frozenset(
    {"review", "blocked", "interrupted", "skipped", "duplicate"}
)
RESERVATION_TTL_MINUTES = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def file_fingerprint(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_day(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone().date().isoformat()


def _platform_list(value: Any, allowed: set[str] | frozenset[str]) -> list[str]:
    if isinstance(value, str):
        source = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        source = value
    else:
        source = []
    return list(
        dict.fromkeys(
            normalized
            for item in source
            if (normalized := str(item or "").strip().lower()) in allowed
        )
    )


class QueueSkip(RuntimeError):
    """A safe business-state stop, distinct from a production failure."""


class ProductionAutomation:
    def __init__(self, center):
        self.center = center
        self._lock = threading.Lock()
        self._process_lock_path = f"{Path(center.db_path).resolve()}.production.lock"
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS production_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL,
                    last_refresh_at TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS candidate_observations (
                    candidate_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    views INTEGER, likes INTEGER,
                    PRIMARY KEY(candidate_id, observed_at)
                );
                CREATE INDEX IF NOT EXISTS production_observation_time
                    ON candidate_observations(candidate_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS production_queue (
                    job_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'queued',
                    fingerprint TEXT DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS production_fingerprint
                    ON production_queue(fingerprint);

                CREATE TABLE IF NOT EXISTS production_asset_claims (
                    fingerprint TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    released_at TEXT NOT NULL DEFAULT '',
                    release_reason TEXT NOT NULL DEFAULT '',
                    CHECK(state IN ('active', 'released'))
                );

                CREATE TABLE IF NOT EXISTS production_asset_claim_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    CHECK(event IN ('claimed', 'released'))
                );

                CREATE TABLE IF NOT EXISTS production_quota_reservations (
                    candidate_id TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL UNIQUE,
                    day_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    job_id TEXT NOT NULL DEFAULT '',
                    reserved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(state IN ('reserved', 'committed', 'released'))
                );
                CREATE INDEX IF NOT EXISTS production_quota_day
                    ON production_quota_reservations(day_key, state);

                CREATE TABLE IF NOT EXISTS production_source_refresh (
                    platform TEXT PRIMARY KEY,
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._migrate_asset_claims(db)
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS production_asset_claim_job
                ON production_asset_claims(job_id, state)
                """
            )
            self._ensure_column(
                db, "production_queue", "origin", "TEXT NOT NULL DEFAULT 'manual'"
            )
            self._ensure_column(
                db, "production_queue", "claim_token", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                db, "production_queue", "claimed_at", "TEXT NOT NULL DEFAULT ''"
            )
            db.execute(
                "INSERT OR IGNORE INTO production_settings(id,payload) VALUES(1,?)",
                (json.dumps(DEFAULTS),),
            )

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_asset_claims(db: sqlite3.Connection) -> None:
        """Remove the legacy UNIQUE(job_id) without discarding claim history."""

        unique_job_index = False
        for index in db.execute(
            "PRAGMA index_list(production_asset_claims)"
        ).fetchall():
            if not int(index[2]):
                continue
            columns = [
                str(row[2])
                for row in db.execute(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            ]
            if columns == ["job_id"]:
                unique_job_index = True
                break
        if not unique_job_index:
            return
        db.executescript(
            """
            ALTER TABLE production_asset_claims
                RENAME TO production_asset_claims_legacy;
            CREATE TABLE production_asset_claims (
                fingerprint TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                released_at TEXT NOT NULL DEFAULT '',
                release_reason TEXT NOT NULL DEFAULT '',
                CHECK(state IN ('active', 'released'))
            );
            INSERT INTO production_asset_claims(
                fingerprint, job_id, claimed_at, state
            )
            SELECT fingerprint, job_id, claimed_at, 'active'
            FROM production_asset_claims_legacy;
            DROP TABLE production_asset_claims_legacy;
            CREATE INDEX IF NOT EXISTS production_asset_claim_job
                ON production_asset_claims(job_id, state);
            """
        )

    def _raw_connect(self) -> sqlite3.Connection:
        """裸连接：调用方必须自行 close()。"""
        db = sqlite3.connect(self.center.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """退出时提交/回滚，并**始终关闭**连接。

        注意：sqlite3.Connection 直接用于 ``with`` 时只处理事务、**不关闭连接**
        （Python 官方语义）。历史实现返回裸连接，导致每次 tick 泄漏 3 个 fd
        （.db + -wal + -shm），累积到系统上限 256 后报
        "unable to open database file"。2026-09-12 修复。
        """
        db = self._raw_connect()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _cross_process_lock(self):
        """OS lock released automatically if the worker process crashes."""

        import fcntl

        Path(self._process_lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._process_lock_path, "a+b")
        acquired = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def settings(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM production_settings WHERE id=1"
            ).fetchone()
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return {
            **DEFAULTS,
            **payload,
            "last_refresh_at": row["last_refresh_at"],
            "last_message": row["last_message"],
        }

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        data = {key: values.get(key, default) for key, default in DEFAULTS.items()}
        for key in ("enabled", "auto_render"):
            data[key] = str(data[key]).lower() in {"1", "true", "on"}
        for key, low, high in (
            ("daily_limit", 1, 10),
            ("interval_minutes", 15, 1440),
            ("minimum_growth_per_hour", 1, 10_000_000),
            ("max_age_hours", 6, 168),
        ):
            data[key] = max(low, min(high, int(data[key])))
        data["sources"] = _platform_list(data["sources"], SOURCE_PLATFORMS)
        data["targets"] = _platform_list(data["targets"], frozenset(LOCALES))
        if not data["sources"] or not data["targets"]:
            raise ValueError("至少选择一个来源和一个目标平台")
        for key in ("topics", "excluded_topics"):
            data[key] = str(data[key] or "").strip()[:1000]
        if data["enabled"] and not data["topics"]:
            raise ValueError("请先填写内容方向，例如 科技,AI工具，再开启自动选题")
        with self.connect() as db:
            db.execute(
                "UPDATE production_settings SET payload=? WHERE id=1",
                (json.dumps(data, ensure_ascii=False),),
            )
        return data

    def observe(
        self,
        candidate_id: str,
        metrics: dict[str, Any],
        observed_at: datetime | None = None,
    ) -> None:
        def counter(key: str) -> int | None:
            value = metrics.get(key)
            try:
                return max(0, int(value)) if value is not None else None
            except (TypeError, ValueError):
                return None

        stamp = _iso(observed_at)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO candidate_observations VALUES(?,?,?,?)",
                (str(candidate_id), stamp, counter("views"), counter("likes")),
            )
            db.execute(
                "DELETE FROM candidate_observations WHERE observed_at<?",
                (_iso(utcnow() - timedelta(days=14)),),
            )

    def rank(
        self, candidates: list[dict[str, Any]], now: datetime | None = None
    ) -> list[dict[str, Any]]:
        now = (now or utcnow()).astimezone(timezone.utc)
        settings = self.settings()

        def terms(text: Any) -> list[str]:
            return [
                item.strip().casefold()
                for item in str(text).replace("，", ",").split(",")
                if item.strip()
            ]

        includes = terms(settings["topics"])
        excludes = terms(settings["excluded_topics"])
        result: list[dict[str, Any]] = []
        with self.connect() as db:
            for item in candidates:
                candidate = dict(item)
                text = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
                raw_rows = db.execute(
                    """
                    SELECT * FROM candidate_observations
                    WHERE candidate_id=?
                    """,
                    (item["id"],),
                ).fetchall()
                rows = sorted(
                    (
                        (parsed, row)
                        for row in raw_rows
                        if (parsed := parse_time(row["observed_at"])) is not None
                    ),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                score = 0.0
                growth: float | None = None
                engagement: float | None = None
                reason = "等待两次有效热度采样"
                latest = rows[0] if rows else None
                previous = next(
                    (
                        pair
                        for pair in rows[1:]
                        if latest and (latest[0] - pair[0]).total_seconds() >= 900
                    ),
                    None,
                )
                metrics = (
                    item.get("metrics")
                    if isinstance(item.get("metrics"), dict)
                    else {}
                )
                updated = parse_time(item.get("updated_at"))
                published = parse_time(metrics.get("published_at")) or parse_time(
                    item.get("discovered_at")
                )
                if (
                    latest
                    and previous
                    and latest[1]["views"] is not None
                    and previous[1]["views"] is not None
                ):
                    hours = (latest[0] - previous[0]).total_seconds() / 3600
                    delta = int(latest[1]["views"]) - int(previous[1]["views"])
                    if delta >= 0 and hours > 0:
                        growth = round(delta / hours, 2)
                        score = math.log1p(growth) * 10
                        if (
                            latest[1]["likes"] is not None
                            and int(latest[1]["views"]) > 0
                        ):
                            engagement = min(
                                1.0,
                                int(latest[1]["likes"])
                                / int(latest[1]["views"]),
                            )
                            score += engagement * 20
                        score = round(score, 2)
                        reason = (
                            "达到增长门槛"
                            if growth >= settings["minimum_growth_per_hour"]
                            else "增长不足，继续观察"
                        )
                    else:
                        reason = "平台计数回退，需重新采样"
                eligible = (
                    growth is not None
                    and growth >= settings["minimum_growth_per_hour"]
                )
                for failed, message in (
                    (
                        not updated
                        or (now - updated).total_seconds()
                        > settings["interval_minutes"] * 180,
                        "采样已过期",
                    ),
                    (
                        not published
                        or (now - published).total_seconds()
                        > settings["max_age_hours"] * 3600,
                        "超出选题时效",
                    ),
                    (
                        bool(includes)
                        and not any(term in text for term in includes),
                        "不符合内容方向",
                    ),
                    (any(term in text for term in excludes), "命中排除词"),
                    (
                        item.get("platform") not in settings["sources"],
                        "未选择此来源",
                    ),
                ):
                    if failed:
                        eligible, reason = False, message
                candidate.update(
                    growth_per_hour=growth,
                    engagement_rate=engagement,
                    opportunity_score=score,
                    eligible=eligible,
                    selection_reason=reason,
                )
                result.append(candidate)
        return sorted(
            result,
            key=lambda item: (item["eligible"], item["opportunity_score"]),
            reverse=True,
        )

    def list_queue(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT q.*, j.title, j.recreation_completed
                    FROM production_queue q
                    LEFT JOIN transfer_jobs j ON j.id=q.job_id
                    ORDER BY q.created_at DESC LIMIT ?
                    """,
                    (max(1, min(1000, int(limit))),),
                )
            ]

    @staticmethod
    def _job_block_reason(job: dict[str, Any] | None) -> str:
        if not job:
            return "任务已不存在"
        if job.get("reviewed_at") or str(job.get("recreation_status") or "") == "approved":
            return "任务已经人工确认"
        if any(
            job.get(key)
            for key in (
                "youtube_video_id",
                "bilibili_post_id",
                "douyin_post_id",
                "x_post_id",
                "tiktok_post_id",
            )
        ):
            return "任务已经存在平台发布回执"
        if str(job.get("status") or "").lower() in {
            "publishing",
            "submitted",
            "completed",
            "skipped",
            "cancelled",
        }:
            return "任务状态已不允许自动制作"
        for key in (
            "youtube_publish_status",
            "bilibili_publish_status",
            "douyin_publish_status",
            "x_publish_status",
            "tiktok_publish_status",
        ):
            if str(job.get(key) or "").lower() in REMOTE_OR_REVIEW_STATES:
                return "任务可能已经提交平台，禁止重复制作"
        return ""

    @staticmethod
    def _has_recreated_output(job: dict[str, Any] | None) -> bool:
        if not job or not int(job.get("recreation_completed") or 0):
            return False
        path = str(job.get("local_video_path") or "")
        return bool(path and Path(path).is_file())

    @staticmethod
    def _has_existing_render_job(job: dict[str, Any] | None) -> bool:
        if not job or not str(job.get("mpt_project_id") or "").strip():
            return False
        return bool(
            str(job.get("mpt_render_id") or "").strip()
            or str(job.get("mpt_status") or "").lower()
            in {"queued", "rendering", "processing", "ready"}
        )

    @staticmethod
    def _has_resumable_locale_delivery(job: dict[str, Any] | None) -> bool:
        """Recognize a crash-safe finished locale without claiming global completion."""

        if not job:
            return False
        try:
            deliveries = json.loads(str(job.get("locale_deliveries_json") or "{}"))
        except (TypeError, ValueError):
            return False
        if not isinstance(deliveries, dict):
            return False
        return any(
            isinstance(delivery, dict)
            and str(delivery.get("status") or "") == "ready"
            and bool(str(delivery.get("artifact_fingerprint") or "").strip())
            and bool(str(delivery.get("path") or "").strip())
            and Path(str(delivery["path"])).is_file()
            for delivery in deliveries.values()
        )

    def _sync_existing_render(
        self, job_id: str, job: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Try read-only reconciliation/download before permitting another render."""

        sync = getattr(self.center, "sync_money_printer_render", None)
        if not callable(sync):
            return job, "已有本地制作任务，但当前制作端不支持自动同步"
        try:
            sync(job_id)
        except Exception as exc:
            refreshed = self.center.get_job(job_id) or job
            return (
                refreshed,
                f"已有本地渲染任务，暂未同步（{type(exc).__name__}）；"
                "为避免重复渲染，已停止自动重做",
            )
        refreshed = self.center.get_job(job_id) or job
        if self._has_recreated_output(refreshed):
            return refreshed, ""
        if self._has_resumable_locale_delivery(refreshed):
            return refreshed, ""
        return refreshed, "已有本地渲染任务，但尚未取回可验证的成片"

    def _stop_for_existing_render(
        self,
        job_id: str,
        job: dict[str, Any],
        *,
        claim_token: str = "",
    ) -> bool:
        if self._has_recreated_output(job):
            self._state(
                job_id,
                "review",
                "检测到已有再创作成片，请试听并确认发布",
                claim_token=claim_token,
            )
            return True
        if not self._has_existing_render_job(job):
            return False
        refreshed, message = self._sync_existing_render(job_id, job)
        if self._has_recreated_output(refreshed):
            self._state(
                job_id,
                "review",
                "已取回中断前生成的成片，请试听并确认发布",
                claim_token=claim_token,
            )
        elif self._has_resumable_locale_delivery(refreshed):
            return False
        else:
            self._state(
                job_id,
                "interrupted",
                message,
                claim_token=claim_token,
            )
        return True

    def enqueue(
        self, job_id: str, candidate_id: str = "", *, origin: str = "manual"
    ) -> bool:
        job = self.center.get_job(job_id)
        reason = self._job_block_reason(job)
        if reason:
            raise ValueError(f"{reason}，不能进入自动制作")
        try:
            targets = json.loads(job["target_platforms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("任务目标平台配置无效") from exc
        if not isinstance(targets, list):
            raise ValueError("任务目标平台配置无效")
        stamp = _iso()
        normalized_origin = "scheduled" if origin == "scheduled" else "manual"
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO production_queue(
                    job_id, candidate_id, state, origin, created_at, updated_at
                ) VALUES(?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, candidate_id, normalized_origin, stamp, stamp),
            )
        self.center._update_job(
            job_id,
            target_locale_map_json=json.dumps(
                {
                    platform: LOCALES[platform]
                    for platform in targets
                    if platform in LOCALES
                }
            ),
        )
        return bool(cursor.rowcount)

    def retry(self, job_id: str) -> bool:
        job = self.center.get_job(job_id)
        reason = self._job_block_reason(job)
        if reason:
            self._state(job_id, "skipped", reason)
            return False
        if self._stop_for_existing_render(job_id, job):
            return False
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE production_queue
                SET state='queued', message='', claim_token='', claimed_at='',
                    updated_at=?
                WHERE job_id=? AND state IN ('blocked','interrupted','review')
                """,
                (_iso(), job_id),
            )
        return bool(cursor.rowcount)

    def recover(self) -> int:
        """Recover local work without repeating an already rendered output."""
        with self._cross_process_lock() as acquired:
            if not acquired:
                return 0
            self._recover_quota_reservations()
            with self.connect() as db:
                rows = db.execute(
                    """
                    SELECT job_id,state FROM production_queue
                    WHERE state IN ('claimed','preparing','drafting','rendering')
                    """
                ).fetchall()
            recovered = 0
            auto_render = bool(self.settings().get("auto_render"))
            for row in rows:
                job_id = str(row["job_id"])
                job = self.center.get_job(job_id)
                if (
                    row["state"] == "rendering"
                    and self._has_existing_render_job(job)
                ):
                    job, sync_message = self._sync_existing_render(job_id, job)
                else:
                    sync_message = ""
                if self._has_recreated_output(job):
                    state = "review"
                    message = "重启后已取回或检测到再创作成片，请试听并确认发布"
                elif self._has_resumable_locale_delivery(job):
                    state = "queued" if auto_render else "interrupted"
                    message = (
                        "已取回中断前完成的语言成片，将续做剩余语言"
                        if auto_render
                        else "已取回部分语言成片；启用本地成片后可继续"
                    )
                else:
                    state = "interrupted"
                    message = sync_message or (
                        "重启前制作未完成，请核对制作端后再手动重试"
                    )
                self._state(job_id, state, message)
                recovered += 1
            return recovered

    def _state(
        self,
        job_id: str,
        state: str,
        message: str = "",
        *,
        claim_token: str = "",
    ) -> bool:
        clear_claim = state in TERMINAL_QUEUE_STATES or state == "queued"
        assignments = (
            "state=?, message=?, updated_at=?, claim_token='', claimed_at=''"
            if clear_claim
            else "state=?, message=?, updated_at=?"
        )
        where = "job_id=?"
        params: list[Any] = [state, message[:500], _iso(), job_id]
        if claim_token:
            where += " AND claim_token=?"
            params.append(claim_token)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE production_queue SET {assignments} WHERE {where}", params
            )
        return bool(cursor.rowcount)

    def _claim_oldest_queue(self) -> dict[str, Any] | None:
        token = str(uuid.uuid4())
        stamp = _iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM production_queue
                WHERE state='queued'
                ORDER BY created_at ASC, job_id ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                db.commit()
                return None
            cursor = db.execute(
                """
                UPDATE production_queue
                SET state='claimed', claim_token=?, claimed_at=?, updated_at=?
                WHERE job_id=? AND state='queued'
                """,
                (token, stamp, stamp, row["job_id"]),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute(
                "SELECT * FROM production_queue WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            db.commit()
        return dict(claimed)

    def _return_claim(self, job_id: str, claim_token: str) -> None:
        self._state(job_id, "queued", "", claim_token=claim_token)

    def _claim_asset(self, job_id: str, fingerprint: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                """
                SELECT job_id,state FROM production_asset_claims
                WHERE fingerprint=?
                """,
                (fingerprint,),
            ).fetchone()
            stamp = _iso()
            claimed = False
            if owner is None:
                db.execute(
                    """
                    INSERT INTO production_asset_claims(
                        fingerprint, job_id, claimed_at, state
                    ) VALUES (?, ?, ?, 'active')
                    """,
                    (fingerprint, job_id, stamp),
                )
                claimed = True
            elif str(owner["job_id"]) == str(job_id) and owner["state"] == "active":
                claimed = True
            elif owner["state"] == "released":
                db.execute(
                    """
                    UPDATE production_asset_claims
                    SET job_id=?, claimed_at=?, state='active',
                        released_at='', release_reason=''
                    WHERE fingerprint=? AND state='released'
                    """,
                    (job_id, stamp, fingerprint),
                )
                claimed = True
            if claimed and not (
                owner
                and str(owner["job_id"]) == str(job_id)
                and owner["state"] == "active"
            ):
                db.execute(
                    """
                    INSERT INTO production_asset_claim_events(
                        fingerprint,job_id,event,created_at
                    ) VALUES(?,?,'claimed',?)
                    """,
                    (fingerprint, job_id, stamp),
                )
            db.commit()
        return claimed

    def release_asset_claim(
        self, job_id: str, fingerprint: str, reason: str
    ) -> bool:
        """Release a stale claim explicitly while retaining an audit event."""

        note = str(reason or "").strip()
        if len(note) < 8:
            raise ValueError("请记录释放去重指纹的具体原因")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """
                UPDATE production_asset_claims
                SET state='released', released_at=?, release_reason=?
                WHERE fingerprint=? AND job_id=? AND state='active'
                """,
                (_iso(), note[:500], fingerprint, job_id),
            )
            if cursor.rowcount:
                db.execute(
                    """
                    INSERT INTO production_asset_claim_events(
                        fingerprint,job_id,event,reason,created_at
                    ) VALUES(?,?,'released',?,?)
                    """,
                    (fingerprint, job_id, note[:500], _iso()),
                )
            db.commit()
        return bool(cursor.rowcount)

    def _reserve_candidate(self, candidate_id: str, daily_limit: int) -> str:
        now = utcnow()
        stamp = _iso(now)
        expires = _iso(now + timedelta(minutes=RESERVATION_TTL_MINUTES))
        day_key = _local_day(now)
        reservation_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE production_quota_reservations
                SET state='released', updated_at=?
                WHERE state='reserved' AND expires_at<=? AND job_id=''
                """,
                (stamp, stamp),
            )
            existing = db.execute(
                "SELECT * FROM production_quota_reservations WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if existing and str(existing["state"]) in {"reserved", "committed"}:
                db.commit()
                return ""
            used = db.execute(
                """
                SELECT COUNT(*) FROM production_quota_reservations
                WHERE day_key=? AND state IN ('reserved','committed')
                """,
                (day_key,),
            ).fetchone()[0]
            if int(used) >= int(daily_limit):
                db.commit()
                return ""
            db.execute(
                """
                INSERT INTO production_quota_reservations(
                    candidate_id, reservation_id, day_key, state, job_id,
                    reserved_at, expires_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', '', ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    reservation_id=excluded.reservation_id,
                    day_key=excluded.day_key,
                    state='reserved', job_id='',
                    reserved_at=excluded.reserved_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id,
                    reservation_id,
                    day_key,
                    stamp,
                    expires,
                    stamp,
                ),
            )
            db.commit()
        return reservation_id

    def _finish_reservation(
        self, candidate_id: str, reservation_id: str, job_id: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE production_quota_reservations
                SET state='committed', job_id=?, updated_at=?
                WHERE candidate_id=? AND reservation_id=? AND state='reserved'
                """,
                (job_id, _iso(), candidate_id, reservation_id),
            )

    def _record_reservation_job(
        self, candidate_id: str, reservation_id: str, job_id: str
    ) -> bool:
        """Persist promotion output before queue attachment can be interrupted."""

        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE production_quota_reservations
                SET job_id=?, updated_at=?
                WHERE candidate_id=? AND reservation_id=? AND state='reserved'
                """,
                (job_id, _iso(), candidate_id, reservation_id),
            )
        return bool(cursor.rowcount)

    def _release_reservation(self, candidate_id: str, reservation_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE production_quota_reservations
                SET state='released', updated_at=?
                WHERE candidate_id=? AND reservation_id=? AND state='reserved'
                """,
                (_iso(), candidate_id, reservation_id),
            )

    def _recover_quota_reservations(self) -> int:
        """Finish a promotion that committed just before a process interruption."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT reservation.*, candidate.job_id AS promoted_job_id
                FROM production_quota_reservations reservation
                LEFT JOIN transfer_candidates candidate
                  ON candidate.id=reservation.candidate_id
                WHERE reservation.state='reserved'
                ORDER BY reservation.reserved_at ASC
                """
            ).fetchall()
        recovered = 0
        now_text = _iso()
        for row in rows:
            candidate_id = str(row["candidate_id"])
            reservation_id = str(row["reservation_id"])
            job_id = str(row["promoted_job_id"] or row["job_id"] or "")
            if job_id:
                try:
                    self.enqueue(job_id, candidate_id, origin="scheduled")
                except ValueError:
                    # Promotion already created a job. It consumed today's slot
                    # even if a later review/publish action made enqueue obsolete.
                    pass
                except Exception:
                    # Keep the durable reservation attached to this job. A later
                    # tick can retry without promoting a second transfer job.
                    continue
                self._finish_reservation(candidate_id, reservation_id, job_id)
                recovered += 1
                continue
            if str(row["expires_at"] or "") <= now_text:
                self._release_reservation(candidate_id, reservation_id)
        return recovered

    def _refresh_sources(self, settings: dict[str, Any]) -> None:
        now = utcnow()
        messages: list[str] = []
        successful_at: list[str] = []
        for source in settings["sources"]:
            with self.connect() as db:
                state = db.execute(
                    "SELECT * FROM production_source_refresh WHERE platform=?",
                    (source,),
                ).fetchone()
            last_success = parse_time(state["last_success_at"]) if state else None
            if (
                last_success
                and (now - last_success).total_seconds()
                < int(settings["interval_minutes"]) * 60
            ):
                continue
            attempted_at = _iso(now)
            try:
                result = self.center.refresh_hot_candidates(source, limit=30)
                message = str(result.get("message") or f"{source}候选已刷新")
                if result.get("success", True):
                    error = ""
                    success_at = attempted_at
                    successful_at.append(success_at)
                else:
                    error = str(result.get("errors") or message)[:500]
                    success_at = (
                        str(state["last_success_at"] or "") if state else ""
                    )
            except Exception as exc:
                message = f"{source}刷新失败：{type(exc).__name__}"
                error = str(exc)[:500]
                success_at = str(state["last_success_at"] or "") if state else ""
            messages.append(message)
            with self.connect() as db:
                db.execute(
                    """
                    INSERT INTO production_source_refresh(
                        platform, last_success_at, last_attempt_at, last_error
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(platform) DO UPDATE SET
                        last_success_at=excluded.last_success_at,
                        last_attempt_at=excluded.last_attempt_at,
                        last_error=excluded.last_error
                    """,
                    (source, success_at, attempted_at, error),
                )
        if messages:
            with self.connect() as db:
                if successful_at:
                    db.execute(
                        """
                        UPDATE production_settings
                        SET last_refresh_at=?, last_message=? WHERE id=1
                        """,
                        (max(successful_at), "；".join(messages)[:1500]),
                    )
                else:
                    db.execute(
                        "UPDATE production_settings SET last_message=? WHERE id=1",
                        ("；".join(messages)[:1500],),
                    )

    def _select_scheduled_candidates(self, settings: dict[str, Any]) -> None:
        for candidate in self.rank(self.center.list_hot_candidates(100)):
            if not candidate["eligible"]:
                continue
            targets = [
                platform
                for platform in settings["targets"]
                if platform != candidate["platform"]
            ]
            if not targets:
                continue
            reservation_id = self._reserve_candidate(
                str(candidate["id"]), int(settings["daily_limit"])
            )
            if not reservation_id:
                continue
            job_id = ""
            try:
                job_id = self.center.promote_hot_candidate(candidate["id"], targets)
                if not job_id:
                    raise ValueError("候选提升未返回任务 ID")
                self._record_reservation_job(
                    str(candidate["id"]), reservation_id, job_id
                )
                self.enqueue(job_id, candidate["id"], origin="scheduled")
                self._finish_reservation(
                    str(candidate["id"]), reservation_id, job_id
                )
            except ValueError:
                if job_id:
                    # The promotion already created a task, while current task
                    # state deliberately refuses automation. Count the slot and
                    # do not create another task for this candidate.
                    self._finish_reservation(
                        str(candidate["id"]), reservation_id, job_id
                    )
                else:
                    self._release_reservation(
                        str(candidate["id"]), reservation_id
                    )
            except Exception:
                if not job_id:
                    self._release_reservation(
                        str(candidate["id"]), reservation_id
                    )
                # With a promoted job, leave the reservation in `reserved`.
                # Recovery retries queue attachment and cannot promote twice.

    def tick(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            with self._cross_process_lock() as acquired:
                if not acquired:
                    return
                self._tick_locked()
        finally:
            self._lock.release()

    def _tick_locked(self) -> None:
        self._recover_quota_reservations()
        settings = self.settings()
        if settings["enabled"]:
            self._refresh_sources(settings)
            self._select_scheduled_candidates(settings)

        queue = self._claim_oldest_queue()
        if not queue:
            return
        job_id = str(queue["job_id"])
        claim_token = str(queue["claim_token"])
        if not self.center._claim_active_job(job_id):
            self._return_claim(job_id, claim_token)
            return
        try:
            job = self.center.get_job(job_id)
            reason = self._job_block_reason(job)
            if reason:
                raise QueueSkip(reason)
            if self._stop_for_existing_render(
                job_id, job, claim_token=claim_token
            ):
                return
            job = self.center.get_job(job_id) or job
            self.center._assert_runtime_capacity("自动制作")
            if not job.get("local_video_path") or not Path(
                job["local_video_path"]
            ).is_file():
                self._state(
                    job_id,
                    "preparing",
                    "下载原片并提取字幕",
                    claim_token=claim_token,
                )
                self.center.prepare_job(job_id)
                job = self.center.get_job(job_id)
            reason = self._job_block_reason(job)
            if reason:
                raise QueueSkip(reason)
            if self._stop_for_existing_render(
                job_id, job, claim_token=claim_token
            ):
                return
            job = self.center.get_job(job_id) or job
            source = job.get("original_video_path") or job.get("local_video_path")
            if not source or not Path(source).is_file():
                raise ValueError("原片下载未完成，请检查任务详情")
            fingerprint = file_fingerprint(source)
            if not self._claim_asset(job_id, fingerprint):
                self._state(
                    job_id,
                    "duplicate",
                    "发现相同原片，已阻止跨来源重复制作",
                    claim_token=claim_token,
                )
                return
            with self.connect() as db:
                db.execute(
                    """
                    UPDATE production_queue SET fingerprint=?
                    WHERE job_id=? AND claim_token=?
                    """,
                    (fingerprint, job_id, claim_token),
                )
            resuming_locales = self._has_resumable_locale_delivery(job)
            if not resuming_locales:
                self._state(
                    job_id,
                    "drafting",
                    "生成新的解说和镜头编排",
                    claim_token=claim_token,
                )
                self.center.generate_recreation_draft(job_id)
                job = self.center.get_job(job_id)
                reason = self._job_block_reason(job)
                if reason:
                    raise QueueSkip(reason)
            if settings["auto_render"]:
                if self._has_recreated_output(job):
                    self._state(
                        job_id,
                        "review",
                        "检测到已有再创作成片，请试听并确认发布",
                        claim_token=claim_token,
                    )
                    return
                self._state(
                    job_id,
                    "rendering",
                    (
                        "续做剩余语言的本地成片"
                        if resuming_locales
                        else "本地配音、字幕和分平台成片"
                    ),
                    claim_token=claim_token,
                )
                self.center._run_money_printer_recreation(job_id)
                job = self.center.get_job(job_id)
                if not self._has_recreated_output(job):
                    raise ValueError("制作端未返回可验证的再创作成片")
                self._state(
                    job_id,
                    "review",
                    "成片已就绪，请试听并确认发布",
                    claim_token=claim_token,
                )
            else:
                self._state(
                    job_id,
                    "interrupted" if resuming_locales else "review",
                    (
                        "已有部分语言成片；启用本地成片后可继续剩余语言"
                        if resuming_locales
                        else "解说草稿已就绪；启用本地成片后可继续制作"
                    ),
                    claim_token=claim_token,
                )
        except QueueSkip as exc:
            self._state(job_id, "skipped", str(exc), claim_token=claim_token)
        except Exception as exc:
            self._state(
                job_id,
                "blocked",
                f"制作暂停：{type(exc).__name__}，请查看任务详情或本机日志",
                claim_token=claim_token,
            )
        finally:
            self.center._release_active_job(job_id)
