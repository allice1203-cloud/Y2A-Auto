#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Safety-first content recreation planning for cross-platform publishing."""

from __future__ import annotations

import json
import logging
import re
from typing import Any


logger = logging.getLogger("content_recreation")

RECREATION_MODES = {
    "commentary": "原创观点解说",
    "localized": "本地化深度改编",
    "drama_recap": "短剧解说 / 剧情复盘",
    "structured_remix": "结构化重剪与新叙事",
}

PROCESSING_MODES = {
    "direct": "直接转发",
    "quick": "简单加工",
    "professional": "专业加工",
}

WATERMARK_STATES = {
    "none": "未发现需要处理的来源或平台标识",
    "own_brand": "仅有本人或本团队品牌标识",
    "third_party_preserved": "已保留作者或来源标识",
    "platform_overlay_removed": "仅清理平台浮层，来源署名仍保留",
}

WATERMARK_REVIEWED_VALUES = set(WATERMARK_STATES)


def _clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _clean_multiline(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def _clean_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean_text(item, 300) for item in value if _clean_text(item, 300)][:limit]


def _fallback_plan(job: dict[str, Any], mode: str) -> dict[str, Any]:
    title = _clean_text(job.get("title") or "这个视频主题", 180)
    uploader = _clean_text(job.get("source_uploader") or "原作者", 120)
    source_url = _clean_text(job.get("source_url"), 1000)
    if mode == "localized":
        angle = f"围绕《{title}》重组叙事顺序，补充中文背景、关键概念解释与本地案例。"
    elif mode == "drama_recap":
        angle = f"围绕《{title}》提炼剧情冲突，以原创旁白复盘人物动机、叙事逻辑和关键转折。"
    elif mode == "structured_remix":
        angle = f"围绕《{title}》重新组织镜头与论证顺序，保留来源署名并形成新的叙事结论。"
    else:
        angle = f"以《{title}》为素材线索，加入本频道的判断、验证过程和可执行结论。"
    contribution = (
        "重新设计开场问题，加入至少三段原创口播观点、事实核验或案例分析，"
        "调整片段顺序并在结尾给出独立结论；不得只增加字幕、边框、片头或水印。"
    )
    if mode == "drama_recap":
        contribution = (
            "以原创旁白承担主要叙事，只选取解释剧情所必需的短片段，重写结构、"
            "补充人物动机和独立评价；不得整集复刻、机械拆条或仅去除水印。"
        )
    return {
        "original_angle": angle,
        "original_contribution": contribution,
        "commentary_outline": [
            "用原创问题或结论开场，说明为什么值得讨论",
            "选取必要片段并逐段加入分析、验证或反驳",
            "结合本地案例给出独立判断和行动建议",
        ],
        "required_edits": [
            "新增原创口播或出镜评论",
            "只保留支撑观点所需的素材片段",
            "重新设计叙事结构、字幕和画面节奏",
            "在简介中准确标注素材来源和授权基础",
        ],
        "x_text": _clean_text(f"{title}｜我的三个观察：信息、判断和实际启发。", 260),
        "youtube_title": _clean_text(f"{title}：加入验证与独立观点后的深度解读", 100),
        "youtube_description": _clean_multiline(
            (
                f"本期围绕《{title}》进行重新策划和原创解读。\n\n"
                f"素材来源：{uploader}\n{source_url}\n\n"
                "发布前将补充原创口播、事实核验、案例分析和独立结论。"
            ),
            5000,
        ),
        "risk_level": "medium" if job.get("recreation_completed") else "high",
        "risk_notes": [
            "AI草案不等于已完成再创作，必须人工确认实际成片具有实质性原创贡献",
            "拥有转载许可也不自动满足YouTube重复使用内容的商业化要求",
            "作者名或来源标识不得为掩盖来源而移除",
        ],
        "watermark_policy": (
            "作者与来源标识默认保留；可以清理不承载作者归属信息的平台浮层，"
            "但必须在成片或发布文案中保留清晰来源署名。"
        ),
        "generated_by": "safe_fallback",
    }


