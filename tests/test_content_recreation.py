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
            "rights_basis": "unconfirmed",
        },
        config={},
    )

    assert plan["generated_by"] == "safe_fallback"
    assert plan["risk_level"] == "high"
    assert "原创口播" in plan["original_contribution"]
    assert len(plan["commentary_outline"]) >= 3


def test_review_payload_rejects_unconfirmed_rights():
    with pytest.raises(ValueError, match="版权"):
        validate_review_payload(
            {
                "rights_basis": "unconfirmed",
                "rights_note": "不确定",
                "original_contribution": "加入原创口播、核验和案例分析形成新的叙事。",
            }
        )


def test_review_payload_enforces_real_contribution_detail():
    with pytest.raises(ValueError, match="具体说明"):
        validate_review_payload(
            {
                "rights_basis": "owned",
                "rights_note": "本人原创并拥有全部权利",
                "original_contribution": "加字幕",
            }
        )


def test_review_payload_rejects_watermark_cleanup_without_owned_or_licensed_rights():
    with pytest.raises(ValueError, match="本人原创或书面授权"):
        validate_review_payload(
            {
                "rights_basis": "cc",
                "rights_note": "许可协议允许再创作和商业发布",
                "original_contribution": (
                    "加入多段原创旁白、剧情结构分析、人物动机复盘、重新编排后的独立评价和结尾观点。"
                ),
                "watermark_status": "authorized_cleanup",
                "watermark_note": "计划去除来源作者标识",
            }
        )


def test_drama_recap_plan_rejects_mechanical_episode_reposting():
    plan = generate_recreation_plan(
        {
            "title": "短剧第一集",
            "source_uploader": "来源作者",
            "source_url": "https://example.com/drama",
            "rights_basis": "licensed",
        },
        config={},
        mode="drama_recap",
    )

    assert "原创旁白" in plan["original_contribution"]
    assert "机械拆条" in plan["original_contribution"]
