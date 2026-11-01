#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Server-side Douyin OAuth and video publishing integration."""

from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import requests

from .utils import get_app_subdir


DOUYIN_SCOPE = "video.create.bind"
AUTHORIZE_URL = "https://open.douyin.com/platform/oauth/connect/"
ACCESS_TOKEN_URL = "https://open.douyin.com/oauth/access_token/"
REFRESH_TOKEN_URL = "https://open.douyin.com/oauth/refresh_token/"
UPLOAD_URL = "https://open.douyin.com/api/douyin/v1/video/upload_video/"
PART_INIT_URL = "https://open.douyin.com/api/douyin/v1/video/init_video_part_upload/"
PART_UPLOAD_URL = "https://open.douyin.com/api/douyin/v1/video/upload_video_part/"
PART_COMPLETE_URL = "https://open.douyin.com/api/douyin/v1/video/complete_video_part_upload/"
CREATE_URL = "https://open.douyin.com/api/douyin/v1/video/create_video/"
DIRECT_UPLOAD_LIMIT = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
PART_SIZE = 20 * 1024 * 1024


class DouyinOpenApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        reconnect_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reconnect_required = reconnect_required


def _paths() -> tuple[Path, Path]:
    config_dir = Path(get_app_subdir("config"))
    return (
        config_dir / "douyin_openapi_app.json",
        config_dir / "douyin_openapi_token.json",
    )


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            value = json.load(file_obj)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=True
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_douyin_oauth_redirect_uri(
    public_base_url: str = "",
    *,
    request_host: str = "",
    request_scheme: str = "http",
) -> str:
    base_url = str(public_base_url or "").strip().rstrip("/")
    if not base_url:
        host = str(request_host or "").strip().split(",", 1)[0].strip()
        scheme = str(request_scheme or "http").strip().split(",", 1)[0].strip()
        if not host:
            raise ValueError("无法确定抖音 OAuth 公网回调地址")
        if host.split(":", 1)[0].lower() == "transfer.sg99.online":
            scheme = "https"
        base_url = f"{scheme}://{host}"
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("抖音 OAuth 公网地址格式无效")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("抖音 OAuth 公网地址必须使用 HTTPS")
    return f"{base_url}/transfer-center/douyin/callback"


def load_douyin_app_credentials() -> dict:
    app_path, _ = _paths()
    return _read_json(app_path)


def save_douyin_app_credentials(client_key: str, client_secret: str = "") -> dict:
    app_path, _ = _paths()
    existing = _read_json(app_path)
    normalized_key = str(client_key or "").strip()
    normalized_secret = str(client_secret or "").strip() or str(
        existing.get("client_secret") or ""
    ).strip()
    if not normalized_key:
        raise ValueError("请填写抖音开放平台 Client Key")
    if not normalized_secret:
        raise ValueError("请填写抖音开放平台 Client Secret")
    payload = {"client_key": normalized_key, "client_secret": normalized_secret}
    _write_private_json(app_path, payload)
    return payload


def load_douyin_token() -> dict:
    _, token_path = _paths()
    return _read_json(token_path)


def douyin_connection_state() -> dict:
    credentials = load_douyin_app_credentials()
    token = load_douyin_token()
    app_configured = bool(
        str(credentials.get("client_key") or "").strip()
        and str(credentials.get("client_secret") or "").strip()
    )
    scope = str(token.get("scope") or "")
    granted_scopes = {item.strip() for item in scope.replace(",", " ").split() if item.strip()}
    token_exists = bool(token.get("access_token") and token.get("open_id"))
    refresh_valid = float(token.get("refresh_expires_at") or 0) > time.time() + 60
    connected = app_configured and token_exists and refresh_valid and DOUYIN_SCOPE in granted_scopes
    if not app_configured:
        status = "unconfigured"
        message = "等待填写抖音开放平台 Client Key 与 Client Secret"
    elif not token_exists:
        status = "authorization_required"
        message = "应用凭据已保存，等待连接抖音发布账号"
    elif not refresh_valid:
        status = "reconnect_required"
        message = "抖音授权已过期，请重新连接发布账号"
    elif DOUYIN_SCOPE not in granted_scopes:
        status = "scope_missing"
        message = "账号已授权，但未授予 video.create.bind 发布能力"
    else:
        status = "connected"
        message = "抖音发布账号已连接"
    return {
        "app_configured": app_configured,
        "client_key": str(credentials.get("client_key") or ""),
        "connected": connected,
        "status": status,
        "message": message,
        "scope_ready": DOUYIN_SCOPE in granted_scopes,
        "open_id": str(token.get("open_id") or ""),
        "expires_at": float(token.get("expires_at") or 0),
    }


