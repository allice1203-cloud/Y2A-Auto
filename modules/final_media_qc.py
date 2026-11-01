#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Local-only technical quality checks for rendered delivery media.

The checks in this module deliberately avoid judging originality or meaning.
They only inspect file/stream structure, timing, black frames, silence, and
subtitle timing with local ffprobe/ffmpeg processes.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping


FULL_SCAN_MAX_SECONDS = 180.0
LONG_SCAN_SAMPLE_SECONDS = 12.0
LONG_SCAN_SAMPLE_COUNT = 5
PROBE_TIMEOUT_SECONDS = 60
MAX_SCAN_TIMEOUT_SECONDS = 180

_BLACK_DURATION_RE = re.compile(r"black_duration\s*:\s*([0-9]+(?:\.[0-9]+)?)")
_SILENCE_DURATION_RE = re.compile(
    r"silence_duration\s*:\s*([0-9]+(?:\.[0-9]+)?)"
)
_TIMED_SUBTITLE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _check(
    check_id: str,
    status: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    severity = {
        "pass": "info",
        "review": "warning",
        "unknown": "warning",
        "blocked": "block",
    }[status]
    result: dict[str, Any] = {
        "id": check_id,
        "status": status,
        "severity": severity,
        "message": message,
    }
    if details:
        result["details"] = details
    return result


def _resolve_media_tools() -> tuple[str | None, str | None]:
    """Resolve only local binaries; this function never downloads a tool."""

    try:
        from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path

        ffmpeg_path = get_ffmpeg_path(allow_system=True)
        ffprobe_path = get_ffprobe_path(
            ffmpeg_path=ffmpeg_path,
            allow_system=True,
        )
        return ffmpeg_path, ffprobe_path
    except Exception:
        return None, None


def _timestamp_seconds(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours = "0"
            minutes, seconds = parts
        else:
            return None
        result = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _stream_duration(stream: Mapping[str, Any]) -> tuple[float, str]:
    direct = _safe_float(stream.get("duration"))
    if direct > 0:
        return direct, "duration"

    duration_ts = _safe_float(stream.get("duration_ts"))
    time_base = str(stream.get("time_base") or "").strip()
    if duration_ts > 0 and time_base:
        try:
            calculated = duration_ts * float(Fraction(time_base))
        except (ValueError, ZeroDivisionError):
            calculated = 0.0
        if calculated > 0 and math.isfinite(calculated):
            return calculated, "duration_ts"

    tag_duration = _timestamp_seconds((stream.get("tags") or {}).get("DURATION"))
    if tag_duration and tag_duration > 0:
        return tag_duration, "tag"
    return 0.0, "unknown"


def _probe_streams(path: str, ffprobe_bin: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "ffprobe failed").strip()[-900:])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    videos = [
        item
        for item in streams
        if isinstance(item, dict)
        and item.get("codec_type") == "video"
        and not bool((item.get("disposition") or {}).get("attached_pic"))
    ]
    audios = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    subtitles = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "subtitle"
    ]
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    video_duration, video_duration_source = _stream_duration(video)
    audio_duration, audio_duration_source = _stream_duration(audio)
    format_info = payload.get("format") if isinstance(payload, dict) else {}
    format_info = format_info if isinstance(format_info, dict) else {}
    duration = _safe_float(format_info.get("duration")) or video_duration

    subtitle_streams = []
    for stream in subtitles:
        subtitle_duration, duration_source = _stream_duration(stream)
        subtitle_streams.append(
            {
                "codec": str(stream.get("codec_name") or ""),
                "language": str((stream.get("tags") or {}).get("language") or ""),
                "duration": round(subtitle_duration, 3),
                "duration_source": duration_source,
            }
        )

    return {
        "duration": round(duration, 3),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "video_duration": round(video_duration, 3),
        "audio_duration": round(audio_duration, 3),
        "video_duration_source": video_duration_source,
        "audio_duration_source": audio_duration_source,
        "subtitle_streams": subtitle_streams,
    }


