import pytest

from modules.content_recreation import (
    build_growth_followup_draft,
    build_rights_risk,
    format_material_checklist_text,
    format_storyboard_text,
    generate_recreation_plan,
    merge_editable_draft,
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
    assert len(plan["hook_options"]) >= 3
    assert len(plan["segment_plan"]) >= 4
    assert all(segment["duration"] <= 15 for segment in plan["segment_plan"])
    assert all("source_start" in segment for segment in plan["segment_plan"])
    assert sum(segment["action"] == "replace" for segment in plan["segment_plan"]) >= 2
    assert set(plan["platform_versions"]) == {"bilibili", "douyin", "youtube"}
    assert plan["bilibili_title"]
    assert plan["douyin_text"]


def test_growth_followup_draft_is_editable_and_requires_no_model_call():
    plan = build_growth_followup_draft(
        {
            "title": "AI 剪辑工具实测｜同类续作实测",
            "source_url": "https://www.bilibili.com/video/BV1draft",
        },
        {
            "candidate_type": "growth_followup",
            "parent_job_id": "parent-1",
            "recommended_target_platform": "youtube",
            "views_24h": 300,
            "engagement_24h": 12,
            "growth_24h_72h": None,
            "suggested_angle": "换一个场景做实测对比",
            "suggested_hook": "前 3 秒先给最大反差",
            "suggested_duration": "45-60 秒",
            "suggested_visual_structure": "结果 → 证据 → 结论",
        },
    )

    assert plan["generated_by"] == "growth_followup_local_draft"
    assert plan["draft_stage"] == "concept"
    assert len(plan["commentary_script"]) >= 100
    assert len(plan["draft_storyboard"]) == 5
    assert len(plan["material_checklist"]) == 5
    assert "换一个场景" in plan["original_angle"]
    assert "前 3 秒" in format_storyboard_text(plan)
    assert "- [ ]" in format_material_checklist_text(plan)

    edited = merge_editable_draft(
        plan,
        "开场｜新旁白｜新画面｜新实拍\n结尾｜新结论｜结论卡｜信息卡",
        "- [ ] 新实拍\n- [x] 信息卡",
    )

    assert [item["stage"] for item in edited["draft_storyboard"]] == [
        "开场",
        "结尾",
    ]
    assert edited["material_checklist"] == ["新实拍", "信息卡"]
    assert edited["broll_suggestions"] == ["新实拍", "信息卡"]


def test_fallback_plan_uses_real_subtitle_timecodes():
    plan = generate_recreation_plan(
        {
            "title": "字幕时间线测试",
            "duration": 90,
            "transcript_source": "video.zh.srt",
            "source_transcript": [
                {"start": 5.0, "end": 8.5, "text": "第一个关键观点"},
                {"start": 9.0, "end": 13.0, "text": "对关键观点进行解释"},
                {"start": 30.0, "end": 34.0, "text": "第二个案例"},
                {"start": 60.0, "end": 66.0, "text": "最后的结论"},
            ],
        },
        config={},
    )

    segments = plan["segment_plan"]
    assert plan["transcript_source"] == "video.zh.srt"
    assert plan["transcript_cue_count"] == 4
    assert segments[0]["source_start"] == 5.0
    assert segments[-1]["source_end"] == 66.0
    assert all(1 <= segment["duration"] <= 15 for segment in segments)
    assert all(segment["action"] in {"keep", "trim", "replace", "exclude"} for segment in segments)


def test_performance_strategy_changes_local_visual_ratio_for_next_plan():
    plan = generate_recreation_plan(
        {
            "title": "数据反馈二剪",
            "duration": 240,
            "performance_strategy": {
                "sample_size": 6,
                "target_local_visual_ratio": 15,
                "hook_guidance": "前 3 秒直接给结果",
            },
        },
        config={},
    )

    assert plan["performance_strategy"]["sample_size"] == 6
    assert len(plan["segment_plan"]) == 8
    assert sum(item["action"] == "replace" for item in plan["segment_plan"]) == 1


def test_unconfirmed_rights_are_a_non_blocking_risk_hint():
    risk = build_rights_risk(
        {"rights_basis": "unconfirmed", "rights_note": "热点观察账号"}
    )

    assert risk["level"] == "red"
    assert risk["blocking"] is False
    assert risk["note"] == "热点观察账号"


def test_review_defaults_to_standard_remix_mode():
    result = validate_review_payload(
        {
            "source_attribution": "原作者：https://example.com/source",
            "original_contribution": "加入中文旁白、重排镜头、补充 B-roll 和独立结论，形成标准二剪成片。",
            "watermark_status": "none",
            "recreation_confirmed": "on",
        }
    )

    assert result["processing_mode"] == "professional"


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


def test_incomplete_concept_can_be_saved_without_publish_confirmation():
    result = validate_review_payload(
        {
            "source_attribution": "参考来源：https://example.com/source",
            "processing_mode": "professional",
            "original_contribution": "待补充",
            "commentary_script": "口播草稿",
        },
        require_confirmation=False,
    )

    assert result["watermark_status"] == "unreviewed"
    assert result["recreation_confirmed"] is False


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
