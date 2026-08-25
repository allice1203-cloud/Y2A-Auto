#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Cross-platform discovery, download and publishing for the transfer center.

This module deliberately lives beside the legacy YouTube -> AcFun/Bilibili
pipeline.  The old task schema is tightly coupled to YouTube and changing it
would make a working workflow unnecessarily risky.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import http.cookiejar
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests

from .content_recreation import (
    deserialize_plan,
    generate_recreation_plan,
    serialize_plan,
    validate_review_payload,
)
from .douyin_downloader import DouyinDownloadError, download_douyin_video
from .media_preflight import build_distribution_plan, prepare_platform_variants
from .utils import get_app_subdir
from .video_intelligence import (
    find_local_cover,
    run_content_preflight,
    run_cover_preflight,
)


logger = logging.getLogger("transfer_center")

PLATFORM_CATALOG = {
    "bilibili": {"label": "B站", "source": True, "target": True, "discovery": True, "publish_mode": "server"},
    "douyin": {"label": "国内抖音", "source": True, "target": True, "discovery": True, "publish_mode": "oauth_optional"},
    "tiktok": {"label": "TikTok", "source": True, "target": True, "discovery": True, "publish_mode": "manual"},
    "youtube": {"label": "YouTube", "source": True, "target": True, "discovery": False, "publish_mode": "oauth"},
    "x": {"label": "X", "source": False, "target": True, "discovery": False, "publish_mode": "manual"},
    "web": {"label": "其他网站", "source": True, "target": False, "discovery": False, "publish_mode": "download_only"},
}
DISCOVERY_PLATFORMS = {key for key, value in PLATFORM_CATALOG.items() if value["discovery"]}
SOURCE_PLATFORMS = {key for key, value in PLATFORM_CATALOG.items() if value["source"]}
# Bilibili/Douyin CDN traffic is reachable directly from the production host. TikTok is
# intentionally excluded so it can use the existing egress proxy on hosts
# where direct access is blocked or intermittently throttled.
DIRECT_SOURCE_PLATFORMS = {"bilibili", "douyin"}
DISCOVERY_MODES = {"account", "keyword", "manual"}
TARGETS = {key for key, value in PLATFORM_CATALOG.items() if value["target"]}
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
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_x_web_intent_url(text: str) -> str:
    """Build the free X web composer URL; media is attached manually."""
    normalized = str(text or "").strip()[:280]
    return "https://x.com/intent/post?" + urlencode({"text": normalized})


def build_youtube_oauth_redirect_uri(
    public_base_url: str = "",
    *,
    request_host: str = "",
    request_scheme: str = "http",
) -> str:
    """Build one stable callback URI for OAuth registration and token exchange."""
    base_url = str(public_base_url or "").strip().rstrip("/")
    if not base_url:
        host = str(request_host or "").strip().split(",", 1)[0].strip()
        scheme = str(request_scheme or "http").strip().split(",", 1)[0].strip()
        if not host:
            raise ValueError("无法确定 YouTube OAuth 公网回调地址")
        if host.split(":", 1)[0].lower() == "transfer.sg99.online":
            scheme = "https"
        base_url = f"{scheme}://{host}"
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("YouTube OAuth 公网地址格式无效")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("YouTube OAuth 公网地址必须使用 HTTPS")
    return f"{base_url}/transfer-center/youtube/callback"


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


def _validate_public_source_url(value: str) -> str:
    """Allow yt-dlp to fetch public web URLs while rejecting local networks."""
    url = _normalize_source_url(value)
    parsed = urlparse(url)
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("请输入完整的 http 或 https 视频链接")
    if parsed.username or parsed.password:
        raise ValueError("视频链接不能包含账号或密码")
    if (
        hostname in {"localhost", "localhost.localdomain"}
        or hostname.endswith((".local", ".internal", ".localhost"))
    ):
        raise ValueError("不允许下载本机或内网地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("不允许下载本机或内网地址")
    return url


def _detect_platform(url: str) -> str:
    parsed = urlparse(_normalize_source_url(url))
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if hostname == "b23.tv" or hostname.endswith(".b23.tv") or (
        hostname == "bilibili.com" or hostname.endswith(".bilibili.com")
    ):
        return "bilibili"
    if hostname == "douyin.com" or hostname.endswith(".douyin.com"):
        return "douyin"
    if hostname in {"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"} or hostname.endswith(
        ".tiktok.com"
    ):
        return "tiktok"
    if (
        hostname == "youtu.be"
        or hostname.endswith(".youtu.be")
        or hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
    ):
        return "youtube"
    return "web" if parsed.scheme in {"http", "https"} and parsed.hostname else ""


def _source_direct_env(platform: str) -> dict[str, str]:
    """Keep Chinese source/CDN traffic off the YouTube proxy bridge."""
    env = os.environ.copy()
    if platform not in DIRECT_SOURCE_PLATFORMS:
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


def _friendly_youtube_publish_error(value: Any) -> tuple[str, bool, bool]:
    """Return a user-facing message, retryability, and reconnect requirement."""
    message = _safe_error(value)
    lowered = message.lower()
    if "youtubesignuprequired" in lowered:
        return (
            "当前 Google 授权没有关联可上传的 YouTube 频道，请重新连接并选择拥有频道的账号",
            False,
            True,
        )
    if any(
        marker in lowered
        for marker in (
            "invalid_grant",
            "authorizationrequired",
            "invalid credentials",
            "token has been expired",
            "unauthorized_client",
        )
    ):
        return ("YouTube 授权已失效，请重新连接频道", False, True)
    if any(
        marker in lowered
        for marker in (
            "quotaexceeded",
            "uploadlimitexceeded",
            "dailylimitexceeded",
        )
    ):
        return ("YouTube 当日上传或 API 额度已用完，请稍后人工重试", False, False)
    retryable = any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "backenderror",
            "internalerror",
            "ratelimitexceeded",
            " 429 ",
            " 500 ",
            " 502 ",
            " 503 ",
            " 504 ",
        )
    )
    return (message or "YouTube 发布失败", retryable, False)


def _youtube_connection_paths() -> tuple[Path, Path]:
    config_dir = Path(get_app_subdir("config"))
    return (
        config_dir / "youtube_transfer_token.json",
        config_dir / "youtube_transfer_channel.json",
    )


