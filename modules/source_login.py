#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Shared safety helpers for local source-platform browser login."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


PLATFORM_LOGIN_SPECS = {
    "bilibili": {
        "label": "B站",
        "login_url": "https://passport.bilibili.com/login",
        "cookie_filename": "bilibili_source_cookies.txt",
        "domains": ("bilibili.com",),
        "required_any": (("SESSDATA",),),
    },
    "douyin": {
        "label": "抖音",
        "login_url": "https://www.douyin.com/",
        "cookie_filename": "douyin_cookies.txt",
        "domains": ("douyin.com", "bytedance.com", "amemv.com"),
        "required_any": (("sessionid", "sessionid_ss"),),
    },
}


def normalize_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform not in PLATFORM_LOGIN_SPECS:
        raise ValueError("不支持的来源平台")
    return platform


def filter_platform_cookies(
    cookies: Iterable[dict[str, Any]],
    platform: str,
) -> list[dict[str, Any]]:
    normalized = normalize_platform(platform)
    domains = PLATFORM_LOGIN_SPECS[normalized]["domains"]
    filtered: list[dict[str, Any]] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        name = str(cookie.get("name") or "").strip()
        if not name or not any(domain == item or domain.endswith(f".{item}") for item in domains):
            continue
        filtered.append(dict(cookie))
    return filtered


def has_authenticated_session(cookies: Iterable[dict[str, Any]], platform: str) -> bool:
    normalized = normalize_platform(platform)
    names = {
        str(cookie.get("name") or "").strip()
        for cookie in filter_platform_cookies(cookies, normalized)
    }
    required_groups = PLATFORM_LOGIN_SPECS[normalized]["required_any"]
    return all(any(name in names for name in group) for group in required_groups)


def build_netscape_cookie_text(
    cookies: Iterable[dict[str, Any]],
    platform: str,
) -> str:
    normalized = normalize_platform(platform)
    filtered = filter_platform_cookies(cookies, normalized)
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated locally by 视频搬运通道. Do not share this file.",
        "",
    ]
    for cookie in sorted(
        filtered,
        key=lambda item: (
            str(item.get("domain") or ""),
            str(item.get("path") or "/"),
            str(item.get("name") or ""),
        ),
    ):
        domain = str(cookie.get("domain") or "").strip()
        if not domain:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        domain_field = f"#HttpOnly_{domain}" if cookie.get("httpOnly") else domain
        path = str(cookie.get("path") or "/").strip() or "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        try:
            expires = max(0, int(float(cookie.get("expires") or 0)))
        except (TypeError, ValueError):
            expires = 0
        name = str(cookie.get("name") or "").replace("\t", "")
        value = str(cookie.get("value") or "").replace("\t", "")
        lines.append(
            "\t".join(
                (
                    domain_field,
                    include_subdomains,
                    path,
                    secure,
                    str(expires),
                    name,
                    value,
                )
            )
        )
    return "\n".join(lines) + "\n"


def write_netscape_cookie_file(
    cookies: Iterable[dict[str, Any]],
    platform: str,
    destination: str | os.PathLike[str],
) -> int:
    normalized = normalize_platform(platform)
    filtered = filter_platform_cookies(cookies, normalized)
    if not has_authenticated_session(filtered, normalized):
        raise ValueError(f"{PLATFORM_LOGIN_SPECS[normalized]['label']}登录状态尚未确认")
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = build_netscape_cookie_text(filtered, normalized)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file_obj:
            file_obj.write(text)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return len(filtered)


def validate_local_return_url(value: Any, allowed_port: int = 5188) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != allowed_port
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/transfer-center/source-login/result")
    ):
        raise ValueError("登录完成后的返回地址无效")
    return text


def create_login_authorization(
    secret: str,
    platform: str,
    return_url: str,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    normalized = normalize_platform(platform)
    safe_return = validate_local_return_url(return_url)
    issued_at = str(int(now if now is not None else time.time()))
    request_nonce = str(nonce or secrets.token_urlsafe(18)).strip()
    if len(secret) < 32 or len(request_nonce) < 12:
        raise ValueError("本机登录授权参数无效")
    message = "\n".join((normalized, safe_return, issued_at, request_nonce))
    signature = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "platform": normalized,
        "return_url": safe_return,
        "issued_at": issued_at,
        "nonce": request_nonce,
        "signature": signature,
    }


def verify_login_authorization(
    secret: str,
    payload: dict[str, Any],
    *,
    max_age_seconds: int = 90,
    now: int | None = None,
) -> tuple[str, str]:
    platform = normalize_platform(payload.get("platform"))
    return_url = validate_local_return_url(payload.get("return_url"))
    try:
        issued_at = int(str(payload.get("issued_at") or ""))
    except ValueError as exc:
        raise ValueError("本机登录请求已失效") from exc
    nonce = str(payload.get("nonce") or "").strip()
    signature = str(payload.get("signature") or "").strip()
    current_time = int(now if now is not None else time.time())
    if (
        len(secret) < 32
        or len(nonce) < 12
        or abs(current_time - issued_at) > max(15, int(max_age_seconds))
    ):
        raise ValueError("本机登录请求已失效")
    expected = create_login_authorization(
        secret,
        platform,
        return_url,
        now=issued_at,
        nonce=nonce,
    )["signature"]
    if not hmac.compare_digest(expected, signature):
        raise ValueError("本机登录请求验证失败")
    return platform, return_url
