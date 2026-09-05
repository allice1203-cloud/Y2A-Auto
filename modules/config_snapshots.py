#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""不包含凭据的设置快照，用于预览与撤销最近修改。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_app_subdir


_SENSITIVE_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "CREDENTIAL",
    "USERNAME",
    "SENDKEY",
    "WEBHOOK",
    "ACCESS_KEY",
)
_SNAPSHOT_ID_PATTERN = re.compile(r"^config-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def is_sensitive_config_key(key: str) -> bool:
    normalized = str(key or "").strip().upper()
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def safe_config_values(config: dict[str, Any] | None) -> dict[str, Any]:
    """只保留适合落盘的非凭据配置。"""

    return {
        str(key): value
        for key, value in sorted((config or {}).items())
        if not is_sensitive_config_key(str(key))
    }


def _snapshot_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    return Path(get_app_subdir("backups")) / "config-snapshots"


def create_config_snapshot(
    config: dict[str, Any],
    *,
    reason: str = "settings_save",
    directory: str | os.PathLike[str] | None = None,
    keep: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """原子写入脱敏设置快照，并仅保留最近若干份。"""

    root = _snapshot_directory(directory)
    root.mkdir(parents=True, exist_ok=True)
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot_id = f"config-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": created_at.isoformat(timespec="seconds"),
        "reason": str(reason or "settings_save")[:80],
        "config": safe_config_values(config),
    }
    destination = root / f"{snapshot_id}.json"
    temporary = root / f".{snapshot_id}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)

    snapshots = sorted(root.glob("config-*.json"), key=lambda path: path.name, reverse=True)
    for old_path in snapshots[max(1, min(int(keep), 20)) :]:
        old_path.unlink(missing_ok=True)
    return {
        "snapshot_id": snapshot_id,
        "created_at": payload["created_at"],
        "reason": payload["reason"],
        "key_count": len(payload["config"]),
    }


def list_config_snapshots(
    *,
    directory: str | os.PathLike[str] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    root = _snapshot_directory(directory)
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(root.glob("config-*.json"), key=lambda item: item.name, reverse=True):
        if len(results) >= max(1, min(int(limit), 20)):
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        config = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(config, dict):
            continue
        results.append(
            {
                "snapshot_id": str(payload.get("snapshot_id") or path.stem),
                "created_at": str(payload.get("created_at") or ""),
                "reason": str(payload.get("reason") or ""),
                "key_count": len(config),
            }
        )
    return results


def load_config_snapshot(
    snapshot_id: str,
    *,
    directory: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    normalized = str(snapshot_id or "").strip()
    if not _SNAPSHOT_ID_PATTERN.fullmatch(normalized):
        raise ValueError("无效的设置快照")
    path = _snapshot_directory(directory) / f"{normalized}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("设置快照不存在") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("设置快照无法读取") from exc
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ValueError("设置快照内容无效")
    return safe_config_values(config)
