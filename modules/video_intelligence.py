#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Local intelligence helpers for content and cover release preflight."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


CONTENT_RULES = (
    ("国家级", "极限或权威性表述", "行业级"),
    ("世界级", "极限或权威性表述", "高水平"),
    ("最高级", "极限表述", "较高等级"),
    ("全网第一", "排名缺少可核验证据", "表现突出"),
    ("全国第一", "排名缺少可核验证据", "处于前列"),
    ("唯一", "绝对化表述", "少见的"),
    ("绝对", "绝对化表述", "通常"),
    ("百分之百", "效果保证", "在适用条件下"),
    ("100%", "效果保证", "在适用条件下"),
    ("永久", "期限保证", "长期"),
    ("包治", "医疗效果保证", "可能有助于改善"),
    ("根治", "医疗效果保证", "请咨询专业人士"),
    ("药到病除", "医疗效果保证", "请遵医嘱使用"),
    ("稳赚", "收益保证", "收益存在波动"),
    ("保本", "收益保证", "本金可能发生损失"),
    ("零风险", "风险保证", "请充分了解相关风险"),
    ("无副作用", "医疗安全保证", "副作用因人而异"),
    ("立刻见效", "即时效果保证", "效果因人而异"),
)

SENSITIVE_CATEGORIES = {
    "医疗健康": ("治疗", "疗效", "疾病", "减肥", "降血压", "抗癌", "保健品"),
    "金融投资": ("股票", "基金", "理财", "收益率", "投资回报", "币圈"),
    "未成年人": ("儿童", "婴幼儿", "学生", "未成年人"),
}


def run_content_preflight(
    fields: dict[str, Any],
    targets: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    """Return a deterministic local wording check without external services."""
    normalized = {
        str(name): str(value or "").strip()
        for name, value in fields.items()
        if str(value or "").strip()
    }
    issues: list[dict[str, str]] = []
    for field, text in normalized.items():
        for term, reason, replacement in CONTENT_RULES:
            if term.lower() in text.lower():
                issues.append(
                    {
                        "field": field,
                        "term": term,
                        "reason": reason,
                        "replacement": replacement,
                        "severity": "review",
                    }
                )

    combined = "\n".join(normalized.values())
    category_notes = []
    for category, terms in SENSITIVE_CATEGORIES.items():
        matched = [term for term in terms if term in combined]
        if matched:
            category_notes.append(
                {
                    "category": category,
                    "matched": matched[:6],
                    "note": "属于需要补充依据并人工复核的敏感领域",
                }
            )

    target_set = {str(item).strip().lower() for item in targets if str(item).strip()}
    required_fields = []
    if "youtube" in target_set and not normalized.get("youtube_title"):
        required_fields.append("YouTube 标题")
    if "bilibili" in target_set and not normalized.get("bilibili_title"):
        required_fields.append("B站标题")
    if "douyin" in target_set and not normalized.get("douyin_text"):
        required_fields.append("抖音文案")
    if "tiktok" in target_set and not normalized.get("tiktok_text"):
        required_fields.append("TikTok 文案")
    if "x" in target_set and not normalized.get("x_text"):
        required_fields.append("X 发布文字")

    hashtags = re.findall(r"(?<!\w)#[^\s#]{1,50}", combined)
    notes = []
    if len(hashtags) > 10:
        notes.append("话题标签超过 10 个，建议保留最相关的 3–8 个")
    if required_fields:
        notes.append("缺少：" + "、".join(required_fields))

    verdict = "pass"
    if required_fields:
        verdict = "block"
    elif issues or category_notes or notes:
        verdict = "review"
    score = max(0, 100 - len(issues) * 8 - len(category_notes) * 10 - len(notes) * 6)
    return {
        "version": 1,
        "verdict": verdict,
        "score": score,
        "issues": issues,
        "category_notes": category_notes,
        "notes": notes,
        "checked_fields": sorted(normalized),
        "disclaimer": "本地规则仅作发布前辅助筛查，不能替代平台审核或专业法律意见。",
    }


def find_local_cover(media_path: str) -> str:
    directory = Path(str(media_path or "")).parent
    if not directory.is_dir():
        return ""
    preferred = (
        "custom_cover.jpg",
        "custom_cover.png",
        "custom_cover.webp",
        "video.jpg",
        "video.png",
        "video.webp",
        "thumbnail.jpg",
        "thumbnail.png",
        "thumbnail.webp",
    )
    for filename in preferred:
        candidate = directory / filename
        if candidate.is_file():
            return str(candidate)
    return ""


def run_cover_preflight(
    cover_path: str,
    media_info: dict[str, Any],
    targets: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    """Inspect local cover dimensions and platform crop risk without uploads."""
    target_set = {str(item).strip().lower() for item in targets if str(item).strip()}
    path = str(cover_path or "").strip()
    width = 0
    height = 0
    size_bytes = 0
    source = "dedicated_cover"
    issues: list[dict[str, str]] = []
    if path and os.path.isfile(path):
        try:
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            size_bytes = os.path.getsize(path)
        except (OSError, UnidentifiedImageError):
            issues.append(
                {"severity": "block", "message": "封面文件无法识别或已经损坏"}
            )
    else:
        source = "video_frame"
        width = int(float(media_info.get("width") or 0))
        height = int(float(media_info.get("height") or 0))
        issues.append(
            {
                "severity": "review",
                "message": "没有找到独立封面，将以视频画面作为封面来源",
            }
        )

    ratio = round(width / height, 4) if width and height else 0.0
    if width < 720 or height < 720:
        issues.append(
            {"severity": "review", "message": "封面短边不足 720px，移动端可能不够清晰"}
        )
    if size_bytes > 10 * 1024 * 1024:
        issues.append(
            {"severity": "review", "message": "封面超过 10MB，部分平台上传可能失败"}
        )

    platform_checks: dict[str, dict[str, Any]] = {}
    for platform in sorted(target_set):
        vertical = platform in {"douyin", "tiktok"}
        preferred_ratio = 9 / 16 if vertical else 16 / 9
        difference = abs(ratio - preferred_ratio) if ratio else 99
        crop_risk = difference > (0.12 if vertical else 0.18)
        label = {
            "douyin": "抖音",
            "tiktok": "TikTok",
            "youtube": "YouTube",
            "bilibili": "B站",
            "x": "X",
        }.get(platform, platform)
        platform_checks[platform] = {
            "label": label,
            "preferred_ratio": "9:16" if vertical else "16:9",
            "crop_risk": crop_risk,
            "safe_zone_note": (
                "主体和标题应避开顶部约 10%、底部约 20% 及右侧交互区"
                if vertical
                else "主体和标题应保留四周约 8% 的裁切安全区"
            ),
        }
        if crop_risk:
            issues.append(
                {
                    "severity": "review",
                    "message": f"{label} 推荐 {platform_checks[platform]['preferred_ratio']}，当前比例存在裁切风险",
                }
            )

    verdict = "pass"
    if any(item["severity"] == "block" for item in issues):
        verdict = "block"
    elif issues:
        verdict = "review"
    return {
        "version": 1,
        "verdict": verdict,
        "source": source,
        "path": path if source == "dedicated_cover" else "",
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "ratio": ratio,
        "issues": issues,
        "platforms": platform_checks,
        "disclaimer": "安全区提示基于平台常见布局，发布前仍需在真实预览中确认。",
    }
