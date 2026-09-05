#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Small, dependency-free Douyin download fallback for the transfer center.

The primary downloader remains yt-dlp.  This module only resolves public Douyin
share pages when that extractor is temporarily unable to do so.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests


MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_REDIRECTS = 6

SOURCE_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
MEDIA_HOST_SUFFIXES = (
    "douyin.com",
    "douyinvod.com",
    "bytecdn.cn",
    "bytedance.com",
    "amemv.com",
    "snssdk.com",
)


class DouyinDownloadError(RuntimeError):
    """Raised when the constrained Douyin fallback cannot safely continue."""


def _host_allowed(url: str, suffixes: tuple[str, ...]) -> bool:
    parsed = urlparse(str(url or "").strip())
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)
    )


def _validate_url(url: str, suffixes: tuple[str, ...], label: str) -> str:
    normalized = str(url or "").strip()
    if not _host_allowed(normalized, suffixes):
        raise DouyinDownloadError(f"{label}地址不在允许的抖音域名范围内")
    return normalized


def _get_with_redirect_guard(
    session: requests.Session,
    url: str,
    *,
    allowed_suffixes: tuple[str, ...],
    timeout: tuple[int, int],
    stream: bool = False,
) -> requests.Response:
    current = _validate_url(url, allowed_suffixes, "请求")
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(
            current,
            allow_redirects=False,
            timeout=timeout,
            stream=stream,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            return response
        location = str(response.headers.get("location") or "").strip()
        response.close()
        if not location:
            raise DouyinDownloadError("抖音重定向缺少目标地址")
        current = _validate_url(
            urljoin(current, location),
            allowed_suffixes,
            "重定向",
        )
    raise DouyinDownloadError("抖音链接重定向次数过多")


def _extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "note_id", "item_id", "video_id"):
        value = str((query.get(key) or [""])[0]).strip()
        if re.fullmatch(r"\d{10,30}", value):
            return value
    match = re.search(r"/(?:video|note|share/video)/(\d{10,30})(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    raise DouyinDownloadError("无法从抖音链接识别视频编号")


def _extract_router_data(page_text: str) -> dict[str, Any]:
    marker = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
        page_text,
        flags=re.DOTALL,
    )
    if not marker:
        raise DouyinDownloadError("抖音页面没有返回可解析的视频数据")
    try:
        data = json.loads(marker.group(1))
    except json.JSONDecodeError as exc:
        raise DouyinDownloadError("抖音视频数据格式无效") from exc
    if not isinstance(data, dict):
        raise DouyinDownloadError("抖音视频数据格式无效")
    return data


def _extract_video_info(data: dict[str, Any]) -> dict[str, str]:
    loader_data = data.get("loaderData")
    if not isinstance(loader_data, dict):
        raise DouyinDownloadError("抖音视频信息缺少 loaderData")
    video_info: dict[str, Any] | None = None
    for value in loader_data.values():
        if isinstance(value, dict) and isinstance(value.get("videoInfoRes"), dict):
            video_info = value["videoInfoRes"]
            break
    item_list = video_info.get("item_list") if video_info else None
    item = item_list[0] if isinstance(item_list, list) and item_list else None
    if not isinstance(item, dict):
        raise DouyinDownloadError("抖音页面没有返回视频条目")
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    play_addr = video.get("play_addr") if isinstance(video.get("play_addr"), dict) else {}
    uri = str(play_addr.get("uri") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._~-]{6,300}", uri):
        raise DouyinDownloadError("抖音页面没有返回有效播放标识")
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return {
        "title": str(item.get("desc") or "").strip()[:500],
        "uploader": str(author.get("nickname") or "").strip()[:300],
        "play_url": f"https://www.douyin.com/aweme/v1/play/?video_id={uri}",
    }


def _read_limited_page(response: requests.Response) -> str:
    content_length = str(response.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > MAX_PAGE_BYTES:
        raise DouyinDownloadError("抖音详情页超过安全大小限制")
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_PAGE_BYTES:
            raise DouyinDownloadError("抖音详情页超过安全大小限制")
    return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _write_video(response: requests.Response, output_path: Path, max_bytes: int) -> None:
    content_length = str(response.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > max_bytes:
        raise DouyinDownloadError("抖音视频超过下载大小限制")
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type and not (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    ):
        raise DouyinDownloadError("抖音播放地址没有返回视频内容")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(f"{output_path.name}.part")
    written = 0
    first_bytes = b""
    try:
        with part_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if not first_bytes:
                    first_bytes = chunk[:32]
                written += len(chunk)
                if written > max_bytes:
                    raise DouyinDownloadError("抖音视频超过下载大小限制")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if written == 0 or (not content_type and b"ftyp" not in first_bytes):
            raise DouyinDownloadError("抖音播放地址返回的内容不是有效视频")
        part_path.replace(output_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise


def download_douyin_video(
    source_url: str,
    output_path: str | Path,
    *,
    session: requests.Session | None = None,
    max_bytes: int = MAX_VIDEO_BYTES,
) -> dict[str, str]:
    """Download one public Douyin video to ``output_path`` using guarded I/O."""
    source = _validate_url(source_url, SOURCE_HOST_SUFFIXES, "来源")
    http = session or requests.Session()
    owns_session = session is None
    try:
        try:
            video_id = _extract_video_id(source)
            real_url = source
        except DouyinDownloadError:
            resolved = _get_with_redirect_guard(
                http,
                source,
                allowed_suffixes=SOURCE_HOST_SUFFIXES,
                timeout=(10, 20),
                stream=True,
            )
            real_url = str(resolved.url or source)
            resolved.close()
            video_id = _extract_video_id(real_url)

        page = _get_with_redirect_guard(
            http,
            f"https://www.iesdouyin.com/share/video/{video_id}/",
            allowed_suffixes=SOURCE_HOST_SUFFIXES,
            timeout=(10, 20),
            stream=True,
        )
        try:
            info = _extract_video_info(_extract_router_data(_read_limited_page(page)))
        finally:
            page.close()

        media = _get_with_redirect_guard(
            http,
            info["play_url"],
            allowed_suffixes=MEDIA_HOST_SUFFIXES,
            timeout=(10, 60),
            stream=True,
        )
        try:
            _write_video(media, Path(output_path), max(1, int(max_bytes)))
        finally:
            media.close()
        return {
            "id": video_id,
            "title": info["title"],
            "uploader": info["uploader"],
            "webpage_url": real_url,
            "extractor": "douyin_public_fallback",
        }
    except DouyinDownloadError:
        raise
    except requests.RequestException as exc:
        raise DouyinDownloadError("抖音备用解析网络请求失败") from exc
    except OSError as exc:
        raise DouyinDownloadError("抖音视频写入失败") from exc
    finally:
        if owns_session:
            http.close()
