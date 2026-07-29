import pytest

from modules.content_recreation import (
    generate_recreation_plan,
    validate_review_payload,
)


def test_fallback_plan_demands_substantive_original_contribution():
    plan = generate_recreation_plan(
        {
            "title": "AI工具实测",
            "source_uploader": "来源作者",
            "source_url": "https://example.com/source",
            "recreation_completed": 0,
        },
        config={},
    )

    assert plan["generated_by"] == "safe_fallback"
    assert plan["risk_level"] == "high"
    assert "原创口播" in plan["original_contribution"]
    assert len(plan["commentary_outline"]) >= 3


def test_review_payload_requires_source_attribution():
    with pytest.raises(ValueError, match="来源标识"):
        validate_review_payload(
            {
                "source_attribution": "",
                "original_contribution": "加入原创口播、核验和案例分析形成新的叙事。",
                "watermark_status": "none",
                "recreation_confirmed": "on",
            }
        )


def test_review_payload_enforces_real_contribution_detail():
    with pytest.raises(ValueError, match="具体说明"):
        validate_review_payload(
            {
                "source_attribution": "原作者：https://example.com/source",
                "processing_mode": "professional",
                "original_contribution": "加字幕",
                "watermark_status": "none",
                "recreation_confirmed": "on",
            }
        )


def test_review_payload_requires_recreated_media_confirmation():
    with pytest.raises(ValueError, match="已完成加工"):
        validate_review_payload(
            {
                "source_attribution": "原作者：https://example.com/source",
                "processing_mode": "professional",
                "original_contribution": (
                    "加入多段原创旁白、剧情结构分析、人物动机复盘、重新编排后的独立评价和结尾观点。"
                ),
                "watermark_status": "platform_overlay_removed",
                "watermark_note": "已清理平台浮层，片尾保留原作者署名",
            }
        )


def test_direct_transfer_only_requires_source_watermark_and_final_confirmation():
    result = validate_review_payload(
        {
            "source_attribution": "原作者：https://example.com/source",
            "processing_mode": "direct",
            "watermark_status": "third_party_preserved",
            "watermark_note": "画面保留原作者账号",
            "publish_confirmed": "on",
        }
    )

    assert result["processing_mode"] == "direct"
    assert result["publish_confirmed"] is True
    assert result["original_contribution"] == ""


def test_drama_recap_plan_rejects_mechanical_episode_reposting():
    plan = generate_recreation_plan(
        {
            "title": "短剧第一集",
            "source_uploader": "来源作者",
            "source_url": "https://example.com/drama",
            "recreation_completed": 1,
        },
        config={},
        mode="drama_recap",
    )

    assert "原创旁白" in plan["original_contribution"]
    assert "机械拆条" in plan["original_contribution"]
