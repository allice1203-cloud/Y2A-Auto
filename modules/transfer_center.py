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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from .utils import get_app_subdir


logger = logging.getLogger("transfer_center")

PLATFORMS = {"bilibili", "douyin"}
DISCOVERY_MODES = {"account", "keyword", "manual"}
TARGETS = {"x", "youtube"}
JOB_STATUSES = {
    "DISCOVERED": "discovered",
    "DOWNLOADING": "downloading",
    "READY": "ready",
    "PUBLISHING": "publishing",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}


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


def _detect_platform(url: str) -> str:
    lowered = str(url or "").lower()
    if "bilibili.com" in lowered or "b23.tv" in lowered:
        return "bilibili"
    if "douyin.com" in lowered:
        return "douyin"
    return ""


class TransferCenter:
    def __init__(self, config_provider: Callable[[], dict] | None = None):
        self._config_provider = config_provider or (lambda: {})
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._scheduler = None
        self.db_path = os.path.join(get_app_subdir("db"), "transfer_center.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
                    local_metadata_path TEXT DEFAULT '',
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
            "auto_prepare": int(_as_bool(payload.get("auto_prepare", True))),
            "auto_publish": int(_as_bool(payload.get("auto_publish", False))),
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
                        interval_minutes, max_items, auto_prepare, auto_publish,
                        enabled, created_at, updated_at
                    ) VALUES (
                        :id, :name, :platform, :discovery_mode, :source_value,
                        :include_keywords, :exclude_keywords, :target_platforms,
                        :interval_minutes, :max_items, :auto_prepare, :auto_publish,
                        :enabled, :created_at, :updated_at
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
        )
        if completed.returncode != 0:
            raise RuntimeError(_safe_error(completed.stderr or completed.stdout or "yt-dlp发现失败"))
        try:
            return json.loads(completed.stdout)
        except Exception as exc:
            raise RuntimeError("平台返回内容无法解析") from exc

    def _requests_session(self, platform: str) -> requests.Session:
        session = requests.Session()
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

    def _insert_discovered_job(self, rule: dict, item: dict) -> tuple[str, bool]:
        now = _utc_now()
        source_id = str(item.get("id") or hashlib.sha256(item["url"].encode()).hexdigest()[:24])
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO transfer_jobs (
                        id, rule_id, source_platform, source_id, source_url,
                        source_uploader, title, description, thumbnail_url,
                        duration, published_at, target_platforms, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            added_ids = []
            for item in items:
                if not self._matches_filters(rule, item):
                    continue
                job_id, created = self._insert_discovered_job(rule, item)
                if created:
                    added_ids.append(job_id)
            message = f"发现 {len(items)} 条，新增 {len(added_ids)} 条"
            self._mark_rule_scan(rule_id, "success", message)
            if rule["auto_prepare"]:
                for job_id in added_ids:
                    self.prepare_job_async(job_id, publish_after=bool(rule["auto_publish"]))
            return {"success": True, "found": len(items), "added": len(added_ids), "message": message}
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
                SET last_scan_at=?, last_scan_status=?, last_scan_message=?, updated_at=?
                WHERE id=?
                """,
                (_utc_now(), status, _safe_error(message), _utc_now(), rule_id),
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

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_manual_job(self, source_url: str, targets: list[str]) -> str:
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
        source_id = hashlib.sha256(source_url.strip().encode()).hexdigest()[:24]
        job_id, created = self._insert_discovered_job(
            rule,
            {"id": source_id, "url": source_url.strip(), "title": ""},
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
            "local_metadata_path",
            "x_post_id",
            "youtube_video_id",
            "error_message",
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

    def prepare_job_async(self, job_id: str, publish_after: bool = False) -> None:
        thread = threading.Thread(
            target=self._prepare_job_guarded,
            args=(job_id, publish_after),
            name=f"transfer-prepare-{job_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _prepare_job_guarded(self, job_id: str, publish_after: bool) -> None:
        try:
            self.prepare_job(job_id)
            if publish_after:
                self.publish_job(job_id)
        except Exception as exc:
            self._update_job(
                job_id,
                status=JOB_STATUSES["FAILED"],
                error_message=_safe_error(exc),
            )
            logger.exception("搬运任务处理失败 %s", job_id)

    def prepare_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("搬运任务不存在")
        self._update_job(job_id, status=JOB_STATUSES["DOWNLOADING"], error_message="")
        output_dir = Path(get_app_subdir("downloads")) / "transfer" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / "video.%(ext)s")
        metadata_path = output_dir / "metadata.json"
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
        ]
        cookie_path = self._cookie_path(job["source_platform"])
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(job["source_url"])
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, check=False)
        if completed.returncode != 0:
            raise RuntimeError(_safe_error(completed.stderr or completed.stdout or "下载失败"))
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
        self._update_job(
            job_id,
            status=JOB_STATUSES["READY"],
            title=str(metadata.get("title") or job.get("title") or "")[:500],
            description=str(metadata.get("description") or job.get("description") or "")[:8000],
            source_uploader=str(metadata.get("uploader") or job.get("source_uploader") or "")[:300],
            thumbnail_url=str(metadata.get("thumbnail") or job.get("thumbnail_url") or "")[:1500],
            duration=metadata.get("duration") or job.get("duration"),
            local_video_path=str(videos[0]),
            local_metadata_path=str(metadata_path),
            error_message="",
        )
        return self.get_job(job_id) or {}

    # ---- publishing ----------------------------------------------------
    def publish_job_async(self, job_id: str) -> None:
        thread = threading.Thread(
            target=self._publish_job_guarded,
            args=(job_id,),
            name=f"transfer-publish-{job_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _publish_job_guarded(self, job_id: str) -> None:
        try:
            self.publish_job(job_id)
        except Exception as exc:
            self._update_job(
                job_id,
                status=JOB_STATUSES["FAILED"],
                error_message=_safe_error(exc),
            )
            logger.exception("搬运任务发布失败 %s", job_id)

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
        text = str(job.get("title") or "新视频").strip()[:260]
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
                "title": str(job.get("title") or "新视频")[:100],
                "description": str(job.get("description") or "")[:5000],
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
        if not job.get("local_video_path") or not os.path.isfile(job["local_video_path"]):
            raise ValueError("视频尚未准备完成")
        targets = _json_list(job["target_platforms"])
        config = self._config()
        self._update_job(job_id, status=JOB_STATUSES["PUBLISHING"], error_message="")
        errors = []
        x_post_id = job.get("x_post_id") or ""
        youtube_video_id = job.get("youtube_video_id") or ""
        if "x" in targets and not x_post_id:
            token = str(config.get("TRANSFER_X_ACCESS_TOKEN") or "").strip()
            if not token:
                errors.append("X 尚未授权")
            else:
                try:
                    x_post_id = self._publish_x(job, token)
                    self._update_job(job_id, x_post_id=x_post_id)
                except Exception as exc:
                    errors.append(f"X: {_safe_error(exc)}")
        if "youtube" in targets and not youtube_video_id:
            token_path = os.path.join(get_app_subdir("config"), "youtube_transfer_token.json")
            if not os.path.isfile(token_path):
                errors.append("YouTube 尚未授权")
            else:
                try:
                    youtube_video_id = self._publish_youtube(job, token_path)
                    self._update_job(job_id, youtube_video_id=youtube_video_id)
                except Exception as exc:
                    errors.append(f"YouTube: {_safe_error(exc)}")
        completed = all(
            (target == "x" and x_post_id) or (target == "youtube" and youtube_video_id)
            for target in targets
        )
        self._update_job(
            job_id,
            status=JOB_STATUSES["COMPLETED"] if completed else JOB_STATUSES["READY"],
            error_message="；".join(errors),
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