def youtube_connection_state() -> dict[str, Any]:
    """Read the last verified YouTube channel without making a network call."""
    token_path, channel_path = _youtube_connection_paths()
    if not token_path.is_file():
        return {"status": "disconnected", "connected": False}
    try:
        token_payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "status": "reconnect_required",
            "connected": False,
            "message": "授权文件无法读取，请重新连接",
        }
    scopes = {
        str(scope).strip()
        for scope in (token_payload.get("scopes") or [])
        if str(scope).strip()
    }
    if not set(YOUTUBE_SCOPES).issubset(scopes):
        return {
            "status": "reconnect_required",
            "connected": False,
            "message": "需要重新验证实际 YouTube 频道",
        }
    try:
        channel_payload = json.loads(channel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        channel_payload = {}
    if not str(channel_payload.get("channel_id") or "").strip():
        return {
            "status": "reconnect_required",
            "connected": False,
            "message": "尚未验证实际 YouTube 频道",
        }
    return {
        "status": "connected",
        "connected": True,
        "channel_id": str(channel_payload.get("channel_id") or ""),
        "channel_title": str(channel_payload.get("channel_title") or ""),
        "long_uploads_status": str(channel_payload.get("long_uploads_status") or ""),
        "verified_at": str(channel_payload.get("verified_at") or ""),
    }


def verify_youtube_credentials(credentials: Any) -> dict[str, str]:
    """Verify that OAuth credentials resolve to an upload-capable channel."""
    try:
        from googleapiclient.discovery import build
    except Exception as exc:
        raise RuntimeError("YouTube发布依赖未安装") from exc
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    try:
        response = (
            youtube.channels()
            .list(part="id,snippet,status", mine=True)
            .execute()
        )
    except Exception as exc:
        friendly, _, reconnect_required = _friendly_youtube_publish_error(exc)
        if reconnect_required:
            raise ValueError(friendly) from exc
        raise
    items = response.get("items") or []
    if not items:
        raise ValueError("当前 Google 账号没有可用的 YouTube 频道")
    channel = items[0]
    channel_id = str(channel.get("id") or "").strip()
    if not channel_id:
        raise ValueError("YouTube 没有返回有效频道信息")
    return {
        "channel_id": channel_id,
        "channel_title": str((channel.get("snippet") or {}).get("title") or "未命名频道"),
        "long_uploads_status": str(
            (channel.get("status") or {}).get("longUploadsStatus") or ""
        ),
    }


def save_youtube_connection(credentials: Any, channel: dict[str, str]) -> None:
    """Persist OAuth credentials only after a real channel was verified."""
    token_path, channel_path = _youtube_connection_paths()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    channel_payload = {
        **channel,
        "verified_at": _utc_now(),
    }
    token_tmp = token_path.with_suffix(".json.tmp")
    channel_tmp = channel_path.with_suffix(".json.tmp")
    token_tmp.write_text(credentials.to_json(), encoding="utf-8")
    channel_tmp.write_text(
        json.dumps(channel_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(token_tmp, 0o600)
    os.chmod(channel_tmp, 0o600)
    os.replace(token_tmp, token_path)
    os.replace(channel_tmp, channel_path)


def invalidate_youtube_connection() -> None:
    """Keep OAuth credentials for diagnosis but remove the verified badge."""
    _, channel_path = _youtube_connection_paths()
    try:
        channel_path.unlink()
    except FileNotFoundError:
        pass


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

                CREATE TABLE IF NOT EXISTS transfer_source_allowlist (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_url TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    rights_basis TEXT NOT NULL DEFAULT 'authorized',
                    rights_note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, account_url)
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
                    content_preflight_json TEXT DEFAULT '{}',
                    cover_preflight_json TEXT DEFAULT '{}',
                    rights_basis TEXT DEFAULT 'unconfirmed',
                    rights_note TEXT DEFAULT '',
                    source_attribution TEXT DEFAULT '',
                    watermark_status TEXT DEFAULT 'unreviewed',
                    watermark_note TEXT DEFAULT '',
                    recreation_mode TEXT DEFAULT 'commentary',
                    processing_mode TEXT DEFAULT 'professional',
                    recreation_status TEXT DEFAULT 'pending',
                    recreation_plan_json TEXT DEFAULT '{}',
                    original_angle TEXT DEFAULT '',
                    original_contribution TEXT DEFAULT '',
                    commentary_script TEXT DEFAULT '',
                    x_text TEXT DEFAULT '',
                    youtube_title TEXT DEFAULT '',
                    youtube_description TEXT DEFAULT '',
                    bilibili_title TEXT DEFAULT '',
                    bilibili_description TEXT DEFAULT '',
                    bilibili_partition_id TEXT DEFAULT '',
                    douyin_text TEXT DEFAULT '',
                    tiktok_text TEXT DEFAULT '',
                    reviewed_at TEXT,
                    recreation_completed INTEGER NOT NULL DEFAULT 0,
                    mpt_project_id TEXT DEFAULT '',
                    mpt_asset_id TEXT DEFAULT '',
                    mpt_task_id TEXT DEFAULT '',
                    mpt_render_id TEXT DEFAULT '',
                    mpt_status TEXT DEFAULT '',
                    mpt_message TEXT DEFAULT '',
                    mpt_workflow TEXT DEFAULT '',
                    media_cleaned_at TEXT,
                    prepare_attempts INTEGER NOT NULL DEFAULT 0,
                    x_publish_status TEXT DEFAULT 'pending',
                    youtube_publish_status TEXT DEFAULT 'pending',
                    bilibili_publish_status TEXT DEFAULT 'pending',
                    douyin_publish_status TEXT DEFAULT 'pending',
                    tiktok_publish_status TEXT DEFAULT 'pending',
                    x_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    youtube_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    bilibili_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    tiktok_publish_attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_retry_stage TEXT DEFAULT '',
                    x_post_id TEXT DEFAULT '',
                    youtube_video_id TEXT DEFAULT '',
                    bilibili_post_id TEXT DEFAULT '',
                    douyin_post_id TEXT DEFAULT '',
                    tiktok_post_id TEXT DEFAULT '',
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
                CREATE INDEX IF NOT EXISTS idx_transfer_allowlist_platform
                    ON transfer_source_allowlist(platform, enabled, updated_at DESC);

                CREATE TABLE IF NOT EXISTS transfer_metrics (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    views INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    comments INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    followers_delta INTEGER NOT NULL DEFAULT 0,
                    note TEXT DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES transfer_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_metrics_job_platform
                    ON transfer_metrics(job_id, platform, recorded_at DESC);

                CREATE TABLE IF NOT EXISTS transfer_candidates (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    uploader TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    heat_score INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT DEFAULT '{}',
                    source_label TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    job_id TEXT DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_candidates_status
                    ON transfer_candidates(status, heat_score DESC, updated_at DESC);
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
            "content_preflight_json": "TEXT DEFAULT '{}'",
            "cover_preflight_json": "TEXT DEFAULT '{}'",
            "rights_basis": "TEXT DEFAULT 'unconfirmed'",
            "rights_note": "TEXT DEFAULT ''",
            "source_attribution": "TEXT DEFAULT ''",
            "watermark_status": "TEXT DEFAULT 'unreviewed'",
            "watermark_note": "TEXT DEFAULT ''",
            "recreation_mode": "TEXT DEFAULT 'commentary'",
            "processing_mode": "TEXT DEFAULT 'professional'",
            "recreation_status": "TEXT DEFAULT 'pending'",
            "recreation_plan_json": "TEXT DEFAULT '{}'",
            "original_angle": "TEXT DEFAULT ''",
            "original_contribution": "TEXT DEFAULT ''",
            "commentary_script": "TEXT DEFAULT ''",
            "x_text": "TEXT DEFAULT ''",
            "youtube_title": "TEXT DEFAULT ''",
            "youtube_description": "TEXT DEFAULT ''",
            "bilibili_title": "TEXT DEFAULT ''",
            "bilibili_description": "TEXT DEFAULT ''",
            "bilibili_partition_id": "TEXT DEFAULT ''",
            "douyin_text": "TEXT DEFAULT ''",
            "tiktok_text": "TEXT DEFAULT ''",
            "reviewed_at": "TEXT",
            "recreation_completed": "INTEGER NOT NULL DEFAULT 0",
            "mpt_project_id": "TEXT DEFAULT ''",
            "mpt_asset_id": "TEXT DEFAULT ''",
            "mpt_task_id": "TEXT DEFAULT ''",
            "mpt_render_id": "TEXT DEFAULT ''",
            "mpt_status": "TEXT DEFAULT ''",
            "mpt_message": "TEXT DEFAULT ''",
            "mpt_workflow": "TEXT DEFAULT ''",
            "media_cleaned_at": "TEXT",
            "prepare_attempts": "INTEGER NOT NULL DEFAULT 0",
            "x_publish_status": "TEXT DEFAULT 'pending'",
            "youtube_publish_status": "TEXT DEFAULT 'pending'",
            "bilibili_publish_status": "TEXT DEFAULT 'pending'",
            "douyin_publish_status": "TEXT DEFAULT 'pending'",
            "tiktok_publish_status": "TEXT DEFAULT 'pending'",
            "x_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "youtube_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "bilibili_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "tiktok_publish_attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "TEXT",
            "last_retry_stage": "TEXT DEFAULT ''",
            "progress_percent": "REAL NOT NULL DEFAULT 0",
            "progress_message": "TEXT DEFAULT ''",
            "bilibili_post_id": "TEXT DEFAULT ''",
            "douyin_post_id": "TEXT DEFAULT ''",
            "tiktok_post_id": "TEXT DEFAULT ''",
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
        conn.execute(
            """
            UPDATE transfer_jobs
            SET progress_message = '素材已就绪，等待选择处理方式'
            WHERE progress_message = '素材已就绪，等待再创作审核'
            """
        )

    def _config(self) -> dict:
        config = self._config_provider()
        return dict(config) if isinstance(config, dict) else {}

    # ---- rules ---------------------------------------------------------
    def list_allowed_sources(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transfer_source_allowlist "
                "ORDER BY enabled DESC, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalize_account_url(platform: str, value: str) -> str:
        url = _normalize_source_url(value).rstrip("/")
        parsed = urlparse(url)
        host = str(parsed.hostname or "").lower()
        if platform == "bilibili" and not (
            host == "bilibili.com" or host.endswith(".bilibili.com")
        ):
            raise ValueError("B站授权来源需要填写个人空间链接")
        if platform == "douyin" and not (
            host == "douyin.com" or host.endswith(".douyin.com")
        ):
            raise ValueError("抖音授权来源需要填写公开主页链接")
        if platform == "tiktok" and not (
            host == "tiktok.com" or host.endswith(".tiktok.com")
        ):
            raise ValueError("TikTok授权来源需要填写公开主页链接")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("授权来源链接格式无效")
        return url

    def save_allowed_source(self, payload: dict) -> str:
        platform = str(payload.get("platform") or "").strip().lower()
        if platform not in DISCOVERY_PLATFORMS:
            raise ValueError("授权来源平台无效")
        account_url = self._normalize_account_url(
            platform, str(payload.get("account_url") or "").strip()
        )
        display_name = str(payload.get("display_name") or "").strip()[:120]
        rights_basis = str(payload.get("rights_basis") or "unconfirmed").strip().lower()
        if rights_basis not in {
            "unconfirmed",
            "owned",
            "authorized",
            "licensed",
            "public_domain",
        }:
            raise ValueError("请选择有效的来源状态")
        rights_note = str(payload.get("rights_note") or "").strip()[:1000]
        now = _utc_now()
        source_id = str(payload.get("id") or uuid.uuid4())
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM transfer_source_allowlist "
                "WHERE platform=? AND account_url=?",
                (platform, account_url),
            ).fetchone()
            if existing:
                source_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE transfer_source_allowlist
                    SET display_name=?, rights_basis=?, rights_note=?,
                        enabled=?, updated_at=? WHERE id=?
                    """,
                    (
                        display_name,
                        rights_basis,
                        rights_note,
                        int(_as_bool(payload.get("enabled", False))),
                        now,
                        source_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO transfer_source_allowlist (
                        id, platform, account_url, display_name, rights_basis,
                        rights_note, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        platform,
                        account_url,
                        display_name,
                        rights_basis,
                        rights_note,
                        int(_as_bool(payload.get("enabled", False))),
                        now,
                        now,
                    ),
                )
        return source_id

    def delete_allowed_source(self, source_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM transfer_source_allowlist WHERE id=?", (source_id,)
            )
        return cursor.rowcount > 0

    def _allowed_source_for(self, platform: str, source_value: str) -> dict | None:
        try:
            normalized = self._normalize_account_url(platform, source_value)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_source_allowlist "
                "WHERE platform=? AND account_url=? AND enabled=1",
                (platform, normalized),
            ).fetchone()
        return dict(row) if row else None

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
        if platform not in DISCOVERY_PLATFORMS:
            raise ValueError("来源平台无效")
        if mode not in DISCOVERY_MODES - {"manual"}:
            raise ValueError("发现方式无效")
        if not source_value:
            raise ValueError("账号链接或关键词不能为空")
        if not targets:
            raise ValueError("至少选择一个发布平台")
        if platform == "douyin" and mode == "account" and "douyin.com" not in source_value.lower():
            raise ValueError("抖音账号监控需要填写公开主页链接")
        if platform == "tiktok" and mode == "account" and "tiktok.com/@" not in source_value.lower():
            raise ValueError("TikTok 账号监控需要填写公开主页链接")
        if platform == "bilibili" and mode == "account" and not any(
            host in source_value.lower() for host in ("bilibili.com", "b23.tv")
        ):
            raise ValueError("B站账号监控需要填写个人空间链接")
        allowed_source = (
            self._allowed_source_for(platform, source_value)
            if mode == "account"
            else None
        )
        requested_auto_prepare = _as_bool(payload.get("auto_prepare", True))
        if mode == "account" and requested_auto_prepare and not allowed_source:
            raise ValueError("自动下载前，请先把该账号加入并启用来源跟踪列表")
        if mode == "keyword":
            requested_auto_prepare = False
        recreation_mode = str(payload.get("recreation_mode") or "commentary").strip().lower()
        if recreation_mode not in {
            "commentary",
            "localized",
            "drama_recap",
            "structured_remix",
        }:
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
            "auto_prepare": int(requested_auto_prepare),
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
        settings = {
            "bilibili": (
                "TRANSFER_BILIBILI_COOKIES_PATH",
                "cookies/bilibili_unified_cookies.txt",
            ),
            "douyin": (
                "TRANSFER_DOUYIN_COOKIES_PATH",
                "cookies/douyin_cookies.txt",
            ),
            "youtube": ("YOUTUBE_COOKIES_PATH", "cookies/youtube_cookies.txt"),
            "tiktok": ("TRANSFER_TIKTOK_COOKIES_PATH", "cookies/tiktok_cookies.txt"),
        }
        config_key, default_path = settings.get(platform, ("", ""))
        if not config_key:
            return None
        configured = str(config.get(config_key) or default_path).strip()
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
        if platform in DIRECT_SOURCE_PLATFORMS:
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
        if platform == "bilibili":
            session.headers["Referer"] = "https://www.bilibili.com/"
        elif platform == "douyin":
            session.headers["Referer"] = "https://www.douyin.com/"
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

    def _discover_tiktok_items(self, rule: dict) -> list[dict]:
        source = str(rule.get("source_value") or "").strip()
        if rule.get("discovery_mode") == "keyword":
            source = f"https://www.tiktok.com/tag/{quote(source, safe='')}"
        data = self._yt_dlp_json(source, "tiktok", int(rule["max_items"]), flat=True)
        entries = data.get("entries") if isinstance(data, dict) else []
        items = []
        for entry in entries or [data]:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "").strip()
            url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
            if video_id and not url.startswith("http"):
                uploader = str(entry.get("uploader_id") or entry.get("uploader") or "").strip()
                url = (
                    f"https://www.tiktok.com/@{uploader}/video/{video_id}"
                    if uploader
                    else f"https://www.tiktok.com/video/{video_id}"
                )
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

    def _fetch_bilibili_space_videos(self, mid: str, limit: int) -> list[dict]:
        cookie_path = self._cookie_path("bilibili")
        if not cookie_path:
            raise RuntimeError("B站账号扫描需要有效的统一登录凭证")

        try:
            from .bilibili_auth import load_credential_from_file
            from .bili_sdk.utils.network import Api

            credential = load_credential_from_file(cookie_path)

            async def request_videos():
                return await Api(
                    url="https://api.bilibili.com/x/space/wbi/arc/search",
                    method="GET",
                    wbi=True,
                    credential=credential,
                ).update_params(
                    mid=mid,
                    pn=1,
                    ps=max(1, min(50, int(limit))),
                    order="pubdate",
                ).result

            data = asyncio.run(request_videos()) or {}
        except Exception as exc:
            logger.warning("B站账号视频列表读取失败: %s", type(exc).__name__)
            raise RuntimeError("B站账号视频列表读取失败，请刷新 B站登录后重试") from exc

        videos = ((data.get("list") or {}).get("vlist") or []) if isinstance(data, dict) else []
        return [item for item in videos if isinstance(item, dict)]

    @staticmethod
    def _bilibili_duration_seconds(value: Any) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parts = [int(part) for part in text.split(":")]
        except ValueError:
            return None
        if not parts or len(parts) > 3:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    def _discover_bilibili_account_items(self, rule: dict) -> list[dict]:
        source = str(rule.get("source_value") or "").strip()
        parsed = urlparse(source)
        path_parts = [part for part in parsed.path.split("/") if part]
        mid = path_parts[0] if path_parts else ""
        if not mid.isdigit():
            raise RuntimeError("B站账号主页链接缺少有效 UID")

        videos = self._fetch_bilibili_space_videos(mid, int(rule["max_items"]))
        items = []
        for video in videos:
            bvid = str(video.get("bvid") or "").strip()
            if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                continue
            items.append(
                {
                    "id": bvid,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "title": str(video.get("title") or ""),
                    "uploader": str(video.get("author") or ""),
                    "description": str(video.get("description") or ""),
                    "thumbnail": str(video.get("pic") or ""),
                    "duration": self._bilibili_duration_seconds(video.get("length")),
                    "timestamp": video.get("created"),
                }
            )
        return items

    def _discover_items(self, rule: dict) -> list[dict]:
        if rule["platform"] == "bilibili":
            if rule["discovery_mode"] == "account":
                return self._discover_bilibili_account_items(rule)
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
        if rule["platform"] == "tiktok":
            return self._discover_tiktok_items(rule)
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
                        recreation_mode, processing_mode, x_publish_status, youtube_publish_status,
                        bilibili_publish_status, douyin_publish_status, tiktok_publish_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        "professional",
                        "pending" if "x" in targets else "skipped",
                        "pending" if "youtube" in targets else "skipped",
                        "pending" if "bilibili" in targets else "skipped",
                        "pending" if "douyin" in targets else "skipped",
                        "pending" if "tiktok" in targets else "skipped",
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
            allowed_source = None
            if rule.get("discovery_mode") == "account":
                allowed_source = self._allowed_source_for(
                    str(rule.get("platform") or ""),
                    str(rule.get("source_value") or ""),
                )
                if rule.get("auto_prepare") and not allowed_source:
                    raise ValueError(
                        "来源跟踪列表已停用或不存在；已阻止自动下载"
                    )
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
                    if allowed_source:
                        self._update_job(
                            job_id,
                            rights_basis=str(
                                allowed_source.get("rights_basis") or "authorized"
                            ),
                            rights_note=str(
                                allowed_source.get("rights_note") or ""
                            ),
                        )
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
            self._mark_rule_scan(
                rule_id,
                "success",
                message,
                complete_first_scan=bool(candidates),
            )
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

    def _mark_rule_scan(
        self,
        rule_id: str,
        status: str,
        message: str,
        *,
        complete_first_scan: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE transfer_rules
                SET last_scan_at=?, last_scan_status=?, last_scan_message=?,
                    first_scan_completed=CASE WHEN ?=1 THEN 1 ELSE first_scan_completed END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    _utc_now(),
                    status,
                    _safe_error(message),
                    int(bool(complete_first_scan)),
                    _utc_now(),
                    rule_id,
                ),
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

    def run_maintenance(self) -> dict[str, Any]:
        """备份任务数据库，并只清理超期的已完成任务媒体。"""
        config = self._config()
        if not _as_bool(config.get("TRANSFER_MAINTENANCE_ENABLED", True)):
            return {"enabled": False, "backup": "", "cleaned_jobs": 0, "bytes_freed": 0}
        backup_path = self.backup_database()
        retention_days = max(
            7, min(3650, int(config.get("TRANSFER_COMPLETED_MEDIA_RETENTION_DAYS") or 30))
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM transfer_jobs
                WHERE status='completed' AND updated_at < ?
                  AND (media_cleaned_at IS NULL OR media_cleaned_at='')
                ORDER BY updated_at ASC LIMIT 100
                """,
                (cutoff,),
            ).fetchall()
        cleaned = 0
        bytes_freed = 0
        root = Path(get_app_subdir("downloads")) / "transfer"
        resolved_root = root.resolve()
        for row in rows:
            job_id = str(row["id"])
            candidate = root / job_id
            try:
                resolved = candidate.resolve()
                if resolved.parent != resolved_root or not resolved.is_dir():
                    self._update_job(job_id, media_cleaned_at=_utc_now())
                    continue
                size = sum(
                    item.stat().st_size
                    for item in resolved.rglob("*")
                    if item.is_file()
                )
                shutil.rmtree(resolved)
                cleaned += 1
                bytes_freed += size
                self._update_job(
                    job_id,
                    local_video_path="",
                    original_video_path="",
                    recreated_media_path="",
                    local_metadata_path="",
                    platform_variants_json="{}",
                    media_cleaned_at=_utc_now(),
                    progress_message="已完成发布，超期本地媒体已安全清理",
                )
            except OSError:
                logger.exception("清理已完成搬运任务失败: %s", job_id)
        return {
            "enabled": True,
            "backup": backup_path,
            "cleaned_jobs": cleaned,
            "bytes_freed": bytes_freed,
        }

    def backup_database(self) -> str:
        backup_dir = Path(get_app_subdir("backups")) / "transfer-center"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"transfer-center-{timestamp}.db"
        temp_path = target.with_suffix(".db.tmp")
        source = self._connect()
        destination = sqlite3.connect(temp_path)
        try:
            source.backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError("搬运数据库备份完整性校验失败")
        finally:
            destination.close()
            source.close()
        os.replace(temp_path, target)
        retention_days = max(
            7,
            min(
                3650,
                int(self._config().get("TRANSFER_DB_BACKUP_RETENTION_DAYS") or 14),
            ),
        )
        cutoff = time.time() - retention_days * 86400
        for old_backup in backup_dir.glob("transfer-center-*.db"):
            if old_backup != target and old_backup.stat().st_mtime < cutoff:
                old_backup.unlink()
        return str(target)

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_published_jobs(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transfer_jobs
                WHERE x_post_id<>'' OR youtube_video_id<>'' OR bilibili_post_id<>''
                   OR douyin_post_id<>'' OR tiktok_post_id<>''
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_performance(self, job_id: str, payload: dict[str, Any]) -> str:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        platform = str(payload.get("platform") or "").strip().lower()
        post_fields = {
            "x": "x_post_id",
            "youtube": "youtube_video_id",
            "bilibili": "bilibili_post_id",
            "douyin": "douyin_post_id",
            "tiktok": "tiktok_post_id",
        }
        if platform not in post_fields:
            raise ValueError("效果数据平台无效")
        if not str(job.get(post_fields[platform]) or "").strip():
            raise ValueError("该平台尚无已发布作品，不能录入效果")

        def metric(name: str, *, signed: bool = False) -> int:
            try:
                value = int(str(payload.get(name) or "0").strip())
            except ValueError as exc:
                raise ValueError("效果数据必须是整数") from exc
            if not signed and value < 0:
                raise ValueError("播放、点赞、评论和分享不能为负数")
            return value

        record_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transfer_metrics (
                    id, job_id, platform, views, likes, comments, shares,
                    followers_delta, note, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    job_id,
                    platform,
                    metric("views"),
                    metric("likes"),
                    metric("comments"),
                    metric("shares"),
                    metric("followers_delta", signed=True),
                    str(payload.get("note") or "").strip()[:500],
                    _utc_now(),
                ),
            )
        return record_id

    def get_performance_summary(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(365, int(days)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT m.*, ROW_NUMBER() OVER (
                        PARTITION BY m.job_id, m.platform
                        ORDER BY m.recorded_at DESC, m.rowid DESC
                    ) AS rank_no
                    FROM transfer_metrics m WHERE m.recorded_at >= ?
                )
                SELECT r.*, j.title, j.source_platform, j.recreation_mode
                FROM ranked r JOIN transfer_jobs j ON j.id=r.job_id
                WHERE r.rank_no=1 ORDER BY r.views DESC
                """,
                (cutoff,),
            ).fetchall()
        records = [dict(row) for row in rows]
        totals = {
            key: sum(int(row.get(key) or 0) for row in records)
            for key in ("views", "likes", "comments", "shares", "followers_delta")
        }
        totals["engagement_rate"] = (
            round(
                100
                * (totals["likes"] + totals["comments"] + totals["shares"])
                / totals["views"],
                2,
            )
            if totals["views"]
            else 0.0
        )
        platform_views: dict[str, int] = {}
        for row in records:
            key = str(row.get("platform") or "")
            platform_views[key] = platform_views.get(key, 0) + int(row.get("views") or 0)
        top_platform = max(platform_views, key=platform_views.get) if platform_views else ""
        suggestions = []
        if not records:
            suggestions.append("发布后录入一次播放、点赞、评论和分享，系统才能形成对比建议。")
        else:
            if top_platform:
                suggestions.append(
                    f"近 {days} 天播放贡献最高的平台是 {PLATFORM_CATALOG.get(top_platform, {}).get('label', top_platform)}，下一轮优先复用其选题和画幅策略。"
                )
            if totals["engagement_rate"] >= 5:
                suggestions.append("综合互动率较高，保留当前原创解说密度和开场钩子。")
            else:
                suggestions.append("互动率偏低，下一轮将开场结论前置，并在结尾增加一个具体问题。")
            if totals["shares"] < totals["comments"]:
                suggestions.append("转发弱于评论，可增加清单、步骤或可保存的结论卡。")
        return {
            "days": days,
            "records": records,
            "totals": totals,
            "top_platform": top_platform,
            "suggestions": suggestions,
        }

    # ---- hot candidate pool ------------------------------------------
    def _upsert_candidate(self, item: dict[str, Any]) -> str:
        platform = str(item.get("platform") or "").strip().lower()
        source_id = str(item.get("source_id") or "").strip()
        source_url = _validate_public_source_url(str(item.get("source_url") or ""))
        if platform not in SOURCE_PLATFORMS or not source_id:
            raise ValueError("热点候选缺少平台或作品编号")
        candidate_id = hashlib.sha256(f"{platform}:{source_id}".encode()).hexdigest()[:32]
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transfer_candidates (
                    id, platform, source_id, source_url, title, uploader,
                    summary, heat_score, metrics_json, source_label,
                    status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                ON CONFLICT(platform, source_id) DO UPDATE SET
                    source_url=excluded.source_url,
                    title=excluded.title,
                    uploader=excluded.uploader,
                    summary=excluded.summary,
                    heat_score=excluded.heat_score,
                    metrics_json=excluded.metrics_json,
                    source_label=excluded.source_label,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id,
                    platform,
                    source_id,
                    source_url,
                    str(item.get("title") or "").strip()[:500],
                    str(item.get("uploader") or "").strip()[:300],
                    str(item.get("summary") or "").strip()[:2000],
                    max(0, int(item.get("heat_score") or 0)),
                    json.dumps(item.get("metrics") or {}, ensure_ascii=False),
                    str(item.get("source_label") or "公开热榜").strip()[:120],
                    now,
                    now,
                ),
            )
        return candidate_id

    def _fetch_bilibili_hot_candidates(self, limit: int) -> list[dict[str, Any]]:
        session = self._requests_session("bilibili")
        payload: dict[str, Any] = {}
        for endpoint, params in (
            (
                "https://api.bilibili.com/x/web-interface/ranking/v2",
                {"rid": 0, "type": "all"},
            ),
            (
                "https://api.bilibili.com/x/web-interface/popular",
                {"ps": limit, "pn": 1},
            ),
        ):
            response = session.get(endpoint, params=params, timeout=(10, 30))
            response.raise_for_status()
            candidate_payload = response.json()
            if int(candidate_payload.get("code") or 0) == 0:
                payload = candidate_payload
                break
        if not payload:
            raise RuntimeError("B站公开热榜暂时不可用")
        rows = ((payload.get("data") or {}).get("list") or [])[:limit]
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            if not bvid:
                continue
            stat = row.get("stat") if isinstance(row.get("stat"), dict) else {}
            owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
            items.append(
                {
                    "platform": "bilibili",
                    "source_id": bvid,
                    "source_url": f"https://www.bilibili.com/video/{bvid}",
                    "title": str(row.get("title") or ""),
                    "uploader": str(owner.get("name") or ""),
                    "summary": str(row.get("desc") or ""),
                    "heat_score": int(stat.get("view") or 0),
                    "metrics": {
                        "views": int(stat.get("view") or 0),
                        "likes": int(stat.get("like") or 0),
                        "coins": int(stat.get("coin") or 0),
                        "favorites": int(stat.get("favorite") or 0),
                    },
                    "source_label": "B站公开热门榜",
                }
            )
        return items

    def _fetch_douyin_hot_candidates(self, limit: int) -> list[dict[str, Any]]:
        response = self._requests_session("douyin").get(
            "https://www.douyin.com/hot",
            timeout=(10, 30),
        )
        response.raise_for_status()
        text = response.text[:8_000_000]
        ids: list[str] = []
        for pattern in (
            r'"aweme_id"\s*:\s*"(\d{10,30})"',
            r'\\?"awemeId\\?"\s*:\s*\\?"(\d{10,30})',
            r'/video/(\d{10,30})',
        ):
            ids.extend(re.findall(pattern, text))
        items = []
        for video_id in list(dict.fromkeys(ids))[:limit]:
            title = ""
            title_match = re.search(
                rf'"aweme_id"\s*:\s*"{re.escape(video_id)}".{{0,2500}}?"desc"\s*:\s*"([^"\\]{{1,300}})',
                text,
                flags=re.DOTALL,
            )
            if title_match:
                title = html.unescape(title_match.group(1))
            items.append(
                {
                    "platform": "douyin",
                    "source_id": video_id,
                    "source_url": f"https://www.douyin.com/video/{video_id}",
                    "title": title or f"抖音热门视频 {video_id}",
                    "source_label": "抖音公开热点页",
                }
            )
        if not items:
            raise RuntimeError("抖音热点页未返回可识别作品；请刷新抖音登录后重试")
        return items

    def refresh_hot_candidates(self, platform: str = "all", limit: int = 20) -> dict[str, Any]:
        selected = str(platform or "all").strip().lower()
        if selected not in {"all", "bilibili", "douyin"}:
            raise ValueError("当前热点候选平台无效")
        limit = max(1, min(50, int(limit)))
        providers = []
        if selected in {"all", "bilibili"}:
            providers.append(("bilibili", self._fetch_bilibili_hot_candidates))
        if selected in {"all", "douyin"}:
            providers.append(("douyin", self._fetch_douyin_hot_candidates))
        added = 0
        errors = []
        platform_counts = {}
        for name, fetcher in providers:
            try:
                items = fetcher(limit)
                for item in items:
                    self._upsert_candidate(item)
                platform_counts[name] = len(items)
                added += len(items)
            except Exception as exc:
                errors.append(f"{PLATFORM_CATALOG[name]['label']}：{_safe_error(exc)}")
        return {
            "success": added > 0,
            "refreshed": added,
            "platform_counts": platform_counts,
            "errors": errors,
            "message": (
                f"热点候选已刷新 {added} 条"
                + (f"；{'；'.join(errors)}" if errors else "")
            ),
        }

    def list_hot_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transfer_candidates
                WHERE status='candidate'
                ORDER BY heat_score DESC, updated_at DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metrics"] = json.loads(item.get("metrics_json") or "{}")
            except (TypeError, ValueError):
                item["metrics"] = {}
            result.append(item)
        return result

    def promote_hot_candidate(self, candidate_id: str, targets: list[str]) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        if not row:
            raise ValueError("热点候选不存在")
        candidate = dict(row)
        valid_targets = [item for item in targets if item in TARGETS]
        if not valid_targets:
            raise ValueError("至少选择一个发布平台")
        if candidate["platform"] in valid_targets:
            raise ValueError("候选来源不能再次发布到同一平台")
        rule = {
            "id": None,
            "platform": candidate["platform"],
            "target_platforms": json.dumps(valid_targets, ensure_ascii=False),
        }
        job_id, created = self._insert_discovered_job(
            rule,
            {
                "id": candidate["source_id"],
                "url": candidate["source_url"],
                "title": candidate["title"],
                "uploader": candidate["uploader"],
                "description": candidate["summary"],
            },
        )
        if not job_id:
            raise RuntimeError("热点候选无法加入任务")
        with self._connect() as conn:
            conn.execute(
                "UPDATE transfer_candidates SET status='promoted', job_id=?, updated_at=? WHERE id=?",
                (job_id, _utc_now(), candidate_id),
            )
        if not created:
            logger.info("热点候选已关联现有任务 %s", job_id)
        return job_id

    def dismiss_hot_candidate(self, candidate_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE transfer_candidates SET status='dismissed', updated_at=? "
                "WHERE id=? AND status='candidate'",
                (_utc_now(), candidate_id),
            )
        return cursor.rowcount > 0

    # ---- authorized source archive -----------------------------------
    def _archive_source(self, source_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_source_allowlist WHERE id=?", (source_id,)
            ).fetchone()
        if not row:
            raise ValueError("授权来源不存在")
        return dict(row)

    def _archive_rules(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        normalized = self._normalize_account_url(
            str(source.get("platform") or ""), str(source.get("account_url") or "")
        )
        return [
            rule
            for rule in self.list_rules()
            if rule.get("discovery_mode") == "account"
            and rule.get("platform") == source.get("platform")
            and self._normalize_account_url(
                str(rule.get("platform") or ""), str(rule.get("source_value") or "")
            )
            == normalized
        ]

    @staticmethod
    def _archive_subtitle_files(video_path: str) -> list[Path]:
        directory = Path(str(video_path or "")).parent
        if not directory.is_dir():
            return []
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".srt", ".vtt", ".md"}
            and path.name not in {"metadata.json"}
        )

    @staticmethod
    def _subtitle_to_text(path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        result = []
        previous = ""
        for line in lines:
            text = re.sub(r"<[^>]+>", "", line).strip()
            if not text or text == "WEBVTT" or text.isdigit() or "-->" in text:
                continue
            if text != previous:
                result.append(text)
                previous = text
        return "\n".join(result).strip()

    def _ensure_archive_markdown(self, job_id: str) -> str:
        job = self.get_job(job_id)
        video_path = str((job or {}).get("local_video_path") or "")
        if not job or not video_path or not os.path.isfile(video_path):
            return ""
        directory = Path(video_path).parent
        markdown_path = directory / "archive_transcript.md"
        if markdown_path.is_file() and markdown_path.stat().st_size > 40:
            return str(markdown_path)
        subtitle = next(
            (path for path in self._archive_subtitle_files(video_path) if path.suffix.lower() in {".srt", ".vtt"}),
            None,
        )
        if not subtitle:
            return ""
        transcript = self._subtitle_to_text(subtitle)
        if not transcript:
            return ""
        body = (
            f"# {str(job.get('title') or '视频归档')}\n\n"
            f"- 来源：{str(job.get('source_url') or '')}\n"
            f"- 作者：{str(job.get('source_uploader') or '')}\n"
            f"- 作品编号：{str(job.get('source_id') or '')}\n\n"
            f"## Transcript\n\n{transcript}\n"
        )
        temporary = markdown_path.with_suffix(".md.tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(markdown_path)
        return str(markdown_path)

    def build_archive_manifest(self, source_id: str, *, persist: bool = True) -> dict[str, Any]:
        source = self._archive_source(source_id)
        rules = self._archive_rules(source)
        rule_ids = [str(rule["id"]) for rule in rules]
        jobs = []
        if rule_ids:
            placeholders = ",".join("?" for _ in rule_ids)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM transfer_jobs WHERE rule_id IN ({placeholders}) "
                    "ORDER BY created_at ASC",
                    tuple(rule_ids),
                ).fetchall()
            jobs = [dict(row) for row in rows]
        items = []
        for job in jobs:
            transcript_path = self._ensure_archive_markdown(job["id"]) if persist else ""
            if not transcript_path:
                transcript_path = next(
                    (
                        str(path)
                        for path in self._archive_subtitle_files(str(job.get("local_video_path") or ""))
                        if path.suffix.lower() == ".md" and path.stat().st_size > 40
                    ),
                    "",
                )
            video_path = str(job.get("local_video_path") or "")
            items.append(
                {
                    "job_id": job["id"],
                    "source_id": job["source_id"],
                    "source_url": job["source_url"],
                    "title": job.get("title") or "",
                    "status": job.get("status") or "",
                    "video_path": video_path if os.path.isfile(video_path) else "",
                    "transcript_path": transcript_path,
                    "video_ready": bool(video_path and os.path.isfile(video_path)),
                    "transcript_ready": bool(transcript_path),
                    "updated_at": job.get("updated_at") or "",
                }
            )
        manifest = {
            "version": 1,
            "source": {
                "id": source["id"],
                "platform": source["platform"],
                "display_name": source.get("display_name") or "",
                "account_url": source["account_url"],
                "rights_basis": source.get("rights_basis") or "",
                "rights_note": source.get("rights_note") or "",
            },
            "generated_at": _utc_now(),
            "rule_ids": rule_ids,
            "total_items": len(items),
            "video_ready": sum(1 for item in items if item["video_ready"]),
            "transcript_ready": sum(1 for item in items if item["transcript_ready"]),
            "incomplete": sum(
                1 for item in items if not item["video_ready"] or not item["transcript_ready"]
            ),
            "items": items,
        }
        if persist:
            archive_dir = Path(get_app_subdir("archives")) / source["id"]
            archive_dir.mkdir(parents=True, exist_ok=True)
            destination = archive_dir / "manifest.json"
            temporary = archive_dir / "manifest.json.tmp"
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(destination)
            manifest["manifest_path"] = str(destination)
        return manifest

    def list_archive_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for source in self.list_allowed_sources():
            try:
                manifest = self.build_archive_manifest(source["id"], persist=False)
                summaries.append({**manifest["source"], **{
                    key: manifest[key]
                    for key in ("total_items", "video_ready", "transcript_ready", "incomplete")
                }})
            except Exception as exc:
                summaries.append({**source, "error": _safe_error(exc)})
        return summaries

    def refresh_archive(self, source_id: str) -> dict[str, Any]:
        source = self._archive_source(source_id)
        if not bool(source.get("enabled")):
            raise ValueError("授权来源已停用，不能刷新归档")
        rules = self._archive_rules(source)
        if not rules:
            raise ValueError("请先为该白名单账号建立账号自动发现规则")
        scans = [self.scan_rule(str(rule["id"])) for rule in rules]
        manifest = self.build_archive_manifest(source_id, persist=True)
        manifest["scan_results"] = scans
        return manifest

    def _transcribe_archive_job_async(self, job_id: str) -> bool:
        if not _as_bool(self._config().get("SPEECH_RECOGNITION_ENABLED", False)):
            return False
        if not self._claim_active_job(job_id):
            return False

        def worker() -> None:
            try:
                job = self.get_job(job_id) or {}
                video_path = str(job.get("local_video_path") or "")
                if not video_path or not os.path.isfile(video_path):
                    return
                from .speech_recognition import create_speech_recognizer_from_config

                recognizer = create_speech_recognizer_from_config(self._config(), job_id)
                if not recognizer:
                    return
                output_path = str(Path(video_path).parent / "archive_transcript.srt")
                if recognizer.transcribe_video_to_subtitles(video_path, output_path):
                    self._ensure_archive_markdown(job_id)
            except Exception:
                logger.exception("归档转写失败 %s", job_id)
            finally:
                self._release_active_job(job_id)

        threading.Thread(
            target=worker,
            name=f"archive-transcribe-{job_id[:8]}",
            daemon=True,
        ).start()
        return True

    def resume_archive(self, source_id: str) -> dict[str, Any]:
        source = self._archive_source(source_id)
        if not bool(source.get("enabled")):
            raise ValueError("授权来源已停用，不能续跑归档")
        manifest = self.build_archive_manifest(source_id, persist=True)
        downloads_started = 0
        transcriptions_started = 0
        for item in manifest["items"]:
            if not item["video_ready"] and item["status"] in {"discovered", "failed"}:
                if downloads_started < 3 and self.prepare_job_async(item["job_id"]):
                    downloads_started += 1
            elif item["video_ready"] and not item["transcript_ready"]:
                if transcriptions_started < 1 and self._transcribe_archive_job_async(item["job_id"]):
                    transcriptions_started += 1
        return {
            "downloads_started": downloads_started,
            "transcriptions_started": transcriptions_started,
            "remaining": manifest["incomplete"],
            "message": (
                f"已续跑下载 {downloads_started} 条、转写 {transcriptions_started} 条；"
                f"当前待补齐 {manifest['incomplete']} 条"
            ),
        }

    def add_manual_job(self, source_url: str, targets: list[str]) -> str:
        source_url = _validate_public_source_url(source_url)
        platform = _detect_platform(source_url)
        if platform not in SOURCE_PLATFORMS:
            raise ValueError("当前链接无法交给 yt-dlp 处理")
        valid_targets = [item for item in targets if item in TARGETS]
        if not valid_targets:
            raise ValueError("至少选择一个发布平台")
        if platform in valid_targets:
            label = PLATFORM_CATALOG.get(platform, {}).get("label", platform)
            raise ValueError(f"{label}来源不能再次发布到同一平台，避免平台内重复搬运")
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
            "content_preflight_json",
            "cover_preflight_json",
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
            "commentary_script",
            "x_text",
            "youtube_title",
            "youtube_description",
            "bilibili_title",
            "bilibili_description",
            "bilibili_partition_id",
            "douyin_text",
            "tiktok_text",
            "reviewed_at",
            "recreation_completed",
            "mpt_project_id",
            "mpt_asset_id",
            "mpt_task_id",
            "mpt_render_id",
            "mpt_status",
            "mpt_message",
            "mpt_workflow",
            "media_cleaned_at",
            "prepare_attempts",
            "x_publish_status",
            "youtube_publish_status",
            "bilibili_publish_status",
            "douyin_publish_status",
            "tiktok_publish_status",
            "x_publish_attempts",
            "youtube_publish_attempts",
            "bilibili_publish_attempts",
            "tiktok_publish_attempts",
            "next_retry_at",
            "last_retry_stage",
            "x_post_id",
            "youtube_video_id",
            "bilibili_post_id",
            "douyin_post_id",
            "tiktok_post_id",
            "error_message",
            "progress_percent",
            "progress_message",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        previous_status = ""
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT status FROM transfer_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            previous_status = str(previous["status"] or "") if previous else ""
            conn.execute(
                f"UPDATE transfer_jobs SET {assignments} WHERE id = ?",
                (*updates.values(), job_id),
            )
        if (
            updates.get("status") == JOB_STATUSES["COMPLETED"]
            and previous_status != JOB_STATUSES["COMPLETED"]
        ):
            self._emit_transfer_notification("published", job_id)

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

    def _emit_transfer_notification(
        self, event_name: str, job_id: str, error_message: str = ""
    ) -> None:
        try:
            from .notifications import (
                EVENT_TRANSFER_FAILED,
                EVENT_TRANSFER_PUBLISHED,
                EVENT_TRANSFER_REVIEW_READY,
                NotificationEvent,
                emit_notification_event,
            )

            event_type = {
                "failed": EVENT_TRANSFER_FAILED,
                "published": EVENT_TRANSFER_PUBLISHED,
                "review_ready": EVENT_TRANSFER_REVIEW_READY,
            }.get(event_name)
            if not event_type:
                return
            job = self.get_job(job_id) or {}
            base_url = str(
                self._config().get("TRANSFER_PUBLIC_BASE_URL")
                or "https://transfer.sg99.online"
            ).rstrip("/")
            emit_notification_event(
                NotificationEvent(
                    event_type=event_type,
                    payload={
                        "task_id": job_id,
                        "title": job.get("title") or "视频搬运任务",
                        "status": job.get("status") or "",
                        "targets": "、".join(_json_list(job.get("target_platforms"))),
                        "review_url": f"{base_url}/transfer-center/jobs/{job_id}/review",
                        "error_message": error_message or job.get("error_message") or "",
                    },
                )
            )
        except Exception:
            logger.debug("搬运通知未启用或不可用", exc_info=True)

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
            "--ignore-config",
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
            "--format",
            "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b",
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
        fallback_metadata: dict[str, Any] = {}
        if return_code != 0:
            raw_error = "".join(output_lines) or "下载失败"
            logger.warning("搬运任务下载失败 %s: %s", job_id, _safe_error(raw_error, limit=2400))
            if job["source_platform"] != "douyin":
                raise RuntimeError(_friendly_download_error(raw_error))
            self._update_job(
                job_id,
                progress_percent=20,
                progress_message="常规下载失败，正在尝试抖音备用解析",
            )
            try:
                fallback_metadata = download_douyin_video(
                    str(job["source_url"]),
                    output_dir / "video.mp4",
                    session=self._requests_session("douyin"),
                )
            except DouyinDownloadError as exc:
                raise RuntimeError(
                    f"{_friendly_download_error(raw_error)}；抖音备用解析失败：{_safe_error(exc)}"
                ) from exc
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
        metadata = dict(fallback_metadata)
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
        generated_fields = {
            "x_text": str(plan.get("x_text") or ""),
            "youtube_title": str(plan.get("youtube_title") or ""),
            "youtube_description": str(plan.get("youtube_description") or ""),
            "bilibili_title": str(
                plan.get("bilibili_title")
                or plan.get("youtube_title")
                or prepared_fields["title"]
                or ""
            )[:80],
            "bilibili_description": str(
                plan.get("bilibili_description")
                or plan.get("youtube_description")
                or prepared_fields["description"]
                or ""
            )[:2000],
            "douyin_text": str(
                plan.get("douyin_text")
                or plan.get("x_text")
                or prepared_fields["title"]
                or ""
            )[:2000],
            "tiktok_text": str(
                plan.get("tiktok_text")
                or plan.get("x_text")
                or prepared_fields["title"]
                or ""
            )[:2000],
        }
        content_preflight = run_content_preflight(generated_fields, targets)
        cover_preflight = run_cover_preflight(
            find_local_cover(str(videos[0])), media_info, targets
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["REVIEW"],
            progress_percent=72,
            progress_message="素材已就绪，默认进入标准二剪",
            **prepared_fields,
            media_probe_json=json.dumps(media_info, ensure_ascii=False),
            platform_variants_json=json.dumps(variants, ensure_ascii=False),
            distribution_plan_json=json.dumps(distribution_plan, ensure_ascii=False),
            content_preflight_json=json.dumps(content_preflight, ensure_ascii=False),
            cover_preflight_json=json.dumps(cover_preflight, ensure_ascii=False),
            recreation_status="draft",
            recreation_completed=0,
            processing_mode="professional",
            recreation_plan_json=serialize_plan(plan),
            original_angle=str(plan.get("original_angle") or ""),
            original_contribution=str(plan.get("original_contribution") or ""),
            commentary_script=str(plan.get("commentary_script") or ""),
            **generated_fields,
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
            commentary_script=str(plan.get("commentary_script") or ""),
            x_text=str(plan.get("x_text") or ""),
            youtube_title=str(plan.get("youtube_title") or ""),
            youtube_description=str(plan.get("youtube_description") or ""),
            bilibili_title=str(plan.get("bilibili_title") or ""),
            bilibili_description=str(plan.get("bilibili_description") or ""),
            douyin_text=str(plan.get("douyin_text") or ""),
            tiktok_text=str(plan.get("tiktok_text") or ""),
            reviewed_at=None,
            error_message="",
        )
        return self.get_job(job_id) or {}

    def replace_recreated_media(self, job_id: str, video_path: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        if any(
            job.get(field)
            for field in (
                "x_post_id",
                "youtube_video_id",
                "bilibili_post_id",
                "douyin_post_id",
                "tiktok_post_id",
            )
        ):
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
        cover_preflight = run_cover_preflight(
            find_local_cover(video_path), media_info, targets
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["REVIEW"],
            local_video_path=video_path,
            recreated_media_path=video_path,
            media_probe_json=json.dumps(media_info, ensure_ascii=False),
            platform_variants_json=json.dumps(variants, ensure_ascii=False),
            distribution_plan_json=json.dumps(distribution_plan, ensure_ascii=False),
            cover_preflight_json=json.dumps(cover_preflight, ensure_ascii=False),
            recreation_status="draft",
            recreation_completed=1,
            watermark_status="unreviewed",
            watermark_note="",
            reviewed_at=None,
            x_publish_status="pending" if "x" in targets else "skipped",
            youtube_publish_status="pending" if "youtube" in targets else "skipped",
            bilibili_publish_status="pending" if "bilibili" in targets else "skipped",
            douyin_publish_status="pending" if "douyin" in targets else "skipped",
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
        targets = _json_list(job.get("target_platforms"))
        content_preflight = run_content_preflight(
            {
                "x_text": normalized["x_text"],
                "youtube_title": normalized["youtube_title"],
                "youtube_description": normalized["youtube_description"],
                "bilibili_title": normalized["bilibili_title"],
                "bilibili_description": normalized["bilibili_description"],
                "douyin_text": normalized["douyin_text"],
                "tiktok_text": normalized["tiktok_text"],
                "commentary_script": normalized["commentary_script"],
            },
            targets,
        )
        media_info = deserialize_plan(job.get("media_probe_json"))
        cover_preflight = run_cover_preflight(
            find_local_cover(str(job.get("local_video_path") or "")),
            media_info,
            targets,
        )
        plan = deserialize_plan(job.get("recreation_plan_json"))
        plan.update(
            {
                "original_angle": normalized["original_angle"],
                "original_contribution": normalized["original_contribution"],
                "commentary_script": normalized["commentary_script"],
                "watermark_status": normalized["watermark_status"],
                "watermark_note": normalized["watermark_note"],
                "x_text": normalized["x_text"],
                "youtube_title": normalized["youtube_title"],
                "youtube_description": normalized["youtube_description"],
                "bilibili_title": normalized["bilibili_title"],
                "bilibili_description": normalized["bilibili_description"],
                "douyin_text": normalized["douyin_text"],
            }
        )
        status = JOB_STATUSES["REVIEW"]
        recreation_status = "draft"
        reviewed_at = None
        if approve:
            if content_preflight["verdict"] == "block":
                raise ValueError("发布前内容预检未通过，请先补齐平台文案")
            if (
                content_preflight["verdict"] == "review"
                and not _as_bool(payload.get("content_preflight_confirmed"))
            ):
                raise ValueError("发布前内容预检发现风险提示，请核对并勾选确认")
            if cover_preflight["verdict"] == "block":
                raise ValueError("封面文件体检未通过，请重新准备封面")
            if (
                cover_preflight["verdict"] == "review"
                and not _as_bool(payload.get("cover_preflight_confirmed"))
            ):
                raise ValueError("封面体检存在裁切或清晰度提示，请核对并勾选确认")
            if "bilibili" in targets and not str(
                normalized.get("bilibili_partition_id") or ""
            ).isdigit():
                raise ValueError("发布到 B站前请选择内容分区")
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
            content_preflight_json=json.dumps(content_preflight, ensure_ascii=False),
            cover_preflight_json=json.dumps(cover_preflight, ensure_ascii=False),
            original_angle=normalized["original_angle"],
            original_contribution=normalized["original_contribution"],
            commentary_script=normalized["commentary_script"],
            watermark_status=normalized["watermark_status"],
            watermark_note=normalized["watermark_note"],
            x_text=normalized["x_text"],
            youtube_title=normalized["youtube_title"],
            youtube_description=normalized["youtube_description"],
            bilibili_title=normalized["bilibili_title"],
            bilibili_description=normalized["bilibili_description"],
            bilibili_partition_id=normalized["bilibili_partition_id"],
            douyin_text=normalized["douyin_text"],
            tiktok_text=normalized["tiktok_text"],
            reviewed_at=reviewed_at,
            error_message="",
        )
        return self.get_job(job_id) or {}

    def send_to_money_printer(
        self,
        job_id: str,
        *,
        workflow: str = "professional",
    ) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        video_path = str(job.get("original_video_path") or job.get("local_video_path") or "")
        if not video_path or not os.path.isfile(video_path):
            raise ValueError("原视频尚未下载完成")
        workflow = workflow if workflow in {"quick", "professional"} else "professional"
        if job.get("mpt_project_id") and job.get("mpt_asset_id"):
            self._update_job(
                job_id,
                processing_mode=workflow,
                mpt_workflow=workflow,
            )
            return self.get_job(job_id) or {}

        base_url, auth_headers = self._money_printer_connection()
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
                headers=auth_headers,
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
                        "来自视频搬运通道。默认进行标准二剪，也可使用 AI 重制；"
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
                    "原片已就绪，可以生成快速二剪成片"
                    if workflow == "quick"
                    else "原片分析完成，可以进行标准二剪或 AI 重制"
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

    def _money_printer_connection(self) -> tuple[str, dict[str, str]]:
        config = self._config()
        base_url = str(
            config.get("TRANSFER_MPT_INTERNAL_URL") or "http://172.17.0.1:8080"
        ).rstrip("/")
        api_key = str(config.get("TRANSFER_MPT_API_KEY") or "").strip()
        if not api_key:
            credential_path = Path(get_app_subdir("config")) / "mpt_internal_credentials.json"
            try:
                payload = json.loads(credential_path.read_text(encoding="utf-8"))
                api_key = str(payload.get("api_key") or "").strip()
            except (OSError, ValueError, TypeError):
                api_key = ""
        return base_url, ({"x-api-key": api_key} if api_key else {})

    @staticmethod
    def _money_printer_json(
        session: requests.Session,
        base_url: str,
        auth_headers: dict[str, str],
        method: str,
        path: str,
        *,
        timeout: tuple[int, int] = (5, 180),
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = session.request(
            method,
            f"{base_url}{path}",
            headers=auth_headers,
            timeout=timeout,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("status") or 500) != 200:
            raise RuntimeError(str(payload.get("message") or "超级印钞机接口失败"))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def recreate_with_money_printer_async(self, job_id: str) -> bool:
        if not self.get_job(job_id):
            raise ValueError("搬运任务不存在")
        if not self._claim_active_job(job_id):
            return False

        def worker() -> None:
            try:
                self._run_money_printer_recreation(job_id)
            except Exception as exc:
                message = _safe_error(exc, limit=500)
                self._update_job(
                    job_id,
                    mpt_status="failed",
                    mpt_message=message,
                    recreation_status="draft",
                )
                logger.exception("一键二创失败: %s", job_id)
                self._emit_transfer_notification("failed", job_id, message)
            finally:
                self._release_active_job(job_id)

        threading.Thread(
            target=worker,
            name=f"mpt-recreate-{job_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _run_money_printer_recreation(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        script = str(job.get("commentary_script") or "").strip()
        if len(script) < 80:
            job = self.generate_recreation_draft(job_id)
            script = str(job.get("commentary_script") or "").strip()
        if len(script) < 80:
            raise ValueError("原创解说稿过短，请先生成或补充解说稿")
        self._update_job(
            job_id,
            processing_mode="professional",
            mpt_status="rendering",
            mpt_message="正在建立项目并分析原片",
        )
        job = self.send_to_money_printer(job_id, workflow="professional")
        base_url, auth_headers = self._money_printer_connection()
        if not auth_headers:
            raise ValueError("超级印钞机内部访问凭证未配置")
        session = requests.Session()
        session.trust_env = False
        project_id = quote(str(job.get("mpt_project_id") or ""))
        probe = deserialize_plan(job.get("media_probe_json"))
        targets = set(_json_list(job.get("target_platforms")))
        aspect = "9:16" if targets.intersection({"douyin", "tiktok"}) else (
            "16:9" if float(probe.get("width") or 0) >= float(probe.get("height") or 0) else "9:16"
        )
        attribution = str(job.get("source_uploader") or "原发布者")[:60]
        self._money_printer_json(
            session, base_url, auth_headers, "POST",
            f"/api/v1/projects/{project_id}/transfer-quick-setup",
            json={
                "asset_id": str(job.get("mpt_asset_id") or ""),
                "title": str(job.get("title") or "视频解读")[:100],
                "opening_title": str(job.get("original_angle") or job.get("title") or "")[:100],
                "closing_cta": f"素材来源：{attribution}",
                "source_attribution": attribution,
                "aspect": aspect,
            },
        )
        self._update_job(job_id, mpt_message="正在生成本地配音和字幕")
        audio = self._money_printer_json(
            session, base_url, auth_headers, "POST", "/api/v1/audio",
            json={
                "video_script": script,
                "video_language": "zh-CN",
                "voice_name": str(self._config().get("TRANSFER_MPT_VOICE_NAME") or "shengjiang:基础·沉稳男声-Male"),
                "voice_volume": 1.0,
                "voice_rate": 1.0,
                "bgm_type": "",
                "bgm_volume": 0.0,
            },
        )
        task_id = str(audio.get("task_id") or "")
        if not task_id:
            raise RuntimeError("本地配音未返回任务编号")
        self._update_job(job_id, mpt_task_id=task_id)
        self._money_printer_json(
            session, base_url, auth_headers, "POST",
            f"/api/v1/projects/{project_id}/tasks/{quote(task_id)}",
        )
        self._wait_for_money_printer_task(session, base_url, auth_headers, task_id, job_id)
        self._update_job(job_id, mpt_message="正在合成重构画面、原创解说和字幕")
        render = self._money_printer_json(
            session, base_url, auth_headers, "POST",
            f"/api/v1/projects/{project_id}/renders",
            json={"preset": "delivery", "include_subtitles": True, "audio_mode": "voiceover"},
        )
        render_id = str(render.get("render_id") or "")
        if not render_id:
            raise RuntimeError("成片渲染未返回任务编号")
        self._update_job(job_id, mpt_render_id=render_id)
        self._wait_for_money_printer_render(session, base_url, auth_headers, render_id, job_id)
        result = self.sync_money_printer_render(job_id)
        self._emit_transfer_notification("review_ready", job_id)
        return result

    def _wait_for_money_printer_task(self, session, base_url, auth_headers, task_id, job_id) -> None:
        deadline = time.monotonic() + int(self._config().get("TRANSFER_MPT_AUDIO_TIMEOUT_SECONDS") or 1200)
        while time.monotonic() < deadline:
            task = self._money_printer_json(
                session, base_url, auth_headers, "GET", f"/api/v1/tasks/{quote(task_id)}", timeout=(5, 30)
            )
            state = int(task.get("state") or 0)
            if state == 1:
                return
            if state == -1:
                raise RuntimeError(str(task.get("error") or "本地配音生成失败"))
            self._update_job(job_id, mpt_message=f"本地配音与字幕生成中（{int(task.get('progress') or 0)}%）")
            time.sleep(3)
        raise TimeoutError("本地配音生成超时")

    def _wait_for_money_printer_render(self, session, base_url, auth_headers, render_id, job_id) -> None:
        deadline = time.monotonic() + int(self._config().get("TRANSFER_MPT_RENDER_TIMEOUT_SECONDS") or 3600)
        while time.monotonic() < deadline:
            render = self._money_printer_json(
                session, base_url, auth_headers, "GET", f"/api/v1/renders/{quote(render_id)}", timeout=(5, 30)
            )
            status = str(render.get("status") or "")
            if status == "ready":
                return
            if status == "failed":
                raise RuntimeError(str(render.get("error_message") or "成片渲染失败"))
            self._update_job(job_id, mpt_message="重构成片渲染中")
            time.sleep(5)
        raise TimeoutError("成片渲染超时")

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
            else str(job.get("mpt_workflow") or job.get("processing_mode") or "professional")
        )
        if selected_workflow not in {"quick", "professional"}:
            selected_workflow = "professional"
        public_url = str(
            self._config().get("TRANSFER_MPT_PUBLIC_URL")
            or "https://video.sg99.online/app/"
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

    def sync_money_printer_render(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        project_id = str(job.get("mpt_project_id") or "").strip()
        if not project_id:
            raise ValueError("尚未建立超级印钞机加工项目")
        if job.get("x_post_id") or job.get("youtube_video_id"):
            raise ValueError("已有平台发布结果，不能替换成片")

        base_url, auth_headers = self._money_printer_connection()
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                f"{base_url}/api/v1/projects/{quote(project_id)}/renders",
                params={"page": 1, "page_size": 20},
                headers=auth_headers,
                timeout=(5, 30),
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("status") or 500) != 200:
                raise RuntimeError(str(payload.get("message") or "读取成片列表失败"))
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            render = next(
                (
                    item
                    for item in data.get("renders") or []
                    if item.get("status") == "ready"
                    and isinstance(item.get("output_asset"), dict)
                    and item["output_asset"].get("content_url")
                ),
                None,
            )
            if not render:
                raise ValueError("超级印钞机尚未生成可同步的成片")

            content_url = urljoin(
                f"{base_url}/",
                str(render["output_asset"]["content_url"]),
            )
            media_response = session.get(
                content_url,
                stream=True,
                headers=auth_headers,
                timeout=(5, 300),
            )
            media_response.raise_for_status()
            content_length = int(media_response.headers.get("Content-Length") or 0)
            if content_length > 10 * 1024 * 1024 * 1024:
                raise ValueError("加工成片超过 10GB，无法自动同步")

            output_dir = Path(get_app_subdir("downloads")) / "transfer" / job_id
            output_dir.mkdir(parents=True, exist_ok=True)
            render_id = str(render.get("render_id") or uuid.uuid4().hex)
            target_path = output_dir / f"mpt-render-{render_id[:12]}.mp4"
            partial_path = target_path.with_suffix(".mp4.part")
            downloaded = 0
            with open(partial_path, "wb") as output:
                for chunk in media_response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > 10 * 1024 * 1024 * 1024:
                        raise ValueError("加工成片超过 10GB，无法自动同步")
                    output.write(chunk)
            if downloaded <= 0:
                raise ValueError("超级印钞机返回了空成片")
            os.replace(partial_path, target_path)
            result = self.replace_recreated_media(job_id, str(target_path))
            self._update_job(
                job_id,
                processing_mode=str(job.get("mpt_workflow") or "professional"),
                mpt_status="imported",
                mpt_message="加工成片已自动同步，请完成最终确认",
            )
            return self.get_job(job_id) or result
        except Exception as exc:
            for candidate in (
                locals().get("partial_path"),
                locals().get("target_path"),
            ):
                if candidate and Path(candidate).is_file():
                    try:
                        Path(candidate).unlink()
                    except OSError:
                        pass
            if isinstance(exc, ValueError):
                raise
            message = _safe_error(exc, limit=500)
            raise RuntimeError(f"同步超级印钞机成片失败：{message}") from exc

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

    def publish_douyin_openapi_async(
        self,
        job_id: str,
        publisher: Callable[..., dict],
    ) -> bool:
        """Publish one reviewed Douyin-ready job after an explicit user click."""
        if not self._claim_active_job(job_id):
            return False
        thread = threading.Thread(
            target=self._publish_douyin_openapi_guarded,
            args=(job_id, publisher),
            name=f"transfer-douyin-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _publish_douyin_openapi_guarded(
        self,
        job_id: str,
        publisher: Callable[..., dict],
    ) -> None:
        try:
            job = self.get_job(job_id)
            if not job:
                raise ValueError("搬运任务不存在")
            if "douyin" not in _json_list(job.get("target_platforms")):
                raise ValueError("当前任务没有选择发布到抖音")
            if str(job.get("douyin_publish_status") or "") != "manual_ready":
                raise ValueError("抖音素材尚未准备完成或正在发布")
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
            video_path = self._target_video_path(job, "douyin")
            if not video_path:
                raise ValueError("抖音媒体版本未就绪")
            self._update_job(
                job_id,
                status=JOB_STATUSES["PUBLISHING"],
                douyin_publish_status="publishing",
                error_message="",
                progress_percent=91,
                progress_message="正在连接已授权的抖音发布账号",
            )
            result = publisher(
                video_path,
                str(job.get("douyin_text") or job.get("title") or "新视频"),
                progress_callback=lambda progress: self._update_job(
                    job_id,
                    progress_percent=92 + min(1.0, max(0.0, float(progress))) * 7,
                    progress_message=f"正在上传到抖音 {float(progress) * 100:.0f}%",
                ),
            )
            self.mark_douyin_api_published(
                job_id,
                str((result or {}).get("item_id") or ""),
                str((result or {}).get("video_id") or ""),
            )
        except Exception as exc:
            reconnect_required = bool(getattr(exc, "reconnect_required", False))
            safe_message = _safe_error(exc)
            self._update_job(
                job_id,
                status=JOB_STATUSES["READY"],
                douyin_publish_status=(
                    "waiting_auth" if reconnect_required else "manual_ready"
                ),
                progress_percent=90,
                progress_message=(
                    "抖音发布账号需要重新连接"
                    if reconnect_required
                    else "抖音接口发布未完成，可继续使用网页发布"
                ),
                error_message=f"抖音: {safe_message}",
                next_retry_at=None,
                last_retry_stage="",
            )
            logger.exception("抖音接口发布失败 %s", job_id)
        finally:
            self._release_active_job(job_id)

    def mark_youtube_reconnected(self) -> int:
        """Restore blocked jobs after a real YouTube channel was verified."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE transfer_jobs
                SET youtube_publish_status='pending',
                    next_retry_at=NULL,
                    last_retry_stage='',
                    error_message='',
                    progress_percent=90,
                    progress_message='YouTube 频道已连接，等待发布',
                    updated_at=?
                WHERE youtube_publish_status='waiting_auth'
                  AND youtube_video_id=''
                  AND instr(target_platforms, '"youtube"') > 0
                """,
                (_utc_now(),),
            )
        return int(cursor.rowcount or 0)

    def mark_x_manually_published(self, job_id: str, post_url: str = "") -> dict:
        """Record completion after the user confirms the free X web upload."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        targets = _json_list(job.get("target_platforms"))
        if "x" not in targets:
            raise ValueError("当前任务没有选择发布到 X")
        if str(job.get("x_publish_status") or "") != "manual_ready":
            raise ValueError("X 素材尚未准备完成或已经确认发布")

        normalized_url = str(post_url or "").strip()
        if normalized_url:
            parsed = urlparse(normalized_url)
            host = (parsed.hostname or "").lower()
            if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
                raise ValueError("请填写有效的 X 帖子链接")
            if not re.search(r"/status/\d+", parsed.path):
                raise ValueError("X 帖子链接格式不完整")
            result_id = normalized_url
        else:
            result_id = f"manual-confirmed:{uuid.uuid4().hex[:12]}"

        other_targets_done = all(
            (
                target == "x"
                or (target == "youtube" and bool(job.get("youtube_video_id")))
                or (target == "bilibili" and bool(job.get("bilibili_post_id")))
                or (target == "douyin" and bool(job.get("douyin_post_id")))
                or (target == "tiktok" and bool(job.get("tiktok_post_id")))
            )
            for target in targets
        )
        completed = other_targets_done
        self._update_job(
            job_id,
            x_publish_status="completed",
            x_post_id=result_id,
            status=(
                JOB_STATUSES["COMPLETED"] if completed else JOB_STATUSES["READY"]
            ),
            progress_percent=100 if completed else 90,
            progress_message=(
                "所有平台发布完成"
                if completed and len(targets) > 1
                else (
                    "X 已确认发布完成"
                    if completed
                    else "X 已确认发布，等待 YouTube 完成"
                )
            ),
            error_message="",
            next_retry_at=None,
            last_retry_stage="",
        )
        return self.get_job(job_id) or {}

    def mark_douyin_manually_published(
        self, job_id: str, post_url: str = ""
    ) -> dict:
        """Record completion after the creator confirms the Douyin web upload."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        targets = _json_list(job.get("target_platforms"))
        if "douyin" not in targets:
            raise ValueError("当前任务没有选择发布到抖音")
        if str(job.get("douyin_publish_status") or "") != "manual_ready":
            raise ValueError("抖音素材尚未准备完成或已经确认发布")

        normalized_url = str(post_url or "").strip()
        if normalized_url:
            parsed = urlparse(normalized_url)
            host = (parsed.hostname or "").lower()
            if host not in {
                "douyin.com",
                "www.douyin.com",
                "v.douyin.com",
            }:
                raise ValueError("请填写有效的抖音作品链接")
            result_id = normalized_url
        else:
            result_id = f"manual-confirmed:{uuid.uuid4().hex[:12]}"

        other_targets_done = all(
            (
                target == "douyin"
                or (target == "x" and bool(job.get("x_post_id")))
                or (target == "youtube" and bool(job.get("youtube_video_id")))
                or (target == "bilibili" and bool(job.get("bilibili_post_id")))
                or (target == "tiktok" and bool(job.get("tiktok_post_id")))
            )
            for target in targets
        )
        self._update_job(
            job_id,
            douyin_publish_status="completed",
            douyin_post_id=result_id,
            status=(
                JOB_STATUSES["COMPLETED"]
                if other_targets_done
                else JOB_STATUSES["READY"]
            ),
            progress_percent=100 if other_targets_done else 90,
            progress_message=(
                "所有平台发布完成"
                if other_targets_done
                else "抖音已确认发布，等待其他平台完成"
            ),
            error_message="",
            next_retry_at=None,
            last_retry_stage="",
        )
        return self.get_job(job_id) or {}

    def mark_douyin_api_published(
        self,
        job_id: str,
        item_id: str,
        video_id: str = "",
    ) -> dict:
        """Record a successful official OpenAPI publish."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        targets = _json_list(job.get("target_platforms"))
        if "douyin" not in targets:
            raise ValueError("当前任务没有选择发布到抖音")
        if str(job.get("douyin_publish_status") or "") not in {
            "manual_ready",
            "publishing",
        }:
            raise ValueError("抖音素材尚未准备完成或已经确认发布")
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            raise ValueError("抖音接口未返回作品 ID")
        other_targets_done = all(
            (
                target == "douyin"
                or (target == "x" and bool(job.get("x_post_id")))
                or (target == "youtube" and bool(job.get("youtube_video_id")))
                or (target == "bilibili" and bool(job.get("bilibili_post_id")))
                or (target == "tiktok" and bool(job.get("tiktok_post_id")))
            )
            for target in targets
        )
        self._update_job(
            job_id,
            douyin_publish_status="completed",
            douyin_post_id=normalized_item_id,
            status=(
                JOB_STATUSES["COMPLETED"]
                if other_targets_done
                else JOB_STATUSES["READY"]
            ),
            progress_percent=100 if other_targets_done else 90,
            progress_message=(
                "所有平台发布完成"
                if other_targets_done
                else "抖音接口发布成功，等待其他平台完成"
            ),
            error_message="",
            next_retry_at=None,
            last_retry_stage="",
        )
        logger.info(
            "抖音接口发布成功 job=%s item_id=%s video_id=%s",
            job_id,
            normalized_item_id,
            str(video_id or ""),
        )
        return self.get_job(job_id) or {}

    def mark_tiktok_manually_published(
        self, job_id: str, post_url: str = ""
    ) -> dict:
        """Record completion after the user confirms the TikTok web upload."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        targets = _json_list(job.get("target_platforms"))
        if "tiktok" not in targets:
            raise ValueError("当前任务没有选择发布到 TikTok")
        if str(job.get("tiktok_publish_status") or "") != "manual_ready":
            raise ValueError("TikTok 素材尚未准备完成或已经确认发布")

        normalized_url = str(post_url or "").strip()
        if normalized_url:
            parsed = urlparse(normalized_url)
            host = (parsed.hostname or "").lower()
            if not (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            ):
                raise ValueError("请填写有效的 TikTok 作品链接")
            result_id = normalized_url
        else:
            result_id = f"manual-confirmed:{uuid.uuid4().hex[:12]}"

        other_targets_done = all(
            (
                target == "tiktok"
                or (target == "x" and bool(job.get("x_post_id")))
                or (target == "youtube" and bool(job.get("youtube_video_id")))
                or (target == "bilibili" and bool(job.get("bilibili_post_id")))
                or (target == "douyin" and bool(job.get("douyin_post_id")))
            )
            for target in targets
        )
        self._update_job(
            job_id,
            tiktok_publish_status="completed",
            tiktok_post_id=result_id,
            status=(JOB_STATUSES["COMPLETED"] if other_targets_done else JOB_STATUSES["READY"]),
            progress_percent=100 if other_targets_done else 90,
            progress_message=(
                "所有平台发布完成"
                if other_targets_done
                else "TikTok 已确认发布，等待其他平台完成"
            ),
            error_message="",
            next_retry_at=None,
            last_retry_stage="",
        )
        return self.get_job(job_id) or {}

    def _resolve_app_file(self, value: str) -> str:
        configured = str(value or "").strip()
        if not configured:
            return ""
        path = (
            configured
            if os.path.isabs(configured)
            else os.path.join(get_app_subdir(""), configured)
        )
        resolved = os.path.realpath(path)
        root = os.path.realpath(get_app_subdir(""))
        try:
            if os.path.commonpath((root, resolved)) != root:
                return ""
        except ValueError:
            return ""
        return resolved if os.path.isfile(resolved) else ""

    def _bilibili_upload_cookie_path(self) -> str:
        config = self._config()
        candidates = (
            config.get("BILIBILI_COOKIES_PATH") or "cookies/bili_cookies.json",
            config.get("TRANSFER_BILIBILI_COOKIES_PATH")
            or "cookies/bilibili_unified_cookies.txt",
        )
        return next(
            (
                path
                for path in (self._resolve_app_file(item) for item in candidates)
                if path
            ),
            "",
        )

    @staticmethod
    def _video_cover_path(job: dict) -> str:
        video_path = str(job.get("local_video_path") or "")
        if not video_path:
            return ""
        output_dir = Path(video_path).parent
        for pattern in (
            "video.jpg",
            "video.jpeg",
            "video.png",
            "video.webp",
            "*.jpg",
            "*.jpeg",
            "*.png",
            "*.webp",
        ):
            match = next(
                (
                    item
                    for item in sorted(output_dir.glob(pattern))
                    if item.is_file() and item.stat().st_size > 0
                ),
                None,
            )
            if match:
                return str(match)

        cover_path = output_dir / "bilibili-cover.jpg"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "1",
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(cover_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not cover_path.is_file():
            raise RuntimeError("无法生成 B站投稿封面")
        return str(cover_path)

    def _publish_bilibili(
        self,
        job: dict,
        *,
        progress_callback: Callable[[float], None] | None = None,
    ) -> str:
        from .bilibili_uploader import BilibiliUploader

        video_path = self._target_video_path(job, "bilibili")
        if not video_path:
            raise RuntimeError("B站媒体版本未就绪")
        cookie_path = self._bilibili_upload_cookie_path()
        if not cookie_path:
            raise ValueError("B站发布账号尚未登录，请先在设置中扫码连接")
        partition_id = str(
            job.get("bilibili_partition_id")
            or self._config().get("FIXED_PARTITION_ID_BILIBILI")
            or ""
        ).strip()
        if not partition_id.isdigit():
            raise ValueError("发布到 B站前请选择内容分区")

        metadata = {}
        metadata_path = str(job.get("local_metadata_path") or "")
        if metadata_path and os.path.isfile(metadata_path):
            try:
                metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                metadata = {}
        tags = [
            str(item).strip()
            for item in (metadata.get("tags") or [])
            if str(item).strip()
        ][:12]
        uploader = BilibiliUploader(cookie_file=cookie_path)

        def on_progress(value: str) -> None:
            if progress_callback is None:
                return
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value or ""))
            if match:
                progress_callback(max(0.0, min(1.0, float(match.group(1)) / 100)))

        success, result = uploader.upload_video(
            video_file_path=video_path,
            cover_file_path=self._video_cover_path(
                {**job, "local_video_path": video_path}
            ),
            title=str(job.get("bilibili_title") or job.get("title") or "")[:80],
            description=str(
                job.get("bilibili_description")
                or job.get("youtube_description")
                or job.get("description")
                or ""
            )[:2000],
            tags=tags,
            partition_id=partition_id,
            youtube_url=str(job.get("source_url") or ""),
            task_id=str(job.get("id") or ""),
            progress_callback=on_progress,
        )
        if not success:
            raise RuntimeError(str(result or "B站上传失败"))
        if isinstance(result, dict):
            return str(
                result.get("url")
                or result.get("bvid")
                or result.get("aid")
                or json.dumps(result, ensure_ascii=False)
            )
        return str(result or "uploaded")

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
                    int(job.get("bilibili_publish_attempts") or 0),
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

    def _publish_youtube(
        self,
        job: dict,
        token_path: str,
        progress_callback: Callable[[float], None] | None = None,
    ) -> str:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except Exception as exc:
            raise RuntimeError("YouTube发布依赖未安装") from exc
        credentials = Credentials.from_authorized_user_file(
            token_path,
            scopes=list(YOUTUBE_SCOPES),
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
                "privacyStatus": str(self._config().get("TRANSFER_YOUTUBE_PRIVACY") or "public"),
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
            status, response = request.next_chunk()
            if status is not None and progress_callback is not None:
                progress_callback(max(0.0, min(1.0, float(status.progress()))))
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
            progress_percent=88,
            progress_message="发布任务已进入后台队列",
        )
        errors = []
        retryable_errors = []
        x_post_id = job.get("x_post_id") or ""
        youtube_video_id = job.get("youtube_video_id") or ""
        bilibili_post_id = job.get("bilibili_post_id") or ""
        douyin_post_id = job.get("douyin_post_id") or ""
        tiktok_post_id = job.get("tiktok_post_id") or ""
        if "x" in targets and not x_post_id:
            x_path = self._target_video_path(job, "x")
            if not x_path:
                self._update_job(job_id, x_publish_status="blocked")
                errors.append("X 媒体版本未就绪，请先完成原创剪辑或重新体检")
            else:
                self._update_job(
                    job_id,
                    x_publish_status="manual_ready",
                    progress_percent=90,
                    progress_message="X 素材已备好，正在处理其他发布平台",
                )
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
                    progress_percent=91,
                    progress_message="正在连接已验证的 YouTube 频道",
                )
                try:
                    youtube_job = {**job, "local_video_path": youtube_path}
                    youtube_video_id = self._publish_youtube(
                        youtube_job,
                        token_path,
                        progress_callback=lambda progress: self._update_job(
                            job_id,
                            progress_percent=92 + (progress * 7),
                            progress_message=f"正在上传到 YouTube {progress * 100:.0f}%",
                        ),
                    )
                    self._update_job(
                        job_id,
                        youtube_video_id=youtube_video_id,
                        youtube_publish_status="completed",
                        progress_percent=99,
                        progress_message="YouTube 上传完成",
                    )
                except Exception as exc:
                    friendly, retryable, reconnect_required = (
                        _friendly_youtube_publish_error(exc)
                    )
                    message = f"YouTube: {friendly}"
                    self._update_job(
                        job_id,
                        youtube_publish_status=(
                            "waiting_auth" if reconnect_required else "failed"
                        ),
                        progress_percent=90,
                        progress_message=(
                            "YouTube 频道需要重新连接"
                            if reconnect_required
                            else "YouTube 发布失败，需要检查"
                        ),
                    )
                    if reconnect_required:
                        invalidate_youtube_connection()
                    errors.append(message)
                    if retryable:
                        retryable_errors.append((message, youtube_attempts))
        if "bilibili" in targets and not bilibili_post_id:
            bilibili_path = self._target_video_path(job, "bilibili")
            if not bilibili_path:
                self._update_job(job_id, bilibili_publish_status="blocked")
                errors.append("B站媒体版本未就绪")
            else:
                bilibili_attempts = (
                    int(job.get("bilibili_publish_attempts") or 0) + 1
                )
                self._update_job(
                    job_id,
                    bilibili_publish_status="publishing",
                    bilibili_publish_attempts=bilibili_attempts,
                    progress_percent=91,
                    progress_message="正在上传到 B站",
                )
                try:
                    bilibili_job = {
                        **job,
                        "id": job_id,
                        "local_video_path": bilibili_path,
                    }
                    bilibili_post_id = self._publish_bilibili(
                        bilibili_job,
                        progress_callback=lambda progress: self._update_job(
                            job_id,
                            progress_percent=92 + (progress * 7),
                            progress_message=f"正在上传到 B站 {progress * 100:.0f}%",
                        ),
                    )
                    self._update_job(
                        job_id,
                        bilibili_post_id=bilibili_post_id,
                        bilibili_publish_status="completed",
                        progress_percent=99,
                        progress_message="B站上传完成",
                    )
                except Exception as exc:
                    safe_message = _safe_error(exc)
                    needs_login = "登录" in safe_message or "cookie" in safe_message.lower()
                    self._update_job(
                        job_id,
                        bilibili_publish_status=(
                            "waiting_auth" if needs_login else "failed"
                        ),
                        progress_percent=90,
                        progress_message=(
                            "B站发布账号需要重新登录"
                            if needs_login
                            else "B站发布失败，需要检查"
                        ),
                    )
                    message = f"B站: {safe_message}"
                    errors.append(message)
                    if any(
                        marker in safe_message.lower()
                        for marker in ("timeout", "timed out", "connection", " 5")
                    ):
                        retryable_errors.append((message, bilibili_attempts))
        if "douyin" in targets and not douyin_post_id:
            douyin_path = self._target_video_path(job, "douyin")
            if not douyin_path:
                self._update_job(job_id, douyin_publish_status="blocked")
                errors.append("抖音媒体版本未就绪")
            else:
                self._update_job(
                    job_id,
                    douyin_publish_status="manual_ready",
                    progress_percent=90,
                    progress_message="抖音素材与文案已备好，等待网页确认发布",
                )
        if "tiktok" in targets and not tiktok_post_id:
            tiktok_path = self._target_video_path(job, "tiktok")
            if not tiktok_path:
                self._update_job(job_id, tiktok_publish_status="blocked")
                errors.append("TikTok 媒体版本未就绪")
            else:
                self._update_job(
                    job_id,
                    tiktok_publish_status="manual_ready",
                    progress_percent=90,
                    progress_message="TikTok 素材与文案已备好，等待网页确认发布",
                )
        completed = all(
            (target == "x" and x_post_id)
            or (target == "youtube" and youtube_video_id)
            or (target == "bilibili" and bilibili_post_id)
            or (target == "douyin" and douyin_post_id)
            or (target == "tiktok" and tiktok_post_id)
            for target in targets
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["COMPLETED"] if completed else JOB_STATUSES["READY"],
            error_message="；".join(errors),
            next_retry_at=None,
            last_retry_stage="",
            progress_percent=100 if completed else 90,
            progress_message=(
                "所有平台发布完成"
                if completed
                else (
                    "YouTube 频道需要重新连接"
                    if self.get_job(job_id).get("youtube_publish_status") == "waiting_auth"
                    else "自动处理完成，请查看分平台状态"
                )
            ),
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
        self._scheduler.add_job(
            self.run_maintenance,
            "cron",
            hour=3,
            minute=20,
            id="transfer-center-maintenance",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
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
