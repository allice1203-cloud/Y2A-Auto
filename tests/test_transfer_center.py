import json

import pytest

import modules.transfer_center as transfer_module


@pytest.fixture()
def center(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transfer_module,
        "get_app_subdir",
        lambda name: str(tmp_path / name) if name else str(tmp_path),
    )
    return transfer_module.TransferCenter(config_provider=lambda: {})


def test_rule_supports_keyword_discovery_without_fixed_account(center):
    rule_id = center.save_rule(
        {
            "name": "AI热门视频",
            "platform": "bilibili",
            "discovery_mode": "keyword",
            "source_value": "AI工具",
            "target_platforms": ["x", "youtube"],
            "interval_minutes": 15,
            "max_items": 10,
            "auto_prepare": True,
            "auto_publish": False,
            "enabled": True,
        }
    )

    rule = center.get_rule(rule_id)
    assert rule["discovery_mode"] == "keyword"
    assert json.loads(rule["target_platforms"]) == ["x", "youtube"]
    assert rule["auto_prepare"] == 1
    assert rule["auto_publish"] == 0
    assert rule["max_age_hours"] == 48
    assert rule["daily_limit"] == 3
    assert rule["require_review"] == 1


def test_manual_job_detects_douyin_and_deduplicates(center):
    url = "https://www.douyin.com/video/1234567890123456789"
    job_id = center.add_manual_job(url, ["youtube"])

    job = center.get_job(job_id)
    assert job["source_platform"] == "douyin"
    assert job["status"] == "discovered"

    with pytest.raises(ValueError, match="已经"):
        center.add_manual_job(url, ["youtube"])


def test_manual_job_normalizes_missing_protocol(center):
    job_id = center.add_manual_job(
        "bilibili.com/video/BV1protocol",
        ["x", "youtube"],
    )

    job = center.get_job(job_id)
    assert job["source_url"] == "https://bilibili.com/video/BV1protocol"
    assert job["source_platform"] == "bilibili"


def test_existing_job_with_missing_protocol_is_migrated(center):
    job_id = center.add_manual_job(
        "https://bilibili.com/video/BV1legacy",
        ["youtube"],
    )
    with center._connect() as connection:
        connection.execute(
            "UPDATE transfer_jobs SET source_url=? WHERE id=?",
            ("bilibili.com/video/BV1legacy", job_id),
        )

    migrated = transfer_module.TransferCenter(config_provider=lambda: {})

    assert migrated.get_job(job_id)["source_url"] == "https://bilibili.com/video/BV1legacy"


def test_chinese_source_download_bypasses_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:17890")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:17890")
    monkeypatch.setenv("NO_PROXY", "localhost")

    env = transfer_module._source_direct_env("bilibili")

    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert ".bilivideo.com" in env["NO_PROXY"]
    assert ".b23.tv" in env["NO_PROXY"]


def test_publish_uses_free_manual_x_mode_without_api_token(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1test12345",
        ["x", "youtube"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {
                "x": {"status": "ready", "path": str(video_path)},
                "youtube": {"status": "ready", "path": str(video_path)},
            }
        ),
        source_attribution="木四点1234\nhttps://www.bilibili.com/video/BV1test12345",
        watermark_status="none",
        recreation_status="approved",
        recreation_completed=1,
        original_contribution="已完成原创口播、事实核验、案例分析和重新编排后的独立结论。",
    )

    result = center.publish_job(job_id)

    assert result["status"] == "ready"
    assert "YouTube 尚未授权" in result["error_message"]
    assert "X 尚未授权" not in result["error_message"]
    assert result["x_publish_status"] == "manual_ready"
    assert result["youtube_publish_status"] == "waiting_auth"


def test_keyword_filters_are_inclusive_and_exclusive(center):
    rule = {
        "include_keywords": "AI, 人工智能",
        "exclude_keywords": "广告, 直播",
    }
    assert center._matches_filters(rule, {"title": "AI工具实测", "description": ""})
    assert not center._matches_filters(rule, {"title": "AI工具广告", "description": ""})
    assert not center._matches_filters(rule, {"title": "旅行记录", "description": ""})


def test_publish_is_blocked_until_source_and_recreation_are_approved(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1rights123",
        ["youtube"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="review",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"youtube": {"status": "ready", "path": str(video_path)}}
        ),
    )

    with pytest.raises(ValueError, match="来源"):
        center.publish_job(job_id)

    center._update_job(job_id, source_attribution="原作者\nhttps://example.com/source")
    with pytest.raises(ValueError, match="再创作"):
        center.publish_job(job_id)


def test_first_scan_preview_and_daily_limit_prevent_bulk_download(center, monkeypatch):
    rule_id = center.save_rule(
        {
            "name": "安全扫描",
            "platform": "bilibili",
            "discovery_mode": "keyword",
            "source_value": "AI",
            "target_platforms": ["youtube"],
            "max_items": 10,
            "max_age_hours": 48,
            "daily_limit": 1,
            "first_scan_preview": True,
            "auto_prepare": True,
        }
    )
    monkeypatch.setattr(
        center,
        "_discover_items",
        lambda rule: [
            {"id": "BV001", "url": "https://www.bilibili.com/video/BV001", "title": "AI 1"},
            {"id": "BV002", "url": "https://www.bilibili.com/video/BV002", "title": "AI 2"},
        ],
    )
    prepared = []
    monkeypatch.setattr(
        center,
        "prepare_job_async",
        lambda job_id, publish_after=False: prepared.append(job_id),
    )

    result = center.scan_rule(rule_id)

    assert result["added"] == 1
    assert result["preview"] is True
    assert prepared == []
    assert center.get_rule(rule_id)["first_scan_completed"] == 1