def _sample_windows(duration: float) -> tuple[str, list[tuple[float, float]]]:
    if duration <= 0:
        return "sampled", [(0.0, 30.0)]
    if duration <= FULL_SCAN_MAX_SECONDS:
        return "full", [(0.0, duration)]

    sample_length = min(LONG_SCAN_SAMPLE_SECONDS, duration)
    available = max(0.0, duration - sample_length)
    starts = [
        round(available * index / (LONG_SCAN_SAMPLE_COUNT - 1), 3)
        for index in range(LONG_SCAN_SAMPLE_COUNT)
    ]
    windows = list(dict.fromkeys((start, sample_length) for start in starts))
    return "sampled", windows


def _run_signal_scan(
    path: str,
    ffmpeg_bin: str,
    *,
    has_audio: bool,
    duration: float,
) -> dict[str, Any]:
    mode, windows = _sample_windows(duration)
    requested_seconds = sum(length for _, length in windows)
    analyzed_seconds = 0.0
    black_seconds = 0.0
    silence_seconds = 0.0
    errors: list[str] = []
    timeouts = 0

    for start, length in windows:
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
        ]
        if start > 0:
            command.extend(["-ss", f"{start:.3f}"])
        command.extend(
            [
                "-i",
                path,
                "-t",
                f"{length:.3f}",
                "-map",
                "0:v:0",
                "-vf",
                "blackdetect=d=0.75:pic_th=0.98:pix_th=0.10",
            ]
        )
        if has_audio:
            command.extend(
                [
                    "-map",
                    "0:a:0",
                    "-af",
                    "silencedetect=n=-50dB:d=0.75",
                ]
            )
        else:
            command.append("-an")
        command.extend(["-f", "null", "-"])
        timeout = max(20, min(MAX_SCAN_TIMEOUT_SECONDS, int(length * 2 + 15)))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            timeouts += 1
            errors.append(f"sample@{start:.3f}s timed out after {timeout}s")
            continue
        except OSError as exc:
            errors.append(f"sample@{start:.3f}s failed: {type(exc).__name__}")
            continue
        if completed.returncode != 0:
            errors.append(
                f"sample@{start:.3f}s failed: "
                f"{(completed.stderr or 'ffmpeg failed').strip()[-240:]}"
            )
            continue

        stderr = completed.stderr or ""
        analyzed_seconds += length
        detected_black = sum(
            _safe_float(item) for item in _BLACK_DURATION_RE.findall(stderr)
        )
        detected_silence = sum(
            _safe_float(item) for item in _SILENCE_DURATION_RE.findall(stderr)
        )
        black_seconds += min(length, max(0.0, detected_black))
        silence_seconds += min(length, max(0.0, detected_silence))

    return {
        "mode": mode,
        "sample_count": len(windows),
        "requested_seconds": round(requested_seconds, 3),
        "analyzed_seconds": round(analyzed_seconds, 3),
        "black_seconds": round(black_seconds, 3),
        "silence_seconds": round(silence_seconds, 3),
        "black_ratio": round(black_seconds / analyzed_seconds, 4)
        if analyzed_seconds > 0
        else None,
        "silence_ratio": round(silence_seconds / analyzed_seconds, 4)
        if analyzed_seconds > 0 and has_audio
        else None,
        "complete": analyzed_seconds + 0.01 >= requested_seconds,
        "timeouts": timeouts,
        "errors": errors,
    }


