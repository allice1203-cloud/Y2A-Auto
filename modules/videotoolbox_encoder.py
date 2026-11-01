#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Apple VideoToolbox encoder selection with an explicit CPU fallback."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional


SUPPORTED_CODECS = frozenset({"h264", "hevc"})
DEFAULT_BITRATES_KBPS = {"h264": 8_000, "hevc": 6_000}
_CPU_ENCODERS = {"h264": "libx264", "hevc": "libx265"}
_HARDWARE_ENCODERS = {
    "h264": "h264_videotoolbox",
    "hevc": "hevc_videotoolbox",
}
_CPU_PRESETS = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)


@dataclass(frozen=True)
class VideoToolboxProbe:
    supported: bool
    encoder_name: str
    reason: str
    returncode: Optional[int] = None


@dataclass(frozen=True)
class EncoderPlan:
    codec: str
    encoder_name: str
    ffmpeg_args: tuple[str, ...]
    hardware_accelerated: bool
    reason: str


def _normalize_codec(codec: str) -> str:
    normalized = str(codec or "").strip().lower()
    if normalized in {"h265", "x265"}:
        normalized = "hevc"
    if normalized not in SUPPORTED_CODECS:
        raise ValueError(f"Unsupported video codec: {codec}")
    return normalized


def _resolve_ffmpeg_path(ffmpeg_path: Optional[str]) -> Optional[str]:
    if ffmpeg_path:
        return str(ffmpeg_path)
    try:
        from .ffmpeg_manager import get_ffmpeg_path

        return get_ffmpeg_path()
    except Exception:
        return None


def probe_videotoolbox(
    ffmpeg_path: Optional[str] = None,
    codec: str = "h264",
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_seconds: float = 10.0,
) -> VideoToolboxProbe:
    """Perform a one-frame hardware-only encode instead of trusting a name list."""

    normalized_codec = _normalize_codec(codec)
    encoder_name = _HARDWARE_ENCODERS[normalized_codec]
    if platform.system() != "Darwin":
        return VideoToolboxProbe(
            supported=False,
            encoder_name=encoder_name,
            reason="videotoolbox_requires_macos",
        )

    executable = _resolve_ffmpeg_path(ffmpeg_path)
    if not executable:
        return VideoToolboxProbe(
            supported=False,
            encoder_name=encoder_name,
            reason="ffmpeg_unavailable",
        )

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:r=1",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        encoder_name,
        "-allow_sw",
        "0",
        "-f",
        "null",
        "-",
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return VideoToolboxProbe(False, encoder_name, "ffmpeg_unavailable")
    except subprocess.TimeoutExpired:
        return VideoToolboxProbe(False, encoder_name, "probe_timeout")
    except OSError:
        return VideoToolboxProbe(False, encoder_name, "probe_failed")

    if result.returncode == 0:
        return VideoToolboxProbe(True, encoder_name, "hardware_encode_passed", 0)
    return VideoToolboxProbe(
        False,
        encoder_name,
        "hardware_encode_failed",
        int(result.returncode),
    )


def videotoolbox_supported(
    ffmpeg_path: Optional[str] = None,
    codec: str = "h264",
    **probe_kwargs,
) -> bool:
    return probe_videotoolbox(ffmpeg_path, codec, **probe_kwargs).supported


def _bitrate_args(bitrate_kbps: int) -> tuple[str, ...]:
    if bitrate_kbps <= 0:
        raise ValueError("Video bitrate must be greater than zero.")
    maxrate = max(bitrate_kbps, int(round(bitrate_kbps * 1.5)))
    bufsize = max(bitrate_kbps, int(round(bitrate_kbps * 2.0)))
    return (
        "-b:v",
        f"{bitrate_kbps}k",
        "-maxrate",
        f"{maxrate}k",
        "-bufsize",
        f"{bufsize}k",
    )


def build_cpu_encoder_plan(
    *,
    codec: str = "h264",
    bitrate_kbps: Optional[int] = None,
    cpu_preset: str = "veryfast",
) -> EncoderPlan:
    normalized_codec = _normalize_codec(codec)
    normalized_preset = str(cpu_preset or "").strip().lower()
    if normalized_preset not in _CPU_PRESETS:
        raise ValueError(f"Unsupported CPU encoder preset: {cpu_preset}")
    encoder_name = _CPU_ENCODERS[normalized_codec]
    args: list[str] = [
        "-c:v",
        encoder_name,
        "-preset",
        normalized_preset,
        "-pix_fmt",
        "yuv420p",
    ]
    if bitrate_kbps is None:
        args.extend(("-crf", "21" if normalized_codec == "h264" else "24"))
    else:
        args.extend(_bitrate_args(int(bitrate_kbps)))
    if normalized_codec == "hevc":
        args.extend(("-tag:v", "hvc1"))
    return EncoderPlan(
        codec=normalized_codec,
        encoder_name=encoder_name,
        ffmpeg_args=tuple(args),
        hardware_accelerated=False,
        reason="cpu_fallback",
    )


def build_video_encoder_plan(
    ffmpeg_path: Optional[str] = None,
    *,
    codec: str = "h264",
    bitrate_kbps: Optional[int] = None,
    prefer_hardware: bool = True,
    cpu_preset: str = "veryfast",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> EncoderPlan:
    """Choose VideoToolbox when a real probe succeeds, otherwise use CPU."""

    normalized_codec = _normalize_codec(codec)
    selected_bitrate = int(
        DEFAULT_BITRATES_KBPS[normalized_codec]
        if bitrate_kbps is None
        else bitrate_kbps
    )
    if selected_bitrate <= 0:
        raise ValueError("Video bitrate must be greater than zero.")

    if prefer_hardware:
        probe = probe_videotoolbox(
            ffmpeg_path,
            normalized_codec,
            runner=runner,
        )
        if probe.supported:
            args: list[str] = [
                "-c:v",
                probe.encoder_name,
                "-pix_fmt",
                "yuv420p",
                "-allow_sw",
                "0",
                "-realtime",
                "1",
                "-prio_speed",
                "1",
            ]
            args.extend(_bitrate_args(selected_bitrate))
            if normalized_codec == "h264":
                args.extend(("-profile:v", "high"))
            else:
                args.extend(("-tag:v", "hvc1"))
            return EncoderPlan(
                codec=normalized_codec,
                encoder_name=probe.encoder_name,
                ffmpeg_args=tuple(args),
                hardware_accelerated=True,
                reason=probe.reason,
            )

    fallback = build_cpu_encoder_plan(
        codec=normalized_codec,
        bitrate_kbps=bitrate_kbps,
        cpu_preset=cpu_preset,
    )
    if prefer_hardware:
        return EncoderPlan(
            codec=fallback.codec,
            encoder_name=fallback.encoder_name,
            ffmpeg_args=fallback.ffmpeg_args,
            hardware_accelerated=False,
            reason="videotoolbox_unavailable_cpu_fallback",
        )
    return fallback
