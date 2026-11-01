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
from typing import Any, Mapping


X_MAX_BYTES = 512 * 1024 * 1024
X_MAX_DURATION_SECONDS = 140.0
YOUTUBE_SHORT_MAX_DURATION_SECONDS = 180.0
YOUTUBE_DEFAULT_MAX_DURATION_SECONDS = 900.0

TARGET_ASPECT_SIZES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
}
X_TARGET_ASPECT_SIZES = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}
TEXT_SUBTITLE_CODECS = {
    "ass",
    "mov_text",
    "ssa",
    "srt",
    "subrip",
    "text",
    "webvtt",
}


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
    subtitle_streams = [
        {
            "codec": str(stream.get("codec_name") or "").lower(),
            "language": str((stream.get("tags") or {}).get("language") or ""),
        }
        for stream in streams or []
        if stream.get("codec_type") == "subtitle"
    ]
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
        "has_subtitles": bool(subtitle_streams),
        "subtitle_streams": subtitle_streams,
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

    if "bilibili" in selected_targets:
        plan["bilibili"] = {
            "route": "standard_upload",
            "label": "B站普通投稿",
            "note": "确认分区、标题和来源说明后，由服务器自动上传。",
        }

    if "douyin" in selected_targets:
        plan["douyin"] = {
            "route": "creator_web",
            "label": "抖音创作者中心",
            "note": "系统准备成片与文案，最后在抖音创作者中心确认发布。",
        }

    if "tiktok" in selected_targets:
        plan["tiktok"] = {
            "route": "creator_web",
            "label": "TikTok Studio",
            "note": "当前为免费半自动网页发布；官方 API 审核通过后再切换自动发布。",
        }

    return plan


def _normalize_target_aspect(
    value: Any,
    *,
    platform: str,
    info: Mapping[str, Any],
) -> str:
    text = str(value or "").strip().lower().replace("×", ":")
    aliases = {
        "16:9": "16:9",
        "16/9": "16:9",
        "landscape": "16:9",
        "horizontal": "16:9",
        "横版": "16:9",
        "9:16": "9:16",
        "9/16": "9:16",
        "portrait": "9:16",
        "vertical": "9:16",
        "竖版": "9:16",
    }
    if text:
        if text in {"auto", "source"}:
            return "9:16" if _safe_int(info.get("height")) >= _safe_int(info.get("width")) else "16:9"
        if text not in aliases:
            raise ValueError(f"{platform} 目标画幅无效：{value}")
        return aliases[text]
    if platform in {"douyin", "tiktok"}:
        return "9:16"
    if platform == "youtube":
        is_vertical = _safe_int(info.get("height")) >= _safe_int(info.get("width"))
        is_short = 0 < _safe_float(info.get("duration")) <= YOUTUBE_SHORT_MAX_DURATION_SECONDS
        return "9:16" if is_vertical and is_short else "16:9"
    return "16:9"


def _software_encoder_args(cpu_preset: str = "veryfast") -> tuple[str, list[str], bool, str]:
    return (
        "libx264",
        ["-c:v", "libx264", "-preset", cpu_preset, "-crf", "22"],
        False,
        "software_fallback",
    )


def _preferred_encoder_args(
    ffmpeg_bin: str,
    *,
    prefer_videotoolbox: bool,
    bitrate_kbps: int,
) -> tuple[str, list[str], bool, str]:
    try:
        from .videotoolbox_encoder import build_video_encoder_plan

        plan = build_video_encoder_plan(
            ffmpeg_bin,
            codec="h264",
            bitrate_kbps=bitrate_kbps,
            prefer_hardware=prefer_videotoolbox,
            cpu_preset="veryfast",
        )
        return (
            str(plan.encoder_name),
            list(plan.ffmpeg_args),
            bool(plan.hardware_accelerated),
            str(plan.reason),
        )
    except Exception as exc:
        encoder, args, hardware, _ = _software_encoder_args()
        return encoder, args, hardware, f"encoder_probe_failed:{type(exc).__name__}"


