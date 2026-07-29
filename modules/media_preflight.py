#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Media inspection and platform-specific preparation for transfer jobs."""

from __future__ import annotations

import json
import math
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


X_MAX_BYTES = 512 * 1024 * 1024
X_MAX_DURATION_SECONDS = 140.0
YOUTUBE_SHORT_MAX_DURATION_SECONDS = 180.0
YOUTUBE_DEFAULT_MAX_DURATION_SECONDS = 900.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _frame_rate(value: Any) -> float:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return _safe_float(text)


def probe_media(video_path: str, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    if not os.path.isfile(video_path):
        raise ValueError("媒体文件不存在")
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "媒体体检失败").strip()[:900])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("媒体体检结果无法解析") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else []
    video_stream = next(
        (stream for stream in streams or [] if stream.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (stream for stream in streams or [] if stream.get("codec_type") == "audio"),
        {},
    )
    format_info = payload.get("format") if isinstance(payload, dict) else {}
    width = _safe_int(video_stream.get("width"))
    height = _safe_int(video_stream.get("height"))
    duration = _safe_float(format_info.get("duration") or video_stream.get("duration"))
    size_bytes = _safe_int(format_info.get("size"), os.path.getsize(video_path))
    return {
        "path": video_path,
        "container": str(format_info.get("format_name") or ""),
        "duration": duration,
        "size_bytes": size_bytes,
        "video_codec": str(video_stream.get("codec_name") or ""),
        "audio_codec": str(audio_stream.get("codec_name") or ""),
        "width": width,
        "height": height,
        "fps": round(_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")), 3),
        "pix_fmt": str(video_stream.get("pix_fmt") or ""),
        "field_order": str(video_stream.get("field_order") or ""),
        "audio_channels": _safe_int(audio_stream.get("channels")),
        "has_audio": bool(audio_stream),
    }


def assess_x_compatibility(info: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    transcode_reasons: list[str] = []
    duration = _safe_float(info.get("duration"))
    size_bytes = _safe_int(info.get("size_bytes"))
    width = _safe_int(info.get("width"))
    height = _safe_int(info.get("height"))
    fps = _safe_float(info.get("fps"))
    ratio = (width / height) if width and height else 0.0

    if duration <= 0:
        blockers.append("无法识别视频时长")
    elif duration > X_MAX_DURATION_SECONDS:
        blockers.append("视频超过140秒，需要先进行原创剪辑或拆条")
    if size_bytes > X_MAX_BYTES:
        transcode_reasons.append("文件超过512MB")
    if str(info.get("video_codec") or "").lower() != "h264":
        transcode_reasons.append("视频不是H.264")
    if info.get("has_audio") and str(info.get("audio_codec") or "").lower() != "aac":
        transcode_reasons.append("音频不是AAC")
    if _safe_int(info.get("audio_channels")) > 2:
        transcode_reasons.append("音频声道超过立体声")
    if fps > 60:
        transcode_reasons.append("帧率超过60fps")
    if str(info.get("pix_fmt") or "") not in {"yuv420p", "yuvj420p"}:
        transcode_reasons.append("像素格式不是YUV 4:2:0")
    if ratio and not (1 / 3 <= ratio <= 3):
        transcode_reasons.append("画面比例超出1:3至3:1")
    if width > 1280 or height > 1280:
        transcode_reasons.append("画面尺寸需要缩放")

    return {
        "compatible": not blockers and not transcode_reasons,
        "blockers": blockers,
        "transcode_reasons": transcode_reasons,
    }


def build_distribution_plan(
    info: dict[str, Any],
    targets: list[str],
) -> dict[str, Any]:
    """Describe safe editorial routes without mechanically splitting a source."""

    duration = _safe_float(info.get("duration"))
    width = _safe_int(info.get("width"))
    height = _safe_int(info.get("height"))
    selected_targets = {str(item or "").strip().lower() for item in targets}
    is_vertical_or_square = bool(width and height and height >= width)
    plan: dict[str, Any] = {
        "duration_seconds": round(duration, 3),
        "orientation": "vertical_or_square" if is_vertical_or_square else "landscape",
        "same_cut_max_seconds": (
            int(X_MAX_DURATION_SECONDS)
            if {"x", "youtube"}.issubset(selected_targets)
            else None
        ),
        "editorial_notice": (
            "系列拆分必须按完整观点或剧情节点重新剪辑并加入原创串联，"
            "不得把第三方长视频机械切段后连续发布。"
        ),
    }

    if "x" in selected_targets:
        needs_series = duration > X_MAX_DURATION_SECONDS
        plan["x"] = {
            "route": "editorial_series" if needs_series else "single_post",
            "recommended_max_seconds": int(X_MAX_DURATION_SECONDS),
            "estimated_editorial_parts": (
                max(2, int(math.ceil(duration / X_MAX_DURATION_SECONDS)))
                if needs_series and duration > 0
                else 1
            ),
            "requires_editorial_cut": needs_series,
            "label": "需要原创系列剪辑" if needs_series else "可作为单条视频",
        }

    if "youtube" in selected_targets:
        is_short = (
            is_vertical_or_square
            and duration > 0
            and duration <= YOUTUBE_SHORT_MAX_DURATION_SECONDS
        )
        needs_long_upload_access = duration > YOUTUBE_DEFAULT_MAX_DURATION_SECONDS
        plan["youtube"] = {
            "route": "shorts" if is_short else "long_form",
            "recommended_max_seconds": (
                int(YOUTUBE_SHORT_MAX_DURATION_SECONDS) if is_short else None
            ),
            "requires_long_upload_access": needs_long_upload_access,
            "label": "YouTube Shorts" if is_short else "YouTube 长视频",
            "note": (
                "超过15分钟，发布频道需已启用长视频上传资格。"
                if needs_long_upload_access
                else "当前时长不要求超过15分钟的长视频资格。"
            ),
        }

    return plan


def _transcode_x(
    source_path: str,
    output_path: str,
    info: dict[str, Any],
    ffmpeg_bin: str,
) -> None:
    filters = [
        "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    ]
    if _safe_float(info.get("fps")) > 60:
        filters.append("fps=60")
    filters.append("format=yuv420p")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        source_path,
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
    ]
    if info.get("has_audio"):
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])
    else:
        cmd.append("-an")
    cmd.append(output_path)
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=7200,
        check=False,
    )
    if completed.returncode != 0 or not os.path.isfile(output_path):
        raise RuntimeError((completed.stderr or "X平台版本转换失败").strip()[-1200:])


