#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Cross-platform discovery, download and publishing for the transfer center.

This module deliberately lives beside the legacy YouTube -> AcFun/Bilibili
pipeline.  The old task schema is tightly coupled to YouTube and changing it
would make a working workflow unnecessarily risky.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import requests

from .content_recreation import (
    deserialize_plan,
    generate_recreation_plan,
    serialize_plan,
    validate_review_payload,
)
from .media_preflight import build_distribution_plan, prepare_platform_variants
from .utils import get_app_subdir


logger = logging.getLogger("transfer_center")

PLATFORMS = {"bilibili", "douyin"}
DISCOVERY_MODES = {"account", "keyword", "manual"}
TARGETS = {"x", "youtube"}
JOB_STATUSES = {
    "DISCOVERED": "discovered",
    "DOWNLOADING": "downloading",
    "REVIEW": "review",
    "READY": "ready",
    "PUBLISHING": "publishing",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}
RETRY_DELAYS_SECONDS = (60, 300, 1800)
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
            items = parsed if isinstance(parsed, list) else []
        except Exception:
            items = [part.strip() for part in str(value or "").split(",")]
    return [str(item).strip() for item in items if str(item).strip()]


def _safe_error(value: Any, limit: int = 900) -> str:
    text = re.sub(r"(?i)(bearer|token|secret|cookie|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", str(value or ""))
    return text.strip()[:limit]


def _normalize_source_url(value: str) -> str:
    url = str(value or "").strip()
    if url and not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = f"https://{url.lstrip('/')}"
    return url


def _detect_platform(url: str) -> str:
    lowered = str(url or "").lower()
    if "bilibili.com" in lowered or "b23.tv" in lowered:
        return "bilibili"
    if "douyin.com" in lowered:
        return "douyin"
    return ""


def _source_direct_env(platform: str) -> dict[str, str]:
    """Keep Chinese source/CDN traffic off the YouTube proxy bridge."""
    env = os.environ.copy()
    if platform not in PLATFORMS:
        return env
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    direct_hosts = (
        "localhost",
        "127.0.0.1",
        ".bilibili.com",
        ".bilivideo.com",
        ".b23.tv",
        ".douyin.com",
        ".bytedance.com",
        ".amemv.com",
        ".douyinvod.com",
    )
    existing = str(env.get("NO_PROXY") or env.get("no_proxy") or "")
    merged = ",".join(dict.fromkeys([*filter(None, existing.split(",")), *direct_hosts]))
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    return env


def _friendly_download_error(value: Any) -> str:
    message = _safe_error(value)
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered:
        if "bilivideo" in lowered:
            return "B站视频分片网络超时，已切换国内直连；请稍后重试"
        return "视频下载网络超时，请稍后重试"
    if "unsupported url" in lowered:
        return "视频链接格式无法识别，请确认链接完整后重试"
    if "cookies" in lowered and any(token in lowered for token in ("expired", "login", "sign in")):
        return "来源平台登录已失效，请刷新登录后重试"
    return message


