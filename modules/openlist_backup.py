#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Loopback-only OpenList adapter used for verified 115 cloud backups."""

from __future__ import annotations

import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, urlparse

import requests


DEFAULT_OPENLIST_URL = "http://127.0.0.1:5245"
DEFAULT_REMOTE_ROOT = "/115-视频备份/视频搬运"
MAX_METADATA_BYTES = 1024 * 1024


def default_openlist_db_path() -> str:
    return str(
        Path.home()
        / "Library"
        / "Application Support"
        / "SG99"
        / "MediaStack"
        / "openlist"
        / "data.db"
    )


def safe_remote_name(value: Any, fallback: str = "未命名视频") -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return (text or fallback)[:80]


class OpenListBackupError(RuntimeError):
    pass


class OpenListBackupClient:
    """Upload to a local OpenList instance without copying 115 credentials."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENLIST_URL,
        database_path: str = "",
        remote_root: str = DEFAULT_REMOTE_ROOT,
        timeout_seconds: int = 900,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_OPENLIST_URL).strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("OpenList 备份接口必须是本机 loopback HTTP 地址")
        self.database_path = str(database_path or default_openlist_db_path())
        self.remote_root = "/" + str(remote_root or DEFAULT_REMOTE_ROOT).strip("/")
        self.timeout_seconds = max(30, min(7200, int(timeout_seconds or 900)))
        self.session = session or requests.Session()
        self.session.trust_env = False

    def _admin_token(self) -> str:
        database = Path(self.database_path).expanduser()
        if not database.is_file():
            raise OpenListBackupError("本机 OpenList 数据库不存在")
        try:
            connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                row = connection.execute(
                    "SELECT value FROM x_setting_items WHERE key='token'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise OpenListBackupError("无法读取本机 OpenList 授权状态") from exc
        token = str(row[0] if row else "").strip()
        if not token:
            raise OpenListBackupError("本机 OpenList 尚未生成管理令牌")
        return token

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": self._admin_token(), **extra}

    @staticmethod
    def _validate_response(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpenListBackupError(f"OpenList {operation}返回格式无效") from exc
        if not response.ok or int(payload.get("code") or 500) != 200:
            message = str(payload.get("message") or f"HTTP {response.status_code}")[:300]
            raise OpenListBackupError(f"OpenList {operation}失败：{message}")
        return payload

    def mkdir(self, remote_path: str) -> None:
        response = self.session.post(
            f"{self.base_url}/api/fs/mkdir",
            headers=self._headers(**{"Content-Type": "application/json"}),
            json={"path": "/" + str(remote_path or "").strip("/")},
            timeout=(5, 60),
        )
        self._validate_response(response, "创建目录")

    def _put(self, remote_path: str, body: BinaryIO, size: int, content_type: str) -> None:
        normalized = "/" + str(remote_path or "").strip("/")
        response = self.session.put(
            f"{self.base_url}/api/fs/put",
            headers=self._headers(
                **{
                    "File-Path": quote(normalized, safe="/"),
                    "As-Task": "false",
                    "Content-Type": content_type,
                    "Content-Length": str(max(0, int(size))),
                }
            ),
            data=body,
            timeout=(10, self.timeout_seconds),
        )
        self._validate_response(response, "上传文件")

    def upload_file(self, local_path: str, remote_path: str) -> int:
        source = Path(local_path)
        if not source.is_file():
            raise OpenListBackupError(f"待备份文件不存在：{source.name}")
        size = source.stat().st_size
        if size <= 0:
            raise OpenListBackupError(f"待备份文件为空：{source.name}")
        with source.open("rb") as handle:
            self._put(remote_path, handle, size, "application/octet-stream")
        self.verify_file(remote_path, size)
        return size

    def upload_json(self, payload: dict[str, Any], remote_path: str) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise OpenListBackupError("备份元数据超过 1MB")
        self._put(remote_path, io.BytesIO(encoded), len(encoded), "application/json")
        self.verify_file(remote_path, len(encoded))
        return len(encoded)

    def list_directory(self, remote_path: str, *, refresh: bool = True) -> list[dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/api/fs/list",
            headers=self._headers(**{"Content-Type": "application/json"}),
            json={
                "path": "/" + str(remote_path or "").strip("/"),
                "password": "",
                "page": 1,
                "per_page": 100,
                "refresh": bool(refresh),
            },
            timeout=(5, 60),
        )
        payload = self._validate_response(response, "读取目录")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        content = data.get("content") if isinstance(data.get("content"), list) else []
        return [item for item in content if isinstance(item, dict)]

    def verify_file(self, remote_path: str, expected_size: int) -> None:
        normalized = "/" + str(remote_path or "").strip("/")
        parent, name = normalized.rsplit("/", 1)
        item = next(
            (candidate for candidate in self.list_directory(parent or "/") if candidate.get("name") == name),
            None,
        )
        if not item:
            raise OpenListBackupError("115 备份上传后未出现在目标目录")
        if int(item.get("size") or -1) != int(expected_size):
            raise OpenListBackupError("115 备份文件大小校验不一致")

    def health(self) -> dict[str, Any]:
        try:
            items = self.list_directory(self.remote_root, refresh=True)
            return {
                "configured": True,
                "reachable": True,
                "ready": True,
                "remote_root": self.remote_root,
                "item_count": len(items),
                "message": "115 网盘备份目录可用",
            }
        except Exception as exc:
            return {
                "configured": True,
                "reachable": False,
                "ready": False,
                "remote_root": self.remote_root,
                "item_count": 0,
                "message": str(exc)[:300],
            }