def _canvas_filter(
    width: int, height: int, *, max_fps: int | None = None
) -> str:
    """Fit the full foreground over a blurred fill; never crop the subject."""

    fps_filter = f",fps={max_fps}" if max_fps else ""
    return (
        "[0:v:0]split=2[background_source][foreground_source];"
        f"[background_source]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=24[background];"
        f"[foreground_source]scale={width}:{height}:force_original_aspect_ratio=decrease[foreground];"
        "[background][foreground]overlay=(W-w)/2:(H-h)/2,setsar=1,"
        f"format=yuv420p{fps_filter}[video]"
    )


def _scaled_filter(
    max_width: int = 1280,
    max_height: int = 1280,
    *,
    max_fps: int | None = None,
) -> str:
    fps_filter = f",fps={max_fps}" if max_fps else ""
    return (
        "[0:v:0]"
        f"scale=w='min({max_width},iw)':h='min({max_height},ih)':"
        "force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"format=yuv420p{fps_filter}[video]"
    )


def _source_ready_for_aspect(
    source_path: str,
    info: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> bool:
    """Keep a proven MP4 when it already exactly matches the delivery shape."""

    return all(
        (
            Path(source_path).suffix.lower() == ".mp4",
            _safe_int(info.get("width")) == width,
            _safe_int(info.get("height")) == height,
            str(info.get("video_codec") or "").lower() == "h264",
            not info.get("has_audio")
            or str(info.get("audio_codec") or "").lower() == "aac",
            0 < _safe_float(info.get("fps")) <= 60,
            str(info.get("pix_fmt") or "").lower() in {"yuv420p", "yuvj420p"},
        )
    )


def _transcode_variant(
    source_path: str,
    output_path: str,
    info: Mapping[str, Any],
    ffmpeg_bin: str,
    *,
    video_filter: str,
    prefer_videotoolbox: bool,
    bitrate_kbps: int,
) -> dict[str, Any]:
    unsupported_subtitles = [
        str(item.get("codec") or "")
        for item in (info.get("subtitle_streams") or [])
        if str(item.get("codec") or "").lower() not in TEXT_SUBTITLE_CODECS
    ]
    if unsupported_subtitles:
        raise RuntimeError(
            "检测到无法安全封装进 MP4 的图片字幕轨："
            + "、".join(sorted(set(unsupported_subtitles)))
            + "；请先提供已烧录字幕的目标语言成片"
        )

    preferred = _preferred_encoder_args(
        ffmpeg_bin,
        prefer_videotoolbox=prefer_videotoolbox,
        bitrate_kbps=bitrate_kbps,
    )
    attempts = [preferred]
    if preferred[2]:
        attempts.append(_software_encoder_args())

    target = Path(output_path)
    partial = target.with_name(f"{target.stem}.part{target.suffix}")
    errors: list[str] = []
    for encoder_name, encoder_args, hardware, reason in attempts:
        if partial.exists():
            partial.unlink()
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            source_path,
            "-filter_complex",
            video_filter,
            "-map",
            "[video]",
        ]
        if info.get("has_audio"):
            cmd.extend(
                ["-map", "0:a?", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
            )
        else:
            cmd.append("-an")
        if info.get("has_subtitles"):
            cmd.extend(["-map", "0:s?", "-c:s", "mov_text"])
        cmd.extend(encoder_args)
        cmd.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "0",
                str(partial),
            ]
        )
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,
            check=False,
        )
        if completed.returncode == 0 and partial.is_file() and partial.stat().st_size > 0:
            os.replace(partial, target)
            return {
                "encoder": encoder_name,
                "hardware_accelerated": hardware,
                "encoder_reason": reason,
            }
        errors.append(
            f"{encoder_name}: {(completed.stderr or '未生成媒体文件').strip()[-600:]}"
        )
    if partial.exists():
        partial.unlink()
    raise RuntimeError("；".join(errors) or "平台媒体版本转换失败")