class TransferCenter:
    def __init__(self, config_provider: Callable[[], dict] | None = None):
        self._config_provider = config_provider or (lambda: {})
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._active_jobs_lock = threading.Lock()
        self._active_jobs: set[str] = set()
        self._scheduler = None
        self.db_path = os.path.join(get_app_subdir("db"), "transfer_center.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS transfer_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    discovery_mode TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    include_keywords TEXT DEFAULT '',
                    exclude_keywords TEXT DEFAULT '',
                    target_platforms TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL DEFAULT 15,
                    max_items INTEGER NOT NULL DEFAULT 10,
                    max_age_hours INTEGER NOT NULL DEFAULT 48,
                    daily_limit INTEGER NOT NULL DEFAULT 3,
                    first_scan_preview INTEGER NOT NULL DEFAULT 1,
                    first_scan_completed INTEGER NOT NULL DEFAULT 0,
                    recreation_mode TEXT NOT NULL DEFAULT 'commentary',
                    require_review INTEGER NOT NULL DEFAULT 1,
                    auto_prepare INTEGER NOT NULL DEFAULT 1,
                    auto_publish INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_scan_at TEXT,
                    last_scan_status TEXT,
                    last_scan_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transfer_jobs (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT,
                    source_platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_uploader TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    thumbnail_url TEXT DEFAULT '',
                    duration REAL,
                    published_at TEXT,
                    target_platforms TEXT NOT NULL,
                    status TEXT NOT NULL,
                    local_video_path TEXT DEFAULT '',
                    original_video_path TEXT DEFAULT '',
                    recreated_media_path TEXT DEFAULT '',
                    local_metadata_path TEXT DEFAULT '',
                    media_probe_json TEXT DEFAULT '{}',
                    platform_variants_json TEXT DEFAULT '{}',
                    distribution_plan_json TEXT DEFAULT '{}',
                    rights_basis TEXT DEFAULT 'unconfirmed',
                    rights_note TEXT DEFAULT '',
                    source_attribution TEXT DEFAULT '',
                    watermark_status TEXT DEFAULT 'unreviewed',
                    watermark_note TEXT DEFAULT '',
                    recreation_mode TEXT DEFAULT 'commentary',
                    processing_mode TEXT DEFAULT 'direct',
                    recreation_status TEXT DEFAULT 'pending',
                    recreation_plan_json TEXT DEFAULT '{}',
                    original_angle TEXT DEFAULT '',
                    original_contribution TEXT DEFAULT '',
                    x_text TEXT DEFAULT '',
                    youtube_title TEXT DEFAULT '',
                    youtube_description TEXT DEFAULT '',
                    reviewed_at TEXT,
                    recreation_completed INTEGER NOT NULL DEFAULT 0,
                    mpt_project_id TEXT DEFAULT '',
                    mpt_asset_id TEXT DEFAULT '',
                    mpt_status TEXT DEFAULT '',
                    mpt_message TEXT DEFAULT '',
                    mpt_workflow TEXT DEFAULT '',
                    prepare_attempts INTEGER NOT NULL DEFAULT 0,
                    x_publish_status TEXT DEFAULT 'pending',
                    youtube_publish_status TEXT DEFAULT 'pending',
                    x_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    youtube_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_retry_stage TEXT DEFAULT '',
                    x_post_id TEXT DEFAULT '',
                    youtube_video_id TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_platform, source_id),
                    FOREIGN KEY(rule_id) REFERENCES transfer_rules(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_transfer_jobs_status
                    ON transfer_jobs(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_transfer_jobs_rule
                    ON transfer_jobs(rule_id, created_at DESC);
                """
            )
            self._migrate_schema(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfer_jobs_retry "
                "ON transfer_jobs(next_retry_at, status)"
            )
            conn.execute(
                "UPDATE transfer_rules SET auto_publish = 0, require_review = 1 "
                "WHERE auto_publish <> 0 OR require_review <> 1"
            )
            conn.execute(
                """
                UPDATE transfer_jobs
                SET status = CASE
                        WHEN local_video_path <> '' THEN 'review'
                        ELSE 'failed'
                    END,
                    next_retry_at = ?,
                    last_retry_stage = CASE
                        WHEN local_video_path <> '' THEN 'publish'
                        ELSE 'prepare'
                    END,
                    error_message = CASE
                        WHEN error_message = '' THEN '服务重启后任务已恢复，等待重新处理'
                        ELSE error_message
                    END,
                    updated_at = ?
                WHERE status IN ('downloading', 'publishing')
                """,
                (_utc_now(), _utc_now()),
            )
            conn.execute(
                """
                UPDATE transfer_jobs
                SET source_url = 'https://' || ltrim(source_url, '/'),
                    updated_at = ?
                WHERE lower(source_url) NOT LIKE 'http://%'
                  AND lower(source_url) NOT LIKE 'https://%'
                  AND (
                      lower(source_url) LIKE '%bilibili.com%'
                      OR lower(source_url) LIKE '%b23.tv%'
                      OR lower(source_url) LIKE '%douyin.com%'
                  )
                """,
                (_utc_now(),),
            )

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        rule_columns = {
            "max_age_hours": "INTEGER NOT NULL DEFAULT 48",
            "daily_limit": "INTEGER NOT NULL DEFAULT 3",
            "first_scan_preview": "INTEGER NOT NULL DEFAULT 1",
            "first_scan_completed": "INTEGER NOT NULL DEFAULT 0",
            "recreation_mode": "TEXT NOT NULL DEFAULT 'commentary'",
            "require_review": "INTEGER NOT NULL DEFAULT 1",
        }
        job_columns = {
            "media_probe_json": "TEXT DEFAULT '{}'",
            "original_video_path": "TEXT DEFAULT ''",
            "recreated_media_path": "TEXT DEFAULT ''",
            "platform_variants_json": "TEXT DEFAULT '{}'",
            "distribution_plan_json": "TEXT DEFAULT '{}'",
            "rights_basis": "TEXT DEFAULT 'unconfirmed'",
            "rights_note": "TEXT DEFAULT ''",
            "source_attribution": "TEXT DEFAULT ''",
            "watermark_status": "TEXT DEFAULT 'unreviewed'",
            "watermark_note": "TEXT DEFAULT ''",
            "recreation_mode": "TEXT DEFAULT 'commentary'",
            "processing_mode": "TEXT DEFAULT 'direct'",
            "recreation_status": "TEXT DEFAULT 'pending'",
            "recreation_plan_json": "TEXT DEFAULT '{}'",
            "original_angle": "TEXT DEFAULT ''",
            "original_contribution": "TEXT DEFAULT ''",
            "x_text": "TEXT DEFAULT ''",
            "youtube_title": "TEXT DEFAULT ''",
            "youtube_description": "TEXT DEFAULT ''",
            "reviewed_at": "TEXT",
            "recreation_completed": "INTEGER NOT NULL DEFAULT 0",
            "mpt_project_id": "TEXT DEFAULT ''",
            "mpt_asset_id": "TEXT DEFAULT ''",
            "mpt_status": "TEXT DEFAULT ''",
            "mpt_message": "TEXT DEFAULT ''",
            "mpt_workflow": "TEXT DEFAULT ''",
            "prepare_attempts": "INTEGER NOT NULL DEFAULT 0",
            "x_publish_status": "TEXT DEFAULT 'pending'",
            "youtube_publish_status": "TEXT DEFAULT 'pending'",
            "x_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "youtube_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "TEXT",
            "last_retry_stage": "TEXT DEFAULT ''",
            "progress_percent": "REAL NOT NULL DEFAULT 0",
            "progress_message": "TEXT DEFAULT ''",
        }
        for table_name, additions in (
            ("transfer_rules", rule_columns),
            ("transfer_jobs", job_columns),
        ):
            existing = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for column_name, definition in additions.items():
                if column_name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
                    )
        conn.execute(
            """
            UPDATE transfer_jobs
            SET original_video_path = local_video_path
            WHERE original_video_path = '' AND local_video_path <> ''
            """
        )
        conn.execute(
            """
            UPDATE transfer_jobs
            SET processing_mode = 'professional'
            WHERE recreation_completed = 1
              AND (processing_mode = '' OR processing_mode = 'direct')
            """
        )
        conn.execute(
            """
            UPDATE transfer_jobs
            SET source_attribution = trim(
                CASE
                    WHEN source_uploader <> '' THEN source_uploader || char(10)
                    ELSE ''
                END || source_url
            )
            WHERE source_attribution = ''
            """
        )

    def _config(self) -> dict:
        config = self._config_provider()
        return dict(config) if isinstance(config, dict) else {}

    # ---- rules ---------------------------------------------------------
    def list_rules(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transfer_rules ORDER BY enabled DESC, created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_rule(self, rule_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM transfer_rules WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None

    def save_rule(self, payload: dict, rule_id: str | None = None) -> str:
        platform = str(payload.get("platform") or "").strip().lower()
        mode = str(payload.get("discovery_mode") or "").strip().lower()
        source_value = str(payload.get("source_value") or "").strip()
        targets = [item for item in _json_list(payload.get("target_platforms")) if item in TARGETS]
        if platform not in PLATFORMS:
            raise ValueError("来源平台无效")
        if mode not in DISCOVERY_MODES - {"manual"}:
            raise ValueError("发现方式无效")
        if not source_value:
            raise ValueError("账号链接或关键词不能为空")
        if not targets:
            raise ValueError("至少选择一个发布平台")
        if platform == "douyin" and mode == "account" and "douyin.com" not in source_value.lower():
            raise ValueError("抖音账号监控需要填写公开主页链接")
        if platform == "bilibili" and mode == "account" and not any(
            host in source_value.lower() for host in ("bilibili.com", "b23.tv")
        ):
            raise ValueError("B站账号监控需要填写个人空间链接")
        recreation_mode = str(payload.get("recreation_mode") or "commentary").strip().lower()
        if recreation_mode not in {"commentary", "localized", "drama_recap", "authorized_repost"}:
            recreation_mode = "commentary"

        now = _utc_now()
        rid = rule_id or str(uuid.uuid4())
        values = {
            "id": rid,
            "name": str(payload.get("name") or source_value).strip()[:120],
            "platform": platform,
            "discovery_mode": mode,
            "source_value": source_value,
            "include_keywords": str(payload.get("include_keywords") or "").strip(),
            "exclude_keywords": str(payload.get("exclude_keywords") or "").strip(),
            "target_platforms": json.dumps(targets, ensure_ascii=False),
            "interval_minutes": max(5, min(1440, int(payload.get("interval_minutes") or 15))),
            "max_items": max(1, min(50, int(payload.get("max_items") or 10))),
            "max_age_hours": max(1, min(720, int(payload.get("max_age_hours") or 48))),
            "daily_limit": max(1, min(50, int(payload.get("daily_limit") or 3))),
            "first_scan_preview": int(_as_bool(payload.get("first_scan_preview", True))),
            "recreation_mode": recreation_mode,
            "require_review": 1,
            "auto_prepare": int(_as_bool(payload.get("auto_prepare", True))),
            "auto_publish": 0,
            "enabled": int(_as_bool(payload.get("enabled", True))),
            "updated_at": now,
        }
        with self._connect() as conn:
            existing = conn.execute("SELECT id, created_at FROM transfer_rules WHERE id = ?", (rid,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE transfer_rules SET
                        name=:name, platform=:platform, discovery_mode=:discovery_mode,
                        source_value=:source_value, include_keywords=:include_keywords,
                        exclude_keywords=:exclude_keywords, target_platforms=:target_platforms,
                        interval_minutes=:interval_minutes, max_items=:max_items,
                        max_age_hours=:max_age_hours, daily_limit=:daily_limit,
                        first_scan_preview=:first_scan_preview,
                        recreation_mode=:recreation_mode, require_review=:require_review,
                        auto_prepare=:auto_prepare, auto_publish=:auto_publish,
                        enabled=:enabled, updated_at=:updated_at
                    WHERE id=:id
                    """,
                    values,
                )
            else:
                values["created_at"] = now
                conn.execute(
                    """
                    INSERT INTO transfer_rules (
                        id, name, platform, discovery_mode, source_value,
                        include_keywords, exclude_keywords, target_platforms,
                        interval_minutes, max_items, max_age_hours, daily_limit,
                        first_scan_preview, recreation_mode, require_review,
                        auto_prepare, auto_publish, enabled, created_at, updated_at
                    ) VALUES (
                        :id, :name, :platform, :discovery_mode, :source_value,
                        :include_keywords, :exclude_keywords, :target_platforms,
                        :interval_minutes, :max_items, :max_age_hours, :daily_limit,
                        :first_scan_preview, :recreation_mode, :require_review,
                        :auto_prepare, :auto_publish, :enabled, :created_at, :updated_at
                    )
                    """,
                    values,
                )
        return rid

    def delete_rule(self, rule_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM transfer_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE transfer_rules SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), _utc_now(), rule_id),
            )
        return cursor.rowcount > 0

    # ---- discovery -----------------------------------------------------
    def _cookie_path(self, platform: str) -> str | None:
        config = self._config()
        configured = str(
            config.get("TRANSFER_DOUYIN_COOKIES_PATH" if platform == "douyin" else "TRANSFER_BILIBILI_COOKIES_PATH")
            or ("cookies/douyin_cookies.txt" if platform == "douyin" else "cookies/bilibili_source_cookies.txt")
        ).strip()
        path = configured if os.path.isabs(configured) else os.path.join(get_app_subdir(""), configured)
        real_root = os.path.realpath(get_app_subdir(""))
        real_path = os.path.realpath(path)
        if os.path.commonpath((real_root, real_path)) != real_root:
            return None
        return real_path if os.path.isfile(real_path) else None

    def _yt_dlp_json(self, source: str, platform: str, max_items: int, flat: bool = True) -> dict:
        cmd = [
            "yt-dlp",
            "--dump-single-json",
            "--no-warnings",
            "--playlist-end",
            str(max_items),
        ]
        if flat:
            cmd.append("--flat-playlist")
        cookie_path = self._cookie_path(platform)
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(source)
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_source_direct_env(platform),
        )
        if completed.returncode != 0:
            raise RuntimeError(_safe_error(completed.stderr or completed.stdout or "yt-dlp发现失败"))
        try:
            return json.loads(completed.stdout)
        except Exception as exc:
            raise RuntimeError("平台返回内容无法解析") from exc

    def _requests_session(self, platform: str) -> requests.Session:
        session = requests.Session()
        if platform in PLATFORMS:
            session.trust_env = False
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        cookie_path = self._cookie_path(platform)
        if cookie_path:
            jar = http.cookiejar.MozillaCookieJar(cookie_path)
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                session.cookies.update(jar)
            except Exception:
                logger.warning("%s 来源 Cookie 无法读取", platform)
        return session

    def _discover_douyin_page(self, rule: dict) -> list[dict]:
        if rule["discovery_mode"] == "keyword":
            url = f"https://www.douyin.com/search/{quote(rule['source_value'])}?type=video"
        else:
            url = rule["source_value"]
        response = self._requests_session("douyin").get(url, timeout=30)
        response.raise_for_status()
        ids = []
        for pattern in (
            r'"aweme_id"\s*:\s*"(\d{10,30})"',
            r'\\?"awemeId\\?"\s*:\s*\\?"(\d{10,30})',
            r'/video/(\d{10,30})',
        ):
            ids.extend(re.findall(pattern, response.text))
        unique_ids = list(dict.fromkeys(ids))[: int(rule["max_items"])]
        if not unique_ids:
            raise RuntimeError(
                "抖音页面未返回可识别视频；请更新抖音 Cookie。若仍失败，平台可能要求新的浏览器签名。"
            )
        return [
            {
                "id": video_id,
                "url": f"https://www.douyin.com/video/{video_id}",
                "title": f"抖音视频 {video_id}",
                "uploader": "",
            }
            for video_id in unique_ids
        ]

    def _discover_items(self, rule: dict) -> list[dict]:
        if rule["platform"] == "bilibili":
            source = (
                f"bilisearch{int(rule['max_items'])}:{rule['source_value']}"
                if rule["discovery_mode"] == "keyword"
                else rule["source_value"]
            )
            data = self._yt_dlp_json(source, "bilibili", int(rule["max_items"]), flat=True)
            entries = data.get("entries") if isinstance(data, dict) else []
            items = []
            for entry in entries or [data]:
                if not isinstance(entry, dict):
                    continue
                video_id = str(entry.get("id") or entry.get("bvid") or "").strip()
                url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
                if video_id and not url.startswith("http"):
                    url = f"https://www.bilibili.com/video/{video_id}"
                if video_id and url:
                    items.append(
                        {
                            "id": video_id,
                            "url": url,
                            "title": str(entry.get("title") or ""),
                            "uploader": str(entry.get("uploader") or entry.get("channel") or ""),
                            "description": str(entry.get("description") or ""),
                            "thumbnail": str(entry.get("thumbnail") or ""),
                            "duration": entry.get("duration"),
                            "timestamp": entry.get("timestamp"),
                        }
                    )
            return items
        return self._discover_douyin_page(rule)

    @staticmethod
    def _matches_filters(rule: dict, item: dict) -> bool:
        haystack = f"{item.get('title', '')} {item.get('description', '')}".lower()
        includes = [part.strip().lower() for part in str(rule.get("include_keywords") or "").split(",") if part.strip()]
        excludes = [part.strip().lower() for part in str(rule.get("exclude_keywords") or "").split(",") if part.strip()]
        if includes and not any(part in haystack for part in includes):
            return False
        return not any(part in haystack for part in excludes)

    @staticmethod
    def _is_recent_enough(rule: dict, item: dict) -> bool:
        raw_value = item.get("timestamp")
        if raw_value in (None, "", 0, "0"):
            return True
        try:
            if isinstance(raw_value, (int, float)) or str(raw_value).isdigit():
                published = datetime.fromtimestamp(float(raw_value), tz=timezone.utc)
            else:
                published = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - published.astimezone(timezone.utc)
            return age <= timedelta(hours=max(1, int(rule.get("max_age_hours") or 48)))
        except Exception:
            return True

    def _jobs_created_today(self, rule_id: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM transfer_jobs
                WHERE rule_id = ? AND substr(created_at, 1, 10) = ?
                """,
                (rule_id, today),
            ).fetchone()
        return int(row["total"] if row else 0)

    def _insert_discovered_job(self, rule: dict, item: dict) -> tuple[str, bool]:
        now = _utc_now()
        source_id = str(item.get("id") or hashlib.sha256(item["url"].encode()).hexdigest()[:24])
        job_id = str(uuid.uuid4())
        targets = _json_list(rule["target_platforms"])
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO transfer_jobs (
                        id, rule_id, source_platform, source_id, source_url,
                        source_uploader, title, description, thumbnail_url,
                        duration, published_at, target_platforms, status,
                        recreation_mode, x_publish_status, youtube_publish_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        rule.get("id"),
                        rule["platform"],
                        source_id,
                        item["url"],
                        item.get("uploader", ""),
                        item.get("title", ""),
                        item.get("description", ""),
                        item.get("thumbnail", ""),
                        item.get("duration"),
                        str(item.get("timestamp") or ""),
                        rule["target_platforms"],
                        JOB_STATUSES["DISCOVERED"],
                        str(rule.get("recreation_mode") or "commentary"),
                        "pending" if "x" in targets else "skipped",
                        "pending" if "youtube" in targets else "skipped",
                        now,
                        now,
                    ),
                )
                return job_id, True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM transfer_jobs WHERE source_platform=? AND source_id=?",
                    (rule["platform"], source_id),
                ).fetchone()
                return (row["id"] if row else ""), False

    def scan_rule(self, rule_id: str) -> dict:
        rule = self.get_rule(rule_id)
        if not rule:
            raise ValueError("监控规则不存在")
        try:
            items = self._discover_items(rule)
            candidates = [
                item
                for item in items
                if self._matches_filters(rule, item) and self._is_recent_enough(rule, item)
            ]
            remaining = max(
                0,
                int(rule.get("daily_limit") or 3) - self._jobs_created_today(rule_id),
            )
            added_ids = []
            for item in candidates[:remaining]:
                job_id, created = self._insert_discovered_job(rule, item)
                if created:
                    added_ids.append(job_id)
            first_scan_preview = bool(rule.get("first_scan_preview")) and not bool(
                rule.get("first_scan_completed")
            )
            message = (
                f"发现 {len(items)} 条，符合时效和关键词 {len(candidates)} 条，"
                f"今日新增 {len(added_ids)} 条"
            )
            if remaining <= 0:
                message += "；已达到今日上限"
            if first_scan_preview:
                message += "；首次扫描仅进入审核区，不自动下载"
            self._mark_rule_scan(rule_id, "success", message)
            if rule["auto_prepare"] and not first_scan_preview:
                for job_id in added_ids:
                    self.prepare_job_async(job_id, publish_after=False)
            return {
                "success": True,
                "found": len(items),
                "eligible": len(candidates),
                "added": len(added_ids),
                "preview": first_scan_preview,
                "message": message,
            }
        except Exception as exc:
            message = _safe_error(exc)
            self._mark_rule_scan(rule_id, "failed", message)
            logger.warning("搬运规则扫描失败 %s: %s", rule_id, message)
            return {"success": False, "found": 0, "added": 0, "message": message}

    def _mark_rule_scan(self, rule_id: str, status: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE transfer_rules
                SET last_scan_at=?, last_scan_status=?, last_scan_message=?,
                    first_scan_completed=CASE WHEN ?='success' THEN 1 ELSE first_scan_completed END,
                    updated_at=?
                WHERE id=?
                """,
                (_utc_now(), status, _safe_error(message), status, _utc_now(), rule_id),
            )

    def scan_due_rules(self) -> None:
        if not self._worker_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            for rule in self.list_rules():
                if not rule["enabled"]:
                    continue
                last_scan = rule.get("last_scan_at")
                if last_scan:
                    try:
                        last_epoch = datetime.fromisoformat(last_scan).timestamp()
                        if now - last_epoch < int(rule["interval_minutes"]) * 60:
                            continue
                    except Exception:
                        pass
                self.scan_rule(rule["id"])
        finally:
            self._worker_lock.release()

    # ---- jobs ----------------------------------------------------------
    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transfer_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dashboard_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM transfer_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["total"]) for row in rows}
        return {
            "total": sum(counts.values()),
            "discovered": counts.get("discovered", 0),
            "downloading": counts.get("downloading", 0),
            "review": counts.get("review", 0),
            "ready": counts.get("ready", 0),
            "publishing": counts.get("publishing", 0),
            "failed": counts.get("failed", 0),
            "completed": counts.get("completed", 0),
        }

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_manual_job(self, source_url: str, targets: list[str]) -> str:
        source_url = _normalize_source_url(source_url)
        platform = _detect_platform(source_url)
        if platform not in PLATFORMS:
            raise ValueError("当前只支持B站和国内抖音链接")
        valid_targets = [item for item in targets if item in TARGETS]
        if not valid_targets:
            raise ValueError("至少选择一个发布平台")
        rule = {
            "id": None,
            "platform": platform,
            "target_platforms": json.dumps(valid_targets, ensure_ascii=False),
        }
        source_id = hashlib.sha256(source_url.encode()).hexdigest()[:24]
        job_id, created = self._insert_discovered_job(
            rule,
            {"id": source_id, "url": source_url, "title": ""},
        )
        if not created:
            raise ValueError("这个视频已经在搬运任务中")
        return job_id

    def _update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "title",
            "description",
            "source_uploader",
            "thumbnail_url",
            "duration",
            "local_video_path",
            "original_video_path",
            "recreated_media_path",
            "local_metadata_path",
            "media_probe_json",
            "platform_variants_json",
            "distribution_plan_json",
            "rights_basis",
            "rights_note",
            "source_attribution",
            "watermark_status",
            "watermark_note",
            "recreation_mode",
            "processing_mode",
            "recreation_status",
            "recreation_plan_json",
            "original_angle",
            "original_contribution",
            "x_text",
            "youtube_title",
            "youtube_description",
            "reviewed_at",
            "recreation_completed",
            "mpt_project_id",
            "mpt_asset_id",
            "mpt_status",
            "mpt_message",
            "mpt_workflow",
            "prepare_attempts",
            "x_publish_status",
            "youtube_publish_status",
            "x_publish_attempts",
            "youtube_publish_attempts",
            "next_retry_at",
            "last_retry_stage",
            "x_post_id",
            "youtube_video_id",
            "error_message",
            "progress_percent",
            "progress_message",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE transfer_jobs SET {assignments} WHERE id = ?",
                (*updates.values(), job_id),
            )

    def _claim_active_job(self, job_id: str) -> bool:
        with self._active_jobs_lock:
            if job_id in self._active_jobs:
                return False
            self._active_jobs.add(job_id)
            return True

    def _release_active_job(self, job_id: str) -> None:
        with self._active_jobs_lock:
            self._active_jobs.discard(job_id)

    def prepare_job_async(self, job_id: str, publish_after: bool = False) -> bool:
        if not self._claim_active_job(job_id):
            return False
        thread = threading.Thread(
            target=self._prepare_job_guarded,
            args=(job_id, publish_after),
            name=f"transfer-prepare-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _prepare_job_guarded(self, job_id: str, publish_after: bool) -> None:
        try:
            self.prepare_job(job_id)
            if publish_after:
                self.publish_job(job_id)
        except Exception as exc:
            job = self.get_job(job_id) or {}
            self._schedule_retry(
                job_id,
                "prepare",
                int(job.get("prepare_attempts") or 1),
                exc,
            )
            logger.exception("搬运任务处理失败 %s", job_id)
        finally:
            self._release_active_job(job_id)

    def _schedule_retry(
        self,
        job_id: str,
        stage: str,
        attempts: int,
        error: Any,
    ) -> None:
        safe_message = _safe_error(error)
        retry_at = None
        if attempts <= len(RETRY_DELAYS_SECONDS):
            retry_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
            ).isoformat(timespec="seconds")
        self._update_job(
            job_id,
            status=(
                JOB_STATUSES["FAILED"]
                if stage == "prepare"
                else JOB_STATUSES["READY"]
            ),
            next_retry_at=retry_at,
            last_retry_stage=stage,
            error_message=(
                f"{safe_message}；系统将在后台自动重试"
                if retry_at
                else f"{safe_message}；自动重试已用完，请人工处理"
            ),
            progress_message=(
                "下载失败，等待自动重试"
                if stage == "prepare" and retry_at
                else "处理失败，需要人工检查"
            ),
        )
        if not retry_at:
            self._emit_failure_notification(job_id, safe_message)

    def _emit_failure_notification(self, job_id: str, error_message: str) -> None:
        try:
            from .notifications import (
                EVENT_TASK_FAILED,
                NotificationEvent,
                emit_notification_event,
            )

            job = self.get_job(job_id) or {}
            emit_notification_event(
                NotificationEvent(
                    event_type=EVENT_TASK_FAILED,
                    payload={
                        "task_id": job_id,
                        "title": job.get("title") or "视频搬运任务",
                        "status": job.get("status") or "failed",
                        "upload_target": ",".join(_json_list(job.get("target_platforms"))),
                        "error_message": error_message,
                    },
                )
            )
        except Exception:
            logger.debug("搬运任务失败通知未启用或不可用", exc_info=True)

    def retry_due_jobs(self) -> None:
        now = _utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, last_retry_stage
                FROM transfer_jobs
                WHERE next_retry_at IS NOT NULL
                  AND next_retry_at <= ?
                  AND status IN ('failed', 'ready')
                ORDER BY next_retry_at ASC
                LIMIT 10
                """,
                (now,),
            ).fetchall()
        for row in rows:
            if row["last_retry_stage"] == "prepare":
                self.prepare_job_async(row["id"], publish_after=False)
            elif row["last_retry_stage"] == "publish":
                self.publish_job_async(row["id"])

    def prepare_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE transfer_jobs
                SET status=?, prepare_attempts=prepare_attempts+1,
                    next_retry_at=NULL, last_retry_stage='prepare',
                    error_message='', progress_percent=3,
                    progress_message='正在解析视频信息', updated_at=?
                WHERE id=?
                """,
                (JOB_STATUSES["DOWNLOADING"], _utc_now(), job_id),
            )
        job = self.get_job(job_id) or job
        output_dir = Path(get_app_subdir("downloads")) / "transfer" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / "video.%(ext)s")
        metadata_path = output_dir / "metadata.json"
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--newline",
            "--no-colors",
            "--force-ipv4",
            "--socket-timeout",
            "60",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--retry-sleep",
            "exp=1:20",
            "--progress-template",
            "download:%(progress._percent_str)s",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "zh.*,en.*,ja.*,ko.*",
            "--sub-format",
            "vtt/srt/best",
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
        ]
        cookie_path = self._cookie_path(job["source_platform"])
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(_normalize_source_url(job["source_url"]))
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_source_direct_env(job["source_platform"]),
        )
        output_lines: list[str] = []
        last_progress = 3
        started_at = time.monotonic()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line)
                output_lines = output_lines[-80:]
                match = re.search(r"download:\s*([0-9]+(?:\.[0-9]+)?)%", line)
                if match:
                    percent = max(3, min(88, int(float(match.group(1)) * 0.85) + 3))
                    if percent >= last_progress + 2:
                        last_progress = percent
                        self._update_job(
                            job_id,
                            progress_percent=percent,
                            progress_message=f"正在下载视频素材 {match.group(1)}%",
                        )
                if time.monotonic() - started_at > 7200:
                    process.terminate()
                    raise RuntimeError("视频下载超过两小时，已停止并等待重试")
            return_code = process.wait(timeout=30)
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise
        if return_code != 0:
            raw_error = "".join(output_lines) or "下载失败"
            logger.warning("搬运任务下载失败 %s: %s", job_id, _safe_error(raw_error, limit=2400))
            raise RuntimeError(_friendly_download_error(raw_error))
        self._update_job(
            job_id,
            progress_percent=90,
            progress_message="下载完成，正在进行媒体体检",
        )
        videos = sorted(
            path
            for path in output_dir.glob("video.*")
            if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
        )
        if not videos:
            raise RuntimeError("下载完成但未找到视频文件")
        info_files = sorted(output_dir.glob("video.info.json"))
        metadata = {}
        if info_files:
            try:
                metadata = json.loads(info_files[0].read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        prepared_fields = {
            "title": str(metadata.get("title") or job.get("title") or "")[:500],
            "description": str(metadata.get("description") or job.get("description") or "")[:8000],
            "source_uploader": str(metadata.get("uploader") or job.get("source_uploader") or "")[:300],
            "thumbnail_url": str(metadata.get("thumbnail") or job.get("thumbnail_url") or "")[:1500],
            "duration": metadata.get("duration") or job.get("duration"),
            "local_video_path": str(videos[0]),
            "original_video_path": str(videos[0]),
            "recreated_media_path": "",
            "local_metadata_path": str(metadata_path),
            "source_attribution": "\n".join(
                item
                for item in (
                    str(metadata.get("uploader") or job.get("source_uploader") or "").strip(),
                    str(job.get("source_url") or "").strip(),
                )
                if item
            ),
        }
        targets = _json_list(job["target_platforms"])
        media_info, variants = prepare_platform_variants(
            str(videos[0]),
            str(output_dir),
            targets,
        )
        distribution_plan = build_distribution_plan(media_info, targets)
        recreation_job = {**job, **prepared_fields}
        plan = generate_recreation_plan(
            recreation_job,
            self._config(),
            mode=str(job.get("recreation_mode") or "commentary"),
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["REVIEW"],
            progress_percent=72,
            progress_message="素材已就绪，等待选择处理方式",
            **prepared_fields,
            media_probe_json=json.dumps(media_info, ensure_ascii=False),
            platform_variants_json=json.dumps(variants, ensure_ascii=False),
            distribution_plan_json=json.dumps(distribution_plan, ensure_ascii=False),
            recreation_status="draft",
            recreation_completed=0,
            processing_mode="direct",
            recreation_plan_json=serialize_plan(plan),
            original_angle=str(plan.get("original_angle") or ""),
            original_contribution=str(plan.get("original_contribution") or ""),
            x_text=str(plan.get("x_text") or ""),
            youtube_title=str(plan.get("youtube_title") or ""),
            youtube_description=str(plan.get("youtube_description") or ""),
            next_retry_at=None,
            last_retry_stage="",
            error_message="",
        )
        return self.get_job(job_id) or {}

    def generate_recreation_draft(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        if not job.get("local_video_path"):
            raise ValueError("请先完成视频下载和媒体体检")
        plan = generate_recreation_plan(
            job,
            self._config(),
            mode=str(job.get("recreation_mode") or "commentary"),
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["REVIEW"],
            recreation_status="draft",
            recreation_plan_json=serialize_plan(plan),
            original_angle=str(plan.get("original_angle") or ""),
            original_contribution=str(plan.get("original_contribution") or ""),
            x_text=str(plan.get("x_text") or ""),
            youtube_title=str(plan.get("youtube_title") or ""),
            youtube_description=str(plan.get("youtube_description") or ""),
            reviewed_at=None,
            error_message="",
        )
        return self.get_job(job_id) or {}

    def replace_recreated_media(self, job_id: str, video_path: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        if job.get("x_post_id") or job.get("youtube_video_id"):
            raise ValueError("已有平台发布结果，不能替换成片")
        if not os.path.isfile(video_path):
            raise ValueError("再创作成片文件不存在")
        targets = _json_list(job.get("target_platforms"))
        media_info, variants = prepare_platform_variants(
            video_path,
            str(Path(video_path).parent),
            targets,
        )
        distribution_plan = build_distribution_plan(media_info, targets)
        self._update_job(
            job_id,
            status=JOB_STATUSES["REVIEW"],
            local_video_path=video_path,
            recreated_media_path=video_path,
            media_probe_json=json.dumps(media_info, ensure_ascii=False),
            platform_variants_json=json.dumps(variants, ensure_ascii=False),
            distribution_plan_json=json.dumps(distribution_plan, ensure_ascii=False),
            recreation_status="draft",
            recreation_completed=1,
            watermark_status="unreviewed",
            watermark_note="",
            reviewed_at=None,
            x_publish_status="pending" if "x" in targets else "skipped",
            youtube_publish_status="pending" if "youtube" in targets else "skipped",
            next_retry_at=None,
            last_retry_stage="",
            error_message="",
        )
        return self.get_job(job_id) or {}

    def save_recreation_review(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        approve: bool = False,
    ) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        normalized = validate_review_payload(payload)
        plan = deserialize_plan(job.get("recreation_plan_json"))
        plan.update(
            {
                "original_angle": normalized["original_angle"],
                "original_contribution": normalized["original_contribution"],
                "watermark_status": normalized["watermark_status"],
                "watermark_note": normalized["watermark_note"],
                "x_text": normalized["x_text"],
                "youtube_title": normalized["youtube_title"],
                "youtube_description": normalized["youtube_description"],
            }
        )
        status = JOB_STATUSES["REVIEW"]
        recreation_status = "draft"
        reviewed_at = None
        if approve:
            if (
                normalized["processing_mode"] != "direct"
                and not int(job.get("recreation_completed") or 0)
            ):
                raise ValueError("请先通过超级印钞机或其他剪辑工具完成加工，并上传新的再创作成片")
            if normalized["processing_mode"] == "direct":
                original_path = str(job.get("original_video_path") or "")
                if original_path and os.path.isfile(original_path):
                    targets = _json_list(job.get("target_platforms"))
                    media_info, variants = prepare_platform_variants(
                        original_path,
                        str(Path(original_path).parent),
                        targets,
                    )
                    distribution_plan = build_distribution_plan(media_info, targets)
                    self._update_job(
                        job_id,
                        local_video_path=original_path,
                        recreation_completed=0,
                        media_probe_json=json.dumps(media_info, ensure_ascii=False),
                        platform_variants_json=json.dumps(variants, ensure_ascii=False),
                        distribution_plan_json=json.dumps(
                            distribution_plan, ensure_ascii=False
                        ),
                    )
                    job = self.get_job(job_id) or job
            variants = deserialize_plan(job.get("platform_variants_json"))
            blockers = []
            for target in _json_list(job.get("target_platforms")):
                target_state = variants.get(target) if isinstance(variants, dict) else None
                if not isinstance(target_state, dict) or target_state.get("status") != "ready":
                    issues = target_state.get("issues") if isinstance(target_state, dict) else []
                    detail = "、".join(str(item) for item in (issues or []))
                    blockers.append(f"{target.upper()}媒体版本未就绪{f'：{detail}' if detail else ''}")
            if blockers:
                raise ValueError("；".join(blockers))
            status = JOB_STATUSES["READY"]
            recreation_status = "approved"
            reviewed_at = _utc_now()
        self._update_job(
            job_id,
            status=status,
            source_attribution=normalized["source_attribution"],
            processing_mode=normalized["processing_mode"],
            recreation_mode=normalized["recreation_mode"],
            recreation_status=recreation_status,
            recreation_plan_json=serialize_plan(plan),
            original_angle=normalized["original_angle"],
            original_contribution=normalized["original_contribution"],
            watermark_status=normalized["watermark_status"],
            watermark_note=normalized["watermark_note"],
            x_text=normalized["x_text"],
            youtube_title=normalized["youtube_title"],
            youtube_description=normalized["youtube_description"],
            reviewed_at=reviewed_at,
            error_message="",
        )
        return self.get_job(job_id) or {}

    def send_to_money_printer(
        self,
        job_id: str,
        *,
        workflow: str = "quick",
    ) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        video_path = str(job.get("original_video_path") or job.get("local_video_path") or "")
        if not video_path or not os.path.isfile(video_path):
            raise ValueError("原视频尚未下载完成")
        workflow = workflow if workflow in {"quick", "professional"} else "quick"
        if job.get("mpt_project_id") and job.get("mpt_asset_id"):
            self._update_job(
                job_id,
                processing_mode=workflow,
                mpt_workflow=workflow,
            )
            return self.get_job(job_id) or {}

        base_url = str(
            self._config().get("TRANSFER_MPT_INTERNAL_URL")
            or "http://172.17.0.1:18081"
        ).rstrip("/")
        session = requests.Session()
        session.trust_env = False
        self._update_job(
            job_id,
            mpt_status="sending",
            mpt_workflow=workflow,
            mpt_message="正在创建加工项目并读取原片",
        )

        def api_json(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            response = session.request(
                method,
                f"{base_url}{path}",
                timeout=(5, 180),
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("status") or 500) != 200:
                raise RuntimeError(str(payload.get("message") or "超级印钞机接口失败"))
            data = payload.get("data")
            return data if isinstance(data, dict) else {}

        try:
            project = api_json(
                "POST",
                "/api/v1/projects",
                json={
                    "name": f"搬运加工｜{str(job.get('title') or '未命名视频')[:36]}",
                    "description": (
                        "来自视频搬运通道。可选择简单加工或专业加工；"
                        "导出后回传成片，再由用户确认发布。\n"
                        f"来源：{job.get('source_uploader') or '原发布者'}\n"
                        f"{job.get('source_url') or ''}"
                    ),
                    "template": "short_video",
                },
            )
            project_id = str(project.get("project_id") or "")
            if not project_id:
                raise RuntimeError("超级印钞机未返回项目编号")
            filename = Path(video_path).name
            query = urlencode(
                {
                    "asset_type": "video",
                    "tags": "搬运通道,参考视频,待再创作",
                    "license_type": "unknown",
                    "rights_note": (
                        f"来源署名：{job.get('source_uploader') or '原发布者'}；"
                        "仅作为再创作参考，不代表原片可直接发布。"
                    ),
                }
            )
            with open(video_path, "rb") as media:
                asset = api_json(
                    "POST",
                    f"/api/v1/assets/upload?{query}",
                    files={"file": (filename, media, mimetypes.guess_type(filename)[0] or "video/mp4")},
                )
            asset_id = str(asset.get("asset_id") or "")
            if not asset_id:
                raise RuntimeError("超级印钞机未返回素材编号")
            api_json(
                "POST",
                f"/api/v1/projects/{quote(project_id)}/reference-analyses",
                json={"asset_id": asset_id},
            )
            self._update_job(
                job_id,
                mpt_project_id=project_id,
                mpt_asset_id=asset_id,
                mpt_status="ready",
                processing_mode=workflow,
                mpt_workflow=workflow,
                mpt_message=(
                    "原片已就绪，可以一键生成简单成片"
                    if workflow == "quick"
                    else "原片分析完成，可以进行专业加工"
                ),
            )
            return self.get_job(job_id) or {}
        except Exception as exc:
            message = _safe_error(exc, limit=500)
            self._update_job(
                job_id,
                mpt_status="failed",
                mpt_message=message,
            )
            raise RuntimeError(f"送入超级印钞机失败：{message}") from exc

    def money_printer_url(
        self,
        job: dict[str, Any],
        *,
        workflow: str | None = None,
    ) -> str:
        project_id = str(job.get("mpt_project_id") or "").strip()
        if not project_id:
            return ""
        selected_workflow = (
            workflow
            if workflow in {"quick", "professional"}
            else str(job.get("mpt_workflow") or job.get("processing_mode") or "quick")
        )
        if selected_workflow not in {"quick", "professional"}:
            selected_workflow = "quick"
        public_url = str(
            self._config().get("TRANSFER_MPT_PUBLIC_URL")
            or "http://192.168.1.249:18081/app/"
        ).strip()
        separator = "&" if "?" in public_url else "?"
        return (
            f"{public_url}{separator}"
            + urlencode(
                {
                    "project_id": project_id,
                    "asset_id": str(job.get("mpt_asset_id") or ""),
                    "studio": "quick" if selected_workflow == "quick" else "intelligence",
                    "workflow": selected_workflow,
                    "source": "transfer",
                    "source_attribution": str(job.get("source_uploader") or "原发布者")[:120],
                }
            )
        )

    # ---- publishing ----------------------------------------------------
    def publish_job_async(self, job_id: str) -> bool:
        if not self._claim_active_job(job_id):
            return False
        thread = threading.Thread(
            target=self._publish_job_guarded,
            args=(job_id,),
            name=f"transfer-publish-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _publish_job_guarded(self, job_id: str) -> None:
        try:
            self.publish_job(job_id)
        except Exception as exc:
            if isinstance(exc, ValueError):
                self._update_job(
                    job_id,
                    status=JOB_STATUSES["REVIEW"],
                    next_retry_at=None,
                    last_retry_stage="",
                    error_message=_safe_error(exc),
                )
            else:
                job = self.get_job(job_id) or {}
                attempts = max(
                    int(job.get("x_publish_attempts") or 0),
                    int(job.get("youtube_publish_attempts") or 0),
                    1,
                )
                self._schedule_retry(job_id, "publish", attempts, exc)
            logger.exception("搬运任务发布失败 %s", job_id)
        finally:
            self._release_active_job(job_id)

    @staticmethod
    def _target_video_path(job: dict, target: str) -> str:
        variants = deserialize_plan(job.get("platform_variants_json"))
        target_state = variants.get(target) if isinstance(variants, dict) else None
        if isinstance(target_state, dict) and target_state.get("status") == "ready":
            path = str(target_state.get("path") or "")
            if path and os.path.isfile(path):
                return path
        return ""

    def _publish_x(self, job: dict, token: str) -> str:
        video_path = job["local_video_path"]
        total_bytes = os.path.getsize(video_path)
        media_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
        headers = {"Authorization": f"Bearer {token}"}
        init = requests.post(
            "https://api.x.com/2/media/upload",
            headers=headers,
            files={
                "command": (None, "INIT"),
                "media_type": (None, media_type),
                "total_bytes": (None, str(total_bytes)),
                "media_category": (None, "tweet_video"),
            },
            timeout=60,
        )
        init.raise_for_status()
        media_id = str((init.json().get("data") or {}).get("id") or "")
        if not media_id:
            raise RuntimeError("X 未返回 media_id")
        chunk_size = 4 * 1024 * 1024
        with open(video_path, "rb") as handle:
            index = 0
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                response = requests.post(
                    "https://api.x.com/2/media/upload",
                    headers=headers,
                    files={
                        "command": (None, "APPEND"),
                        "media_id": (None, media_id),
                        "segment_index": (None, str(index)),
                        "media": ("chunk", chunk, "application/octet-stream"),
                    },
                    timeout=120,
                )
                response.raise_for_status()
                index += 1
        final = requests.post(
            "https://api.x.com/2/media/upload",
            headers=headers,
            files={"command": (None, "FINALIZE"), "media_id": (None, media_id)},
            timeout=60,
        )
        final.raise_for_status()
        processing = (final.json().get("data") or {}).get("processing_info") or {}
        for _ in range(60):
            state = processing.get("state")
            if state in (None, "succeeded"):
                break
            if state == "failed":
                raise RuntimeError(_safe_error(processing.get("error") or "X 视频处理失败"))
            time.sleep(max(1, min(10, int(processing.get("check_after_secs") or 2))))
            status = requests.get(
                "https://api.x.com/2/media/upload",
                headers=headers,
                params={"command": "STATUS", "media_id": media_id},
                timeout=30,
            )
            status.raise_for_status()
            processing = (status.json().get("data") or {}).get("processing_info") or {}
        else:
            raise RuntimeError("X 视频处理超时")
        text = str(job.get("x_text") or job.get("title") or "新视频").strip()[:260]
        post = requests.post(
            "https://api.x.com/2/tweets",
            headers={**headers, "Content-Type": "application/json"},
            json={"text": text, "media": {"media_ids": [media_id]}},
            timeout=60,
        )
        post.raise_for_status()
        post_id = str((post.json().get("data") or {}).get("id") or "")
        if not post_id:
            raise RuntimeError("X 发布成功响应缺少帖子ID")
        return post_id

    def _publish_youtube(self, job: dict, token_path: str) -> str:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except Exception as exc:
            raise RuntimeError("YouTube发布依赖未安装") from exc
        credentials = Credentials.from_authorized_user_file(
            token_path,
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
        )
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
            Path(token_path).write_text(credentials.to_json(), encoding="utf-8")
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        body = {
            "snippet": {
                "title": str(job.get("youtube_title") or job.get("title") or "新视频")[:100],
                "description": str(
                    job.get("youtube_description") or job.get("description") or ""
                )[:5000],
                "categoryId": str(self._config().get("TRANSFER_YOUTUBE_CATEGORY_ID") or "22"),
            },
            "status": {
                "privacyStatus": str(self._config().get("TRANSFER_YOUTUBE_PRIVACY") or "private"),
                "selfDeclaredMadeForKids": False,
            },
        }
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(job["local_video_path"], chunksize=8 * 1024 * 1024, resumable=True),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        video_id = str((response or {}).get("id") or "")
        if not video_id:
            raise RuntimeError("YouTube 发布响应缺少视频ID")
        return video_id

    def publish_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        if len(str(job.get("source_attribution") or "").strip()) < 2:
            raise ValueError("来源标识尚未填写，禁止发布")
        if str(job.get("recreation_status") or "") != "approved":
            raise ValueError("当前视频尚未人工确认，禁止发布")
        if str(job.get("watermark_status") or "") not in {
            "none",
            "own_brand",
            "third_party_preserved",
            "platform_overlay_removed",
        }:
            raise ValueError("作者名、来源标识和平台浮层尚未核对，禁止发布")
        if not job.get("local_video_path") or not os.path.isfile(job["local_video_path"]):
            raise ValueError("视频尚未准备完成")
        targets = _json_list(job["target_platforms"])
        config = self._config()
        self._update_job(
            job_id,
            status=JOB_STATUSES["PUBLISHING"],
            next_retry_at=None,
            last_retry_stage="publish",
            error_message="",
        )
        errors = []
        retryable_errors = []
        x_post_id = job.get("x_post_id") or ""
        youtube_video_id = job.get("youtube_video_id") or ""
        if "x" in targets and not x_post_id:
            x_path = self._target_video_path(job, "x")
            if not x_path:
                self._update_job(job_id, x_publish_status="blocked")
                errors.append("X 媒体版本未就绪，请先完成原创剪辑或重新体检")
            else:
                self._update_job(job_id, x_publish_status="manual_ready")
        if "youtube" in targets and not youtube_video_id:
            youtube_path = self._target_video_path(job, "youtube")
            token_path = os.path.join(get_app_subdir("config"), "youtube_transfer_token.json")
            if not youtube_path:
                self._update_job(job_id, youtube_publish_status="blocked")
                errors.append("YouTube媒体版本未就绪")
            elif not os.path.isfile(token_path):
                self._update_job(job_id, youtube_publish_status="waiting_auth")
                errors.append("YouTube 尚未授权")
            else:
                youtube_attempts = int(job.get("youtube_publish_attempts") or 0) + 1
                self._update_job(
                    job_id,
                    youtube_publish_status="publishing",
                    youtube_publish_attempts=youtube_attempts,
                )
                try:
                    youtube_job = {**job, "local_video_path": youtube_path}
                    youtube_video_id = self._publish_youtube(youtube_job, token_path)
                    self._update_job(
                        job_id,
                        youtube_video_id=youtube_video_id,
                        youtube_publish_status="completed",
                    )
                except Exception as exc:
                    message = f"YouTube: {_safe_error(exc)}"
                    self._update_job(job_id, youtube_publish_status="failed")
                    errors.append(message)
                    retryable_errors.append((message, youtube_attempts))
        completed = all(
            (target == "x" and x_post_id) or (target == "youtube" and youtube_video_id)
            for target in targets
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["COMPLETED"] if completed else JOB_STATUSES["READY"],
            error_message="；".join(errors),
            next_retry_at=None,
            last_retry_stage="",
        )
        if retryable_errors and not completed:
            max_attempts = max(item[1] for item in retryable_errors)
            self._schedule_retry(
                job_id,
                "publish",
                max_attempts,
                "；".join(item[0] for item in retryable_errors),
            )
        return self.get_job(job_id) or {}

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._scheduler and self._scheduler.running:
            return
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._scheduler.add_job(
            self.scan_due_rules,
            "interval",
            seconds=60,
            id="transfer-center-scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        self._scheduler.add_job(
            self.retry_due_jobs,
            "interval",
            seconds=60,
            id="transfer-center-retry",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        self._scheduler.start()
        logger.info("搬运中心调度器已启动")

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)


_global_transfer_center: TransferCenter | None = None
_global_lock = threading.Lock()


def get_transfer_center(config_provider: Callable[[], dict] | None = None) -> TransferCenter:
    global _global_transfer_center
    with _global_lock:
        if _global_transfer_center is None:
            _global_transfer_center = TransferCenter(config_provider=config_provider)
        elif config_provider is not None:
            _global_transfer_center._config_provider = config_provider
        return _global_transfer_center
