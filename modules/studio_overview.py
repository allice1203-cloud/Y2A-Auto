#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Read-only aggregation for the legacy and transfer task dashboards."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


LEGACY_REVIEW_STATUS = "awaiting_manual_review"
LEGACY_COMPLETED_STATUS = "completed"
LEGACY_FAILED_STATUS = "failed"
LEGACY_PENDING_STATUS = "pending"
LEGACY_READY_STATUS = "ready_for_upload"

TRANSFER_REVIEW_STATUS = "review"
TRANSFER_COMPLETED_STATUS = "completed"
TRANSFER_FAILED_STATUS = "failed"
TRANSFER_PENDING_STATUSES = {"discovered"}
TRANSFER_READY_STATUSES = {"ready"}
TRANSFER_INACTIVE_STATUSES = {
    TRANSFER_REVIEW_STATUS,
    TRANSFER_COMPLETED_STATUS,
    TRANSFER_FAILED_STATUS,
    "skipped",
}

LEGACY_STATUS_LABELS = {
    "pending": "等待处理",
    "fetching_info": "获取视频信息",
    "info_fetched": "视频信息已获取",
    "translating": "翻译文案",
    "tagging": "生成标签",
    "partitioning": "推荐分区",
    "moderating": "内容检查",
    "downloading": "下载素材",
    "downloaded": "素材已下载",
    "asr_transcribing": "语音转写",
    "translating_subtitle": "翻译字幕",
    "encoding_video": "生成视频",
    "awaiting_manual_review": "待人工审核",
    "ready_for_upload": "等待发布",
    "uploading": "正在发布",
    "completed": "已完成",
    "failed": "处理失败",
}

TRANSFER_STATUS_LABELS = {
    "discovered": "等待下载",
    "downloading": "下载素材",
    "review": "待人工审核",
    "ready": "等待发布",
    "publishing": "正在发布",
    "submitted": "等待平台回执",
    "completed": "已完成",
    "failed": "处理失败",
    "skipped": "已跳过",
}


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _load_rows(
    db_path: str | Path,
    table_name: str,
    *,
    columns: str = "*",
    status: str | None = None,
) -> list[dict[str, Any]]:
    conn = _read_only_connection(db_path)
    if conn is None:
        return []
    try:
        if not _table_exists(conn, table_name):
            return []
        query = f"SELECT {columns} FROM {table_name}"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            parameters = (status,)
        query += " ORDER BY updated_at DESC"
        rows = conn.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return datetime.min


def _is_today(value: Any, today: datetime) -> bool:
    return _parse_timestamp(value).date() == today.date()


def _status_tone(status: str, *, source: str) -> str:
    if status in {LEGACY_REVIEW_STATUS, TRANSFER_REVIEW_STATUS}:
        return "warning"
    if status in {LEGACY_COMPLETED_STATUS, TRANSFER_COMPLETED_STATUS}:
        return "success"
    if status in {LEGACY_FAILED_STATUS, TRANSFER_FAILED_STATUS}:
        return "danger"
    if status in {LEGACY_PENDING_STATUS, "discovered"}:
        return "muted"
    if source == "transfer" and status == "skipped":
        return "muted"
    return "primary"


def _legacy_next_action(status: str) -> str:
    if status == LEGACY_REVIEW_STATUS:
        return "去审核"
    if status == LEGACY_READY_STATUS:
        return "准备发布"
    if status == LEGACY_FAILED_STATUS:
        return "查看失败原因"
    if status == LEGACY_COMPLETED_STATUS:
        return "查看结果"
    if status == LEGACY_PENDING_STATUS:
        return "查看任务"
    return "查看进度"


def _transfer_next_action(status: str) -> str:
    if status == TRANSFER_REVIEW_STATUS:
        return "去审核"
    if status == "ready":
        return "准备发布"
    if status == "failed":
        return "查看失败原因"
    if status == "completed":
        return "查看结果"
    if status == "discovered":
        return "准备素材"
    if status in {"downloading", "publishing", "submitted"}:
        return "查看进度"
    return "查看详情"


def _legacy_recent_item(
    row: dict[str, Any],
    route_builder: Callable[..., str],
) -> dict[str, Any]:
    task_id = str(row.get("id") or "")
    status = str(row.get("status") or "")
    detail_url = route_builder("edit_task", task_id=task_id)
    thumbnail_url = (
        route_builder("get_task_cover", task_id=task_id)
        if row.get("cover_path_local")
        else ""
    )
    return {
        "id": task_id,
        "source": "legacy",
        "title": str(
            row.get("video_title_translated")
            or row.get("video_title_original")
            or "未获取标题"
        ),
        "status": status,
        "status_label": LEGACY_STATUS_LABELS.get(status, status or "未知状态"),
        "status_tone": _status_tone(status, source="legacy"),
        "updated_at": str(row.get("updated_at") or ""),
        "detail_url": detail_url,
        "thumbnail_url": thumbnail_url,
        "next_action_label": _legacy_next_action(status),
        "next_action_url": detail_url,
    }