def build_douyin_authorization_url(redirect_uri: str, state: str) -> str:
    credentials = load_douyin_app_credentials()
    client_key = str(credentials.get("client_key") or "").strip()
    if not client_key or not credentials.get("client_secret"):
        raise ValueError("请先保存抖音开放平台应用凭据")
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_key": client_key,
            "response_type": "code",
            "scope": DOUYIN_SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def _json_response(response: Any, label: str) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        raise DouyinOpenApiError(f"{label}返回了无法识别的响应") from exc
    if not isinstance(payload, dict):
        raise DouyinOpenApiError(f"{label}返回格式无效")
    return payload


def _raise_for_payload(payload: dict, label: str) -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    code = data.get("error_code") or extra.get("error_code") or payload.get("error_code") or 0
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = code
    if numeric_code not in (0, "0", None):
        description = str(
            data.get("description")
            or extra.get("description")
            or extra.get("sub_description")
            or payload.get("message")
            or "接口调用失败"
        ).strip()
        friendly = {
            10007: "抖音授权码已过期，请重新连接",
            10010: "抖音 refresh_token 已过期，请重新连接",
            10013: "Client Key 或 Client Secret 不正确",
            10003: "Client Secret 不正确",
            28001003: "抖音授权无效，请重新连接",
            28001008: "抖音授权已过期，请重新连接",
            28001014: "抖音应用尚未获得任何开放能力",
            28001018: "抖音应用尚未获批‘代替用户发布内容到抖音’能力",
            2114006: "视频时长超过抖音接口允许的 15 分钟",
            2114007: "已达到抖音接口当日发布上限",
            2190005: "视频文件超过直传限制",
            2190007: "抖音返回了无效的视频文件 ID",
        }.get(numeric_code, description)
        raise DouyinOpenApiError(
            f"{label}失败：{friendly}",
            code=numeric_code,
            reconnect_required=numeric_code in {10007, 10010, 28001003, 28001008},
        )
    return data


def _save_token_data(data: dict, previous: dict | None = None) -> dict:
    _, token_path = _paths()
    existing = previous or {}
    now = time.time()
    refresh_expires_in = int(data.get("refresh_expires_in") or 0)
    payload = {
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": str(data.get("refresh_token") or existing.get("refresh_token") or ""),
        "open_id": str(data.get("open_id") or existing.get("open_id") or ""),
        "scope": str(data.get("scope") or existing.get("scope") or ""),
        "expires_at": now + max(0, int(data.get("expires_in") or 0)) - 60,
        "refresh_expires_at": (
            now + refresh_expires_in - 60
            if refresh_expires_in > 0
            else float(existing.get("refresh_expires_at") or 0)
        ),
    }
    if not payload["access_token"] or not payload["open_id"]:
        raise DouyinOpenApiError("抖音授权响应缺少 access_token 或 open_id")
    _write_private_json(token_path, payload)
    return payload


