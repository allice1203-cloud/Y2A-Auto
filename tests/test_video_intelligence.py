from PIL import Image

from modules.video_intelligence import run_content_preflight, run_cover_preflight


def test_content_preflight_passes_ordinary_platform_copy():
    result = run_content_preflight(
        {"douyin_text": "三个实用的视频剪辑技巧 #剪辑"},
        ["douyin"],
    )

    assert result["verdict"] == "pass"
    assert result["score"] == 100
    assert result["issues"] == []


def test_content_preflight_flags_claims_and_sensitive_categories():
    result = run_content_preflight(
        {"youtube_title": "股票稳赚、零风险的唯一方法"},
        ["youtube"],
    )

    assert result["verdict"] == "review"
    assert {item["term"] for item in result["issues"]} >= {"稳赚", "零风险", "唯一"}
    assert any(item["category"] == "金融投资" for item in result["category_notes"])


def test_content_preflight_blocks_missing_required_target_copy():
    result = run_content_preflight({}, ["bilibili", "tiktok"])

    assert result["verdict"] == "block"
    assert "B站标题" in result["notes"][0]
    assert "TikTok 文案" in result["notes"][0]


def test_cover_preflight_passes_matching_vertical_cover(tmp_path):
    cover = tmp_path / "custom_cover.jpg"
    Image.new("RGB", (1080, 1920), "white").save(cover)

    result = run_cover_preflight(str(cover), {}, ["douyin", "tiktok"])

    assert result["verdict"] == "pass"
    assert result["source"] == "dedicated_cover"
    assert result["platforms"]["douyin"]["crop_risk"] is False


def test_cover_preflight_warns_for_horizontal_frame_on_vertical_platform():
    result = run_cover_preflight(
        "",
        {"width": 1920, "height": 1080},
        ["douyin"],
    )

    assert result["verdict"] == "review"
    assert result["source"] == "video_frame"
    assert result["platforms"]["douyin"]["crop_risk"] is True


def test_cover_preflight_blocks_corrupt_cover(tmp_path):
    cover = tmp_path / "custom_cover.jpg"
    cover.write_bytes(b"not-an-image")

    result = run_cover_preflight(str(cover), {}, ["youtube"])

    assert result["verdict"] == "block"
    assert any(item["severity"] == "block" for item in result["issues"])