def test_review_approval_requires_new_media_and_ready_variants(center, tmp_path, monkeypatch):
    job_id = center.add_manual_job(
        "https://www.douyin.com/video/1234567890123456700",
        ["x"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="review",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"x": {"status": "ready", "path": str(video_path)}}
        ),
    )

    with pytest.raises(ValueError, match="上传新的再创作成片"):
        center.save_recreation_review(
            job_id,
            {
                "source_attribution": "原账号\nhttps://www.douyin.com/video/1234567890123456700",
                "original_contribution": "加入三段原创口播、事实核验、案例分析、重新编排镜头，并在结尾给出全新的独立结论。",
                "watermark_status": "none",
                "recreation_confirmed": "on",
            },
            approve=True,
        )

    recreated_path = tmp_path / "recreated.mp4"
    recreated_path.write_bytes(b"new-cut")
    monkeypatch.setattr(
        transfer_module,
        "prepare_platform_variants",
        lambda source_path, output_dir, targets: (
            {"duration": 45, "width": 1080, "height": 1920},
            {"x": {"status": "ready", "path": source_path, "issues": []}},
        ),
    )
    center.replace_recreated_media(job_id, str(recreated_path))

    result = center.save_recreation_review(
        job_id,
        {
            "source_attribution": "原账号\nhttps://www.douyin.com/video/1234567890123456700",
            "recreation_mode": "commentary",
            "original_angle": "验证原观点在国内场景是否成立",
            "original_contribution": "成片加入三段原创口播、事实核验、国内案例分析和重新编排后的独立结论。",
            "watermark_status": "none",
            "watermark_note": "",
            "recreation_confirmed": "on",
            "x_text": "经过验证，我对这个观点有三个不同判断。",
            "youtube_title": "深度验证",
            "youtube_description": "原创分析",
        },
        approve=True,
    )

    assert result["status"] == "ready"
    assert result["recreation_status"] == "approved"
    assert result["source_attribution"].startswith("原账号")


def test_recreated_media_replacement_revokes_previous_approval(center, tmp_path, monkeypatch):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1replacement",
        ["x", "youtube"],
    )
    video_path = tmp_path / "recreated.mp4"
    video_path.write_bytes(b"new-cut")
    center._update_job(
        job_id,
        status="ready",
        recreation_status="approved",
        recreation_completed=1,
        reviewed_at="2026-07-26T00:00:00+00:00",
    )
    monkeypatch.setattr(
        transfer_module,
        "prepare_platform_variants",
        lambda source_path, output_dir, targets: (
            {"duration": 60, "width": 720, "height": 1280},
            {
                "x": {"status": "ready", "path": source_path, "issues": []},
                "youtube": {"status": "ready", "path": source_path, "issues": []},
            },
        ),
    )

    result = center.replace_recreated_media(job_id, str(video_path))

    assert result["status"] == "review"
    assert result["recreation_status"] == "draft"
    assert result["recreation_completed"] == 1
    assert result["reviewed_at"] is None
    assert result["local_video_path"] == str(video_path)


def test_money_printer_url_opens_imported_project(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1moneyprinter",
        ["youtube"],
    )
    center._update_job(job_id, mpt_project_id="project-123")

    url = center.money_printer_url(center.get_job(job_id))

    assert url.startswith("http://192.168.1.249:18081/app/?")
    assert "project_id=project-123" in url
    assert "studio=intelligence" in url


def test_send_to_money_printer_creates_project_uploads_and_analyzes(
    center, tmp_path, monkeypatch
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1mptbridge",
        ["youtube"],
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    center._update_job(
        job_id,
        title="参考视频",
        source_uploader="原账号",
        local_video_path=str(source_path),
        original_video_path=str(source_path),
    )
    calls = []

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": 200, "message": "success", "data": self._data}

    class FakeSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/api/v1/projects"):
                return FakeResponse({"project_id": "project-123"})
            if "/api/v1/assets/upload?" in url:
                return FakeResponse({"asset_id": "asset-456"})
            if url.endswith("/api/v1/projects/project-123/reference-analyses"):
                return FakeResponse({"analysis_id": "analysis-789"})
            raise AssertionError(url)

    monkeypatch.setattr(transfer_module.requests, "Session", FakeSession)

    result = center.send_to_money_printer(job_id)

    assert result["mpt_status"] == "ready"
    assert result["mpt_project_id"] == "project-123"
    assert result["mpt_asset_id"] == "asset-456"
    assert calls[0][0] == "POST"
    assert calls[1][2]["files"]["file"][0] == "source.mp4"
    assert calls[2][2]["json"] == {"asset_id": "asset-456"}