def _normalize_plan(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    plan = {
        "original_angle": _clean_multiline(source.get("original_angle") or fallback["original_angle"], 1200),
        "original_contribution": _clean_multiline(
            source.get("original_contribution") or fallback["original_contribution"],
            3000,
        ),
        "commentary_outline": _clean_list(source.get("commentary_outline")) or fallback["commentary_outline"],
        "required_edits": _clean_list(source.get("required_edits")) or fallback["required_edits"],
        "x_text": _clean_multiline(source.get("x_text") or fallback["x_text"], 260),
        "youtube_title": _clean_text(source.get("youtube_title") or fallback["youtube_title"], 100),
        "youtube_description": _clean_multiline(
            source.get("youtube_description") or fallback["youtube_description"],
            5000,
        ),
        "risk_level": str(source.get("risk_level") or fallback["risk_level"]).lower(),
        "risk_notes": _clean_list(source.get("risk_notes")) or fallback["risk_notes"],
        "watermark_policy": _clean_multiline(
            source.get("watermark_policy") or fallback["watermark_policy"],
            1200,
        ),
        "generated_by": str(source.get("generated_by") or fallback["generated_by"]),
    }
    if plan["risk_level"] not in {"low", "medium", "high"}:
        plan["risk_level"] = fallback["risk_level"]
    return plan


def generate_recreation_plan(
    job: dict[str, Any],
    config: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    normalized_mode = str(mode or job.get("recreation_mode") or "commentary").strip().lower()
    if normalized_mode not in RECREATION_MODES:
        normalized_mode = "commentary"
    fallback = _fallback_plan(job, normalized_mode)
    app_config = dict(config or {})
    if not str(app_config.get("OPENAI_API_KEY") or "").strip():
        return fallback

    try:
        from .ai_enhancer import _request_json_object, get_openai_client

        client = get_openai_client(app_config)
        payload = {
            "source": {
                "platform": job.get("source_platform"),
                "title": _clean_text(job.get("title"), 500),
                "description": _clean_multiline(job.get("description"), 5000),
                "uploader": _clean_text(job.get("source_uploader"), 300),
                "duration_seconds": job.get("duration"),
            },
            "source_attribution": _clean_multiline(job.get("source_attribution"), 1500),
            "recreation_completed": bool(job.get("recreation_completed")),
            "recreation_mode": normalized_mode,
            "requirements": {
                "x_text_max_chars": 260,
                "youtube_title_max_chars": 100,
                "youtube_description_max_chars": 5000,
            },
        }
        system_prompt = (
            "你是跨平台视频再创作总编和版权风险审校员。请输出JSON对象，字段必须包括："
            "original_angle、original_contribution、commentary_outline、required_edits、"
            "x_text、youtube_title、youtube_description、risk_level、risk_notes。"
            "目标是形成具有实质性原创贡献的制作方案，而不是换标题、加字幕、加边框或去水印。"
            "不得替用户判断合理使用成立、不得建议规避平台审核。"
            "作者名和来源标识默认保留，不得建议通过去除标识掩盖来源。"
            "必须要求加入原创口播/出镜评论、事实核验、案例分析或新的叙事结构。"
            "当授权依据不明确时 risk_level 必须为 high。"
        )
        parsed = _request_json_object(
            client,
            str(app_config.get("OPENAI_MODEL_NAME") or "gpt-3.5-turbo"),
            system_prompt,
            payload,
            max_tokens=1800,
            temperature=0.3,
            thinking_enabled=bool(app_config.get("OPENAI_THINKING_ENABLED", False)),
            logger_obj=logger,
            scene_name="transfer_content_recreation",
        )
        if isinstance(parsed, dict):
            parsed["generated_by"] = "ai"
            return _normalize_plan(parsed, fallback)
    except Exception as exc:
        logger.warning("生成再创作方案失败，使用安全模板: %s", exc)
    return fallback


def validate_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source_attribution = _clean_multiline(payload.get("source_attribution"), 2000)
    if len(source_attribution) < 2:
        raise ValueError("请保留原作者、原账号或原视频链接等来源标识")
    recreation_mode = str(payload.get("recreation_mode") or "commentary").strip().lower()
    if recreation_mode not in RECREATION_MODES:
        recreation_mode = "commentary"
    processing_mode = str(payload.get("processing_mode") or "direct").strip().lower()
    if processing_mode not in PROCESSING_MODES:
        processing_mode = "direct"
    original_contribution = _clean_multiline(payload.get("original_contribution"), 3000)
    if processing_mode == "quick" and len(original_contribution) < 4:
        raise ValueError("请简单说明本次加工内容，例如画幅、片头片尾、字幕或品牌包装")
    if processing_mode == "professional" and len(original_contribution) < 30:
        raise ValueError("请具体说明成片增加了哪些原创观点、口播、核验或叙事改造")
    watermark_status = str(payload.get("watermark_status") or "").strip().lower()
    if watermark_status not in WATERMARK_REVIEWED_VALUES:
        raise ValueError("必须核对成片中的作者名、来源标识和平台浮层")
    watermark_note = _clean_multiline(payload.get("watermark_note"), 1500)
    if watermark_status != "none" and len(watermark_note) < 4:
        raise ValueError("请说明来源标识保留位置或平台浮层处理结果")
    confirmation_field = (
        "publish_confirmed" if processing_mode == "direct" else "recreation_confirmed"
    )
    confirmed = str(payload.get(confirmation_field) or "").strip().lower()
    if confirmed not in {"1", "true", "yes", "on"}:
        if processing_mode == "direct":
            raise ValueError("请确认当前预览原片、来源标识和发布平台均无误")
        raise ValueError("请确认当前预览的是已完成加工的新成片")
    return {
        "source_attribution": source_attribution,
        "processing_mode": processing_mode,
        "recreation_mode": recreation_mode,
        "original_angle": _clean_multiline(payload.get("original_angle"), 1200),
        "original_contribution": original_contribution,
        "watermark_status": watermark_status,
        "watermark_note": watermark_note,
        "publish_confirmed": processing_mode == "direct",
        "recreation_confirmed": processing_mode != "direct",
        "x_text": _clean_multiline(payload.get("x_text"), 260),
        "youtube_title": _clean_text(payload.get("youtube_title"), 100),
        "youtube_description": _clean_multiline(payload.get("youtube_description"), 5000),
        "bilibili_title": _clean_text(payload.get("bilibili_title"), 80),
        "bilibili_description": _clean_multiline(
            payload.get("bilibili_description"), 2000
        ),
        "bilibili_partition_id": _clean_text(
            payload.get("bilibili_partition_id"), 20
        ),
        "douyin_text": _clean_multiline(payload.get("douyin_text"), 2000),
    }


def serialize_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False)


def deserialize_plan(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