def _parse_external_subtitles(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {"format": path.suffix.lower().lstrip("."), "cue_count": 0}

    cues: list[tuple[float, float]] = []
    suffix = path.suffix.lower()
    if suffix in {".srt", ".vtt"}:
        for match in _TIMED_SUBTITLE_RE.finditer(text):
            start = _timestamp_seconds(match.group("start"))
            end = _timestamp_seconds(match.group("end"))
            if start is not None and end is not None and end > start:
                cues.append((start, end))
    elif suffix in {".ass", ".ssa"}:
        for raw_line in text.splitlines():
            if not raw_line.lstrip().lower().startswith("dialogue:"):
                continue
            parts = raw_line.split(",", 3)
            if len(parts) < 3:
                continue
            start = _timestamp_seconds(parts[1])
            end = _timestamp_seconds(parts[2])
            if start is not None and end is not None and end > start:
                cues.append((start, end))
    else:
        return {
            "format": suffix.lstrip(".") or "unknown",
            "cue_count": None,
            "parse_supported": False,
        }

    return {
        "format": suffix.lstrip("."),
        "cue_count": len(cues),
        "first_start": round(min((item[0] for item in cues), default=0.0), 3),
        "last_end": round(max((item[1] for item in cues), default=0.0), 3),
        "parse_supported": True,
    }


def _subtitle_check(
    subtitle_path: str | None,
    media: Mapping[str, Any],
) -> dict[str, Any]:
    duration = _safe_float(media.get("duration"))
    embedded = media.get("subtitle_streams") or []
    if subtitle_path:
        path = Path(subtitle_path).expanduser()
        if not path.is_file():
            return _check(
                "subtitles",
                "unknown",
                "指定字幕文件不存在，无法确认成片字幕是否完整",
                source="external",
                path=str(path),
            )
        try:
            subtitle = _parse_external_subtitles(path)
        except OSError as exc:
            return _check(
                "subtitles",
                "unknown",
                "字幕文件无法读取，需人工复核",
                source="external",
                error=type(exc).__name__,
            )
        if subtitle.get("parse_supported") is False:
            return _check(
                "subtitles",
                "unknown",
                "字幕格式暂不支持自动核对，不能判定为合格",
                source="external",
                **subtitle,
            )
        if not subtitle.get("cue_count"):
            return _check(
                "subtitles",
                "blocked",
                "字幕文件为空或没有有效时间轴",
                source="external",
                **subtitle,
            )
        if duration <= 0:
            return _check(
                "subtitles",
                "unknown",
                "视频时长未知，无法核对字幕是否超时",
                source="external",
                **subtitle,
            )
        overrun = _safe_float(subtitle.get("last_end")) - duration
        hard_tolerance = max(1.0, duration * 0.01)
        if overrun > hard_tolerance:
            return _check(
                "subtitles",
                "blocked",
                "字幕结束时间超过视频时长",
                source="external",
                overrun_seconds=round(overrun, 3),
                **subtitle,
            )
        if overrun > 0.25:
            return _check(
                "subtitles",
                "review",
                "字幕末尾略超出视频，需要人工确认播放器表现",
                source="external",
                overrun_seconds=round(overrun, 3),
                **subtitle,
            )
        return _check(
            "subtitles",
            "pass",
            "外部字幕存在且时间轴未超过视频",
            source="external",
            **subtitle,
        )

    if embedded:
        known_durations = [
            _safe_float(item.get("duration"))
            for item in embedded
            if _safe_float(item.get("duration")) > 0
        ]
        if duration > 0 and known_durations:
            overrun = max(known_durations) - duration
            if overrun > max(1.0, duration * 0.01):
                return _check(
                    "subtitles",
                    "blocked",
                    "内嵌字幕轨结束时间超过视频时长",
                    source="embedded",
                    stream_count=len(embedded),
                    overrun_seconds=round(overrun, 3),
                )
            return _check(
                "subtitles",
                "pass",
                "已检测到内嵌字幕轨，时间范围未超过视频",
                source="embedded",
                stream_count=len(embedded),
            )
        return _check(
            "subtitles",
            "review",
            "已检测到内嵌字幕轨，但容器未提供可核对的字幕时长",
            source="embedded",
            stream_count=len(embedded),
        )

    return _check(
        "subtitles",
        "unknown",
        "未提供外部字幕且未检测到字幕轨；烧录字幕无法由流信息确认",
        source="unknown",
    )


def _overall_status(checks: list[dict[str, Any]]) -> str:
    states = {str(item.get("status") or "unknown") for item in checks}
    if "blocked" in states:
        return "blocked"
    if states.intersection({"review", "unknown"}):
        return "review"
    return "pass"


def inspect_final_media(
    path: str,
    subtitle_path: str | None = None,
    require_audio: bool = True,
) -> dict[str, Any]:
    """Inspect a final media file with bounded, local-only technical checks."""

    checked_at = _utc_now()
    media_path = Path(path).expanduser()
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "status": "blocked",
        "checks": checks,
        "checked_at": checked_at,
    }

    if not media_path.is_file() or media_path.stat().st_size <= 0:
        checks.append(
            _check("media_file", "blocked", "成片文件不存在或为空")
        )
        return result
    checks.append(
        _check(
            "media_file",
            "pass",
            "成片文件存在",
            size_bytes=media_path.stat().st_size,
        )
    )

    ffmpeg_bin, ffprobe_bin = _resolve_media_tools()
    if not ffprobe_bin:
        checks.append(
            _check(
                "video_stream",
                "unknown",
                "本机未找到 ffprobe，无法确认视频流",
            )
        )
        checks.append(
            _check(
                "audio_stream",
                "unknown",
                "本机未找到 ffprobe，无法确认音轨",
                required=bool(require_audio),
            )
        )
        checks.append(
            _check("av_duration", "unknown", "缺少流信息，无法核对音视频时长")
        )
        checks.append(_subtitle_check(subtitle_path, {}))
        result["status"] = _overall_status(checks)
        return result

    try:
        media = _probe_streams(os.fspath(media_path), ffprobe_bin)
    except FileNotFoundError:
        checks.extend(
            [
                _check("video_stream", "unknown", "ffprobe 不可执行，无法确认视频流"),
                _check(
                    "audio_stream",
                    "unknown",
                    "ffprobe 不可执行，无法确认音轨",
                    required=bool(require_audio),
                ),
                _check("av_duration", "unknown", "缺少流信息，无法核对音视频时长"),
            ]
        )
        checks.append(_subtitle_check(subtitle_path, {}))
        result["status"] = _overall_status(checks)
        return result
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        checks.append(
            _check(
                "video_stream",
                "blocked",
                "成片无法被 ffprobe 正常读取",
                error=type(exc).__name__,
            )
        )
        result["status"] = _overall_status(checks)
        return result

    result["media"] = media
    if media["has_video"]:
        checks.append(
            _check(
                "video_stream",
                "pass",
                "已检测到有效视频流",
                codec=media["video_codec"],
                duration_seconds=media["duration"],
            )
        )
    else:
        checks.append(_check("video_stream", "blocked", "成片缺少有效视频流"))

    if media["has_audio"]:
        checks.append(
            _check(
                "audio_stream",
                "pass",
                "已检测到音轨",
                codec=media["audio_codec"],
                required=bool(require_audio),
            )
        )
    elif require_audio:
        checks.append(
            _check(
                "audio_stream",
                "blocked",
                "成片缺少必需音轨",
                required=True,
            )
        )
    else:
        checks.append(
            _check(
                "audio_stream",
                "pass",
                "当前任务不要求音轨",
                required=False,
                present=False,
            )
        )

    if not media["has_video"] or not media["has_audio"]:
        checks.append(
            _check(
                "av_duration",
                "pass" if not require_audio and media["has_video"] else "unknown",
                "无需比较音视频时长"
                if not require_audio and media["has_video"]
                else "缺少视频流或音轨，无法比较音视频时长",
            )
        )
    else:
        video_duration = _safe_float(media.get("video_duration"))
        audio_duration = _safe_float(media.get("audio_duration"))
        if video_duration <= 0 or audio_duration <= 0:
            checks.append(
                _check(
                    "av_duration",
                    "unknown",
                    "容器未提供独立音视频流时长，不能判定同步合格",
                )
            )
        else:
            delta = abs(video_duration - audio_duration)
            review_tolerance = max(0.5, video_duration * 0.01)
            block_tolerance = max(2.0, video_duration * 0.05)
            details = {
                "video_seconds": round(video_duration, 3),
                "audio_seconds": round(audio_duration, 3),
                "delta_seconds": round(delta, 3),
            }
            if delta > block_tolerance:
                checks.append(
                    _check(
                        "av_duration",
                        "blocked",
                        "音视频流时长差异严重",
                        **details,
                    )
                )
            elif delta > review_tolerance:
                checks.append(
                    _check(
                        "av_duration",
                        "review",
                        "音视频流时长存在可见差异，需要试听确认",
                        **details,
                    )
                )
            else:
                checks.append(
                    _check(
                        "av_duration",
                        "pass",
                        "音视频流时长基本一致",
                        **details,
                    )
                )

    if not media["has_video"]:
        checks.append(_check("black_frames", "unknown", "缺少视频流，未执行黑屏检测"))
        checks.append(_check("silence", "unknown", "缺少视频流，未执行信号检测"))
    elif not ffmpeg_bin:
        checks.append(
            _check("black_frames", "unknown", "本机未找到 ffmpeg，未执行黑屏检测")
        )
        checks.append(
            _check("silence", "unknown", "本机未找到 ffmpeg，未执行静音检测")
        )
    else:
        scan = _run_signal_scan(
            os.fspath(media_path),
            ffmpeg_bin,
            has_audio=bool(media["has_audio"]),
            duration=_safe_float(media["duration"]),
        )
        result["scan"] = scan
        if not scan["complete"]:
            checks.append(
                _check(
                    "black_frames",
                    "unknown",
                    "黑屏检测超时或未完成，不能判定为合格",
                    **scan,
                )
            )
        else:
            black_ratio = _safe_float(scan.get("black_ratio"))
            if black_ratio >= 0.90:
                checks.append(
                    _check(
                        "black_frames",
                        "blocked",
                        "抽检画面几乎全部为黑屏",
                        ratio=black_ratio,
                        mode=scan["mode"],
                    )
                )
            elif black_ratio >= 0.50:
                checks.append(
                    _check(
                        "black_frames",
                        "review",
                        "抽检画面存在大面积黑屏，需要人工复核",
                        ratio=black_ratio,
                        mode=scan["mode"],
                    )
                )
            else:
                checks.append(
                    _check(
                        "black_frames",
                        "pass",
                        "未发现严重黑屏",
                        ratio=black_ratio,
                        mode=scan["mode"],
                    )
                )

        if not media["has_audio"]:
            checks.append(
                _check(
                    "silence",
                    "pass" if not require_audio else "unknown",
                    "当前任务不要求音轨，跳过静音检测"
                    if not require_audio
                    else "缺少音轨，无法执行静音检测",
                )
            )
        elif not scan["complete"]:
            checks.append(
                _check(
                    "silence",
                    "unknown",
                    "静音检测超时或未完成，不能判定为合格",
                    **scan,
                )
            )
        else:
            silence_ratio = _safe_float(scan.get("silence_ratio"))
            if silence_ratio >= 0.95 and require_audio:
                checks.append(
                    _check(
                        "silence",
                        "blocked",
                        "必需音轨几乎全部静音",
                        ratio=silence_ratio,
                        mode=scan["mode"],
                    )
                )
            elif silence_ratio >= 0.60:
                checks.append(
                    _check(
                        "silence",
                        "review",
                        "音轨长时间静音，需要试听确认",
                        ratio=silence_ratio,
                        mode=scan["mode"],
                    )
                )
            else:
                checks.append(
                    _check(
                        "silence",
                        "pass",
                        "未发现严重静音",
                        ratio=silence_ratio,
                        mode=scan["mode"],
                    )
                )

    checks.append(_subtitle_check(subtitle_path, media))
    result["status"] = _overall_status(checks)
    return result