def _transfer_recent_item(
    row: dict[str, Any],
    route_builder: Callable[..., str],
) -> dict[str, Any]:
    job_id = str(row.get("id") or "")
    status = str(row.get("status") or "")
    detail_url = route_builder("transfer_center_review_job", job_id=job_id)
    return {
        "id": job_id,
        "source": "transfer",
        "title": str(row.get("title") or "正在获取视频信息"),
        "status": status,
        "status_label": TRANSFER_STATUS_LABELS.get(status, status or "未知状态"),
        "status_tone": _status_tone(status, source="transfer"),
        "updated_at": str(row.get("updated_at") or ""),
        "detail_url": detail_url,
        "thumbnail_url": str(row.get("thumbnail_url") or ""),
        "next_action_label": _transfer_next_action(status),
        "next_action_url": detail_url,
    }


def build_studio_overview(
    legacy_db_path: str | Path,
    transfer_db_path: str | Path,
    route_builder: Callable[..., str],
    *,
    recent_limit: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine both task stores without changing either workflow state."""

    legacy_rows = _load_rows(
        legacy_db_path,
        "tasks",
        columns=(
            "id, status, created_at, updated_at, video_title_original, "
            "video_title_translated, cover_path_local"
        ),
    )
    transfer_rows = _load_rows(
        transfer_db_path,
        "transfer_jobs",
        columns="id, status, created_at, updated_at, title, thumbnail_url",
    )
    legacy_statuses = [str(row.get("status") or "") for row in legacy_rows]
    transfer_statuses = [str(row.get("status") or "") for row in transfer_rows]

    review = legacy_statuses.count(LEGACY_REVIEW_STATUS) + transfer_statuses.count(
        TRANSFER_REVIEW_STATUS
    )
    completed = legacy_statuses.count(LEGACY_COMPLETED_STATUS) + transfer_statuses.count(
        TRANSFER_COMPLETED_STATUS
    )
    failed = legacy_statuses.count(LEGACY_FAILED_STATUS) + transfer_statuses.count(
        TRANSFER_FAILED_STATUS
    )
    active = sum(
        status not in {
            LEGACY_REVIEW_STATUS,
            LEGACY_COMPLETED_STATUS,
            LEGACY_FAILED_STATUS,
        }
        for status in legacy_statuses
    ) + sum(status not in TRANSFER_INACTIVE_STATUSES for status in transfer_statuses)

    recent_items = [
        _legacy_recent_item(row, route_builder) for row in legacy_rows[:recent_limit]
    ] + [
        _transfer_recent_item(row, route_builder) for row in transfer_rows[:recent_limit]
    ]
    recent_items.sort(
        key=lambda item: _parse_timestamp(item.get("updated_at")), reverse=True
    )

    today = now or datetime.now()
    return {
        "total": len(legacy_rows) + len(transfer_rows),
        "review": review,
        "completed": completed,
        "active": active,
        "failed": failed,
        "recent_tasks": recent_items[: max(1, int(recent_limit))],
        # Compatibility values used by the existing homepage contract.
        "pending": legacy_statuses.count(LEGACY_PENDING_STATUS)
        + sum(status in TRANSFER_PENDING_STATUSES for status in transfer_statuses),
        "ready": legacy_statuses.count(LEGACY_READY_STATUS)
        + sum(status in TRANSFER_READY_STATUSES for status in transfer_statuses),
        "created_today": sum(_is_today(row.get("created_at"), today) for row in legacy_rows)
        + sum(_is_today(row.get("created_at"), today) for row in transfer_rows),
        "completed_today": sum(
            row.get("status") == LEGACY_COMPLETED_STATUS
            and _is_today(row.get("updated_at"), today)
            for row in legacy_rows
        )
        + sum(
            row.get("status") == TRANSFER_COMPLETED_STATUS
            and _is_today(row.get("updated_at"), today)
            for row in transfer_rows
        ),
        "failed_today": sum(
            row.get("status") == LEGACY_FAILED_STATUS
            and _is_today(row.get("updated_at"), today)
            for row in legacy_rows
        )
        + sum(
            row.get("status") == TRANSFER_FAILED_STATUS
            and _is_today(row.get("updated_at"), today)
            for row in transfer_rows
        ),
    }


def load_transfer_review_jobs(transfer_db_path: str | Path) -> list[dict[str, Any]]:
    """Return only jobs whose persisted business status is really ``review``."""

    return _load_rows(
        transfer_db_path,
        "transfer_jobs",
        status=TRANSFER_REVIEW_STATUS,
    )