def exchange_douyin_code(code: str, *, http: Any = requests) -> dict:
    credentials = load_douyin_app_credentials()
    response = http.post(
        ACCESS_TOKEN_URL,
        data={
            "client_key": credentials.get("client_key", ""),
            "client_secret": credentials.get("client_secret", ""),
            "code": str(code or "").strip(),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    payload = _json_response(response, "抖音授权")
    data = _raise_for_payload(payload, "抖音授权")
    return _save_token_data(data)


def refresh_douyin_access_token(*, http: Any = requests) -> dict:
    credentials = load_douyin_app_credentials()
    current = load_douyin_token()
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not refresh_token:
        raise DouyinOpenApiError("抖音授权缺少 refresh_token，请重新连接", reconnect_required=True)
    response = http.post(
        REFRESH_TOKEN_URL,
        data={
            "client_key": credentials.get("client_key", ""),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    data = _raise_for_payload(_json_response(response, "刷新抖音授权"), "刷新抖音授权")
    return _save_token_data(data, previous=current)


def get_valid_douyin_token(*, http: Any = requests) -> dict:
    token = load_douyin_token()
    if not token.get("access_token") or not token.get("open_id"):
        raise DouyinOpenApiError("抖音发布账号尚未连接", reconnect_required=True)
    if float(token.get("expires_at") or 0) <= time.time() + 120:
        token = refresh_douyin_access_token(http=http)
    return token


def _post_api(
    url: str,
    token: dict,
    label: str,
    *,
    http: Any,
    params: dict | None = None,
    files: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 300,
) -> dict:
    response = http.post(
        url,
        headers={"access-token": token["access_token"]},
        params={"open_id": token["open_id"], **(params or {})},
        files=files,
        json=json_body,
        timeout=timeout,
    )
    return _raise_for_payload(_json_response(response, label), label)


def _extract_video_id(data: dict, label: str) -> str:
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    video_id = str(video.get("video_id") or data.get("video_id") or "").strip()
    if not video_id:
        raise DouyinOpenApiError(f"{label}成功但未返回 video_id")
    return video_id


def _upload_video(
    video_path: Path,
    token: dict,
    *,
    progress_callback: Callable[[float], None] | None,
    http: Any,
) -> str:
    size = video_path.stat().st_size
    media_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    if size <= DIRECT_UPLOAD_LIMIT:
        with video_path.open("rb") as file_obj:
            data = _post_api(
                UPLOAD_URL,
                token,
                "抖音视频上传",
                http=http,
                files={"video": (video_path.name, file_obj, media_type)},
            )
        if progress_callback:
            progress_callback(0.85)
        return _extract_video_id(data, "抖音视频上传")

    init_data = _post_api(PART_INIT_URL, token, "抖音分片初始化", http=http, json_body={})
    upload_id = str(init_data.get("upload_id") or "").strip()
    if not upload_id:
        raise DouyinOpenApiError("抖音分片初始化成功但未返回 upload_id")
    uploaded = 0
    part_number = 1
    with video_path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(PART_SIZE)
            if not chunk:
                break
            _post_api(
                PART_UPLOAD_URL,
                token,
                f"抖音第 {part_number} 个分片上传",
                http=http,
                params={"upload_id": upload_id, "part_number": part_number},
                files={"video": (video_path.name, chunk, media_type)},
            )
            uploaded += len(chunk)
            if progress_callback:
                progress_callback(min(0.82, uploaded / size * 0.82))
            part_number += 1
    complete_data = _post_api(
        PART_COMPLETE_URL,
        token,
        "抖音分片合并",
        http=http,
        params={"upload_id": upload_id},
        json_body={},
    )
    return _extract_video_id(complete_data, "抖音分片合并")


def publish_douyin_video(
    video_path: str,
    text: str,
    *,
    private_status: int = 0,
    progress_callback: Callable[[float], None] | None = None,
    http: Any = requests,
) -> dict:
    path = Path(str(video_path or "")).resolve()
    if not path.is_file():
        raise ValueError("抖音发布视频不存在")
    if path.stat().st_size <= 0:
        raise ValueError("抖音发布视频为空")
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError("抖音接口单个视频最大支持 4GB")
    token = get_valid_douyin_token(http=http)
    video_id = _upload_video(
        path,
        token,
        progress_callback=progress_callback,
        http=http,
    )
    if progress_callback:
        progress_callback(0.9)
    data = _post_api(
        CREATE_URL,
        token,
        "抖音视频创建",
        http=http,
        json_body={
            "video_id": video_id,
            "text": str(text or "").strip()[:1000],
            "private_status": private_status if private_status in {0, 1, 2} else 0,
            "download_type": 0,
        },
    )
    item_id = str(data.get("item_id") or "").strip()
    published_video_id = str(data.get("video_id") or video_id).strip()
    if not item_id:
        raise DouyinOpenApiError("抖音视频创建成功但未返回 item_id")
    if progress_callback:
        progress_callback(1.0)
    return {"item_id": item_id, "video_id": published_video_id}