def prepare_platform_variants(
    source_path: str,
    output_dir: str,
    targets: list[str],
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return media probe data and target variant records.

    YouTube can consume the merged MP4 source directly. X receives either the
    source when it already conforms or a local H.264/AAC normalized variant.
    Editorial duration blockers are never solved by silently trimming content.
    """

    info = probe_media(source_path, ffprobe_bin=ffprobe_bin)
    variants: dict[str, Any] = {}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if "youtube" in targets:
        variants["youtube"] = {
            "status": "ready",
            "path": source_path,
            "issues": [],
        }

    if "x" in targets:
        assessment = assess_x_compatibility(info)
        if assessment["blockers"]:
            variants["x"] = {
                "status": "needs_edit",
                "path": "",
                "issues": assessment["blockers"],
            }
        elif assessment["transcode_reasons"]:
            output_path = str(Path(output_dir) / "x-ready.mp4")
            _transcode_x(source_path, output_path, info, ffmpeg_bin)
            normalized = probe_media(output_path, ffprobe_bin=ffprobe_bin)
            verified = assess_x_compatibility(normalized)
            if verified["blockers"] or verified["transcode_reasons"]:
                variants["x"] = {
                    "status": "failed",
                    "path": "",
                    "issues": verified["blockers"] + verified["transcode_reasons"],
                }
            else:
                variants["x"] = {
                    "status": "ready",
                    "path": output_path,
                    "issues": assessment["transcode_reasons"],
                    "normalized": True,
                }
        else:
            variants["x"] = {
                "status": "ready",
                "path": source_path,
                "issues": [],
            }
    return info, variants