def _verify_variant(
    source_info: Mapping[str, Any],
    output_info: Mapping[str, Any],
    target_aspect: str,
) -> list[str]:
    issues: list[str] = []
    target_ratio = 16 / 9 if target_aspect == "16:9" else 9 / 16
    width = _safe_int(output_info.get("width"))
    height = _safe_int(output_info.get("height"))
    if not width or not height or abs(width / height - target_ratio) > 0.01:
        issues.append(f"输出画幅不是 {target_aspect}")
    if str(output_info.get("video_codec") or "").lower() != "h264":
        issues.append("输出视频不是 H.264")
    issues.extend(_verify_stream_preservation(source_info, output_info))
    return issues


def _verify_stream_preservation(
    source_info: Mapping[str, Any],
    output_info: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    if source_info.get("has_audio") and not output_info.get("has_audio"):
        issues.append("输出丢失了原有音频")
    if source_info.get("has_subtitles") and not output_info.get("has_subtitles"):
        issues.append("输出丢失了原有字幕轨")
    source_duration = _safe_float(source_info.get("duration"))
    output_duration = _safe_float(output_info.get("duration"))
    tolerance = max(1.0, source_duration * 0.02)
    if source_duration > 0 and abs(source_duration - output_duration) > tolerance:
        issues.append("输出时长与目标语言源文件不一致")
    return issues


def prepare_platform_variants(
    source_path: str,
    output_dir: str,
    targets: list[str],
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    target_sources: Mapping[str, str] | None = None,
    aspect_map: Mapping[str, str] | None = None,
    prefer_videotoolbox: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create target-ready media without cropping foreground content.

    ``target_sources`` lets the caller route a platform to its matching
    language render. ``aspect_map`` may override the normal 16:9/9:16 choice.
    Hardware H.264 is preferred on Apple Silicon and retried with libx264 if an
    actual encode fails. Editorial duration blockers are never silently trimmed.
    """

    info = probe_media(source_path, ffprobe_bin=ffprobe_bin)
    variants: dict[str, Any] = {}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    normalized_sources = {
        str(key).strip().lower(): str(value or "").strip()
        for key, value in (target_sources or {}).items()
    }
    normalized_aspects = {
        str(key).strip().lower(): value for key, value in (aspect_map or {}).items()
    }
    selected_targets = list(
        dict.fromkeys(str(item or "").strip().lower() for item in targets)
    )
    probe_cache: dict[str, dict[str, Any]] = {str(Path(source_path)): info}

    for target in selected_targets:
        if target not in {"bilibili", "douyin", "tiktok", "youtube", "x"}:
            continue
        target_source = normalized_sources.get(target) or source_path
        if not os.path.isfile(target_source):
            variants[target] = {
                "status": "failed",
                "path": "",
                "source_path": target_source,
                "issues": ["目标语言成片文件不存在"],
            }
            continue
        source_key = str(Path(target_source))
        source_info = probe_cache.get(source_key)
        if source_info is None:
            source_info = probe_media(target_source, ffprobe_bin=ffprobe_bin)
            probe_cache[source_key] = source_info

        if target != "x":
            target_aspect = str(normalized_aspects.get(target) or "")
            try:
                target_aspect = _normalize_target_aspect(
                    normalized_aspects.get(target),
                    platform=target,
                    info=source_info,
                )
                width, height = TARGET_ASPECT_SIZES[target_aspect]
                if target not in normalized_sources and _source_ready_for_aspect(
                    target_source,
                    source_info,
                    width=width,
                    height=height,
                ):
                    variants[target] = {
                        "status": "ready",
                        "path": target_source,
                        "source_path": target_source,
                        "target_aspect": target_aspect,
                        "target_width": width,
                        "target_height": height,
                        "issues": [],
                        "normalized": False,
                        "encoder": "source",
                        "hardware_accelerated": False,
                    }
                    continue
                output_path = str(
                    Path(output_dir)
                    / f"{target}-ready-{target_aspect.replace(':', 'x')}.mp4"
                )
                encode = _transcode_variant(
                    target_source,
                    output_path,
                    source_info,
                    ffmpeg_bin,
                    video_filter=_canvas_filter(
                        width,
                        height,
                        max_fps=60 if _safe_float(source_info.get("fps")) > 60 else None,
                    ),
                    prefer_videotoolbox=prefer_videotoolbox,
                    bitrate_kbps=8_000,
                )
                output_info = probe_media(output_path, ffprobe_bin=ffprobe_bin)
                issues = _verify_variant(source_info, output_info, target_aspect)
                if issues and os.path.isfile(output_path):
                    Path(output_path).unlink()
                variants[target] = {
                    "status": "failed" if issues else "ready",
                    "path": "" if issues else output_path,
                    "source_path": target_source,
                    "target_aspect": target_aspect,
                    "target_width": width,
                    "target_height": height,
                    "issues": issues,
                    "normalized": True,
                    **encode,
                }
            except Exception as exc:
                variants[target] = {
                    "status": "failed",
                    "path": "",
                    "source_path": target_source,
                    "target_aspect": target_aspect,
                    "issues": [str(exc)[:900]],
                }
            continue

        assessment = assess_x_compatibility(source_info)
        if assessment["blockers"]:
            variants["x"] = {
                "status": "needs_edit",
                "path": "",
                "source_path": target_source,
                "target_aspect": "source",
                "issues": assessment["blockers"],
            }
            continue
        requested_x_aspect = normalized_aspects.get("x")
        if assessment["transcode_reasons"] or requested_x_aspect:
            try:
                if requested_x_aspect:
                    target_aspect = _normalize_target_aspect(
                        requested_x_aspect,
                        platform="x",
                        info=source_info,
                    )
                    width, height = X_TARGET_ASPECT_SIZES[target_aspect]
                    video_filter = _canvas_filter(
                        width,
                        height,
                        max_fps=60 if _safe_float(source_info.get("fps")) > 60 else None,
                    )
                else:
                    target_aspect = "source"
                    width = height = 0
                    video_filter = _scaled_filter(
                        max_fps=60 if _safe_float(source_info.get("fps")) > 60 else None
                    )
                output_path = str(Path(output_dir) / "x-ready.mp4")
                encode = _transcode_variant(
                    target_source,
                    output_path,
                    source_info,
                    ffmpeg_bin,
                    video_filter=video_filter,
                    prefer_videotoolbox=prefer_videotoolbox,
                    bitrate_kbps=5_000,
                )
                normalized = probe_media(output_path, ffprobe_bin=ffprobe_bin)
                verified = assess_x_compatibility(normalized)
                variant_issues = (
                    verified["blockers"]
                    + verified["transcode_reasons"]
                    + _verify_stream_preservation(source_info, normalized)
                )
                if variant_issues and os.path.isfile(output_path):
                    Path(output_path).unlink()
                variants["x"] = {
                    "status": "failed" if variant_issues else "ready",
                    "path": "" if variant_issues else output_path,
                    "source_path": target_source,
                    "target_aspect": target_aspect,
                    "target_width": width,
                    "target_height": height,
                    "issues": variant_issues,
                    "normalization_reasons": assessment["transcode_reasons"],
                    "normalized": True,
                    **encode,
                }
            except Exception as exc:
                variants["x"] = {
                    "status": "failed",
                    "path": "",
                    "source_path": target_source,
                    "target_aspect": str(requested_x_aspect or "source"),
                    "issues": [str(exc)[:900]],
                }
        else:
            variants["x"] = {
                "status": "ready",
                "path": target_source,
                "source_path": target_source,
                "target_aspect": "source",
                "issues": [],
                "normalized": False,
                "encoder": "source",
                "hardware_accelerated": False,
            }
    return info, variants
