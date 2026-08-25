import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import modules.config_manager as config_module
import modules.media_preflight as preflight_module
import modules.transfer_center as transfer_module
from modules.notifications import (
    EVENT_TRANSFER_FAILED,
    EVENT_TRANSFER_PUBLISHED,
    EVENT_TRANSFER_REVIEW_READY,
    NotificationEvent,
    build_notification_message,
)


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
    assert rule["auto_prepare"] == 0
    assert rule["auto_publish"] == 0
    assert rule["max_age_hours"] == 48
    assert rule["daily_limit"] == 3
    assert rule["require_review"] == 1


def test_account_auto_prepare_requires_enabled_tracking_source(center):
    payload = {
        "name": "授权B站账号",
        "platform": "bilibili",
        "discovery_mode": "account",
        "source_value": "https://space.bilibili.com/123456",
        "target_platforms": ["youtube"],
        "auto_prepare": True,
    }
    with pytest.raises(ValueError, match="来源跟踪列表"):
        center.save_rule(payload)

    center.save_allowed_source(
        {
            "platform": "bilibili",
            "account_url": "https://space.bilibili.com/123456/",
            "display_name": "本人账号",
            "rights_basis": "owned",
            "rights_note": "本人运营的B站账号",
            "enabled": True,
        }
    )
    rule_id = center.save_rule(payload)
    assert center.get_rule(rule_id)["auto_prepare"] == 1


def test_tracking_source_allows_unconfirmed_non_blocking_risk(center):
    source_id = center.save_allowed_source(
        {
            "platform": "douyin",
            "account_url": "https://www.douyin.com/user/test-account",
            "rights_basis": "unconfirmed",
            "rights_note": "",
            "enabled": True,
        }
    )

    source = next(item for item in center.list_allowed_sources() if item["id"] == source_id)
    assert source["rights_basis"] == "unconfirmed"
    assert source["rights_note"] == ""


def test_scan_copies_allowlist_rights_to_discovered_job(center, monkeypatch):
    account_url = "https://space.bilibili.com/654321"
    center.save_allowed_source(
        {
            "platform": "bilibili",
            "account_url": account_url,
            "display_name": "授权作者",
            "rights_basis": "licensed",
            "rights_note": "许可协议编号 LIC-2026-001",
            "enabled": True,
        }
    )
    rule_id = center.save_rule(
        {
            "name": "授权账号",
            "platform": "bilibili",
            "discovery_mode": "account",
            "source_value": account_url,
            "target_platforms": ["youtube"],
            "auto_prepare": True,
            "first_scan_preview": True,
        }
    )
    monkeypatch.setattr(
        center,
        "_discover_items",
        lambda rule: [
            {
                "id": "BV1allowed",
                "url": "https://www.bilibili.com/video/BV1allowed",
                "title": "授权视频",
                "timestamp": None,
            }
        ],
    )
    result = center.scan_rule(rule_id)
    assert result["added"] == 1
    job = center.list_jobs()[0]
    assert job["status"] == "discovered"
    assert job["processing_mode"] == "professional"
    assert job["rights_basis"] == "licensed"
    assert job["rights_note"] == "许可协议编号 LIC-2026-001"


def test_manual_job_detects_douyin_and_deduplicates(center):
    url = "https://www.douyin.com/video/1234567890123456789"
    job_id = center.add_manual_job(url, ["youtube"])

    job = center.get_job(job_id)
    assert job["source_platform"] == "douyin"
    assert job["status"] == "discovered"

    with pytest.raises(ValueError, match="已经"):
        center.add_manual_job(url, ["youtube"])


def test_manual_job_detects_tiktok_and_routes_cross_platform(center):
    url = "https://www.tiktok.com/@creator/video/6718335390845095173"
    job_id = center.add_manual_job(url, ["youtube", "douyin", "bilibili"])

    job = center.get_job(job_id)
    assert job["source_platform"] == "tiktok"
    assert json.loads(job["target_platforms"]) == ["youtube", "douyin", "bilibili"]
    assert job["tiktok_publish_status"] == "skipped"


def test_tiktok_account_scan_uses_ytdlp_candidates(center, monkeypatch):
    rule_id = center.save_rule(
        {
            "name": "TikTok 科技账号",
            "platform": "tiktok",
            "discovery_mode": "account",
            "source_value": "https://www.tiktok.com/@creator",
            "target_platforms": ["youtube", "douyin"],
            "first_scan_preview": True,
            "auto_prepare": False,
        }
    )
    monkeypatch.setattr(
        center,
        "_yt_dlp_json",
        lambda *_args, **_kwargs: {
            "entries": [
                {
                    "id": "6718335390845095173",
                    "webpage_url": "https://www.tiktok.com/@creator/video/6718335390845095173",
                    "title": "TikTok candidate",
                }
            ]
        },
    )

    result = center.scan_rule(rule_id)
    assert result["success"] is True
    assert result["added"] == 1
    assert center.get_job(center.list_jobs()[0]["id"])["source_platform"] == "tiktok"


def test_bilibili_account_scan_uses_space_video_api(center, monkeypatch):
    rule = {
        "platform": "bilibili",
        "discovery_mode": "account",
        "source_value": "https://space.bilibili.com/123456",
        "max_items": 3,
    }
    monkeypatch.setattr(
        center,
        "_fetch_bilibili_space_videos",
        lambda mid, limit: [
            {
                "bvid": "BV1xx411c7mD",
                "title": "本人账号视频",
                "author": "本人账号",
                "description": "视频简介",
                "pic": "https://example.com/cover.jpg",
                "length": "01:23",
                "created": 1_700_000_000,
            }
        ],
    )
    monkeypatch.setattr(
        center,
        "_yt_dlp_json",
        lambda *_args, **_kwargs: pytest.fail("账号扫描不应把空间主页交给 yt-dlp"),
    )

    items = center._discover_items(rule)

    assert items == [
        {
            "id": "BV1xx411c7mD",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "title": "本人账号视频",
            "uploader": "本人账号",
            "description": "视频简介",
            "thumbnail": "https://example.com/cover.jpg",
            "duration": 83,
            "timestamp": 1_700_000_000,
        }
    ]


def test_empty_first_scan_keeps_preview_guard_for_future_video(center, monkeypatch):
    account_url = "https://space.bilibili.com/123456"
    center.save_allowed_source(
        {
            "platform": "bilibili",
            "account_url": account_url,
            "display_name": "本人账号",
            "rights_basis": "owned",
            "rights_note": "本人运营的 B站账号",
            "enabled": True,
        }
    )
    rule_id = center.save_rule(
        {
            "name": "空账号安全扫描",
            "platform": "bilibili",
            "discovery_mode": "account",
            "source_value": account_url,
            "target_platforms": ["youtube"],
            "first_scan_preview": True,
            "auto_prepare": True,
        }
    )
    monkeypatch.setattr(center, "_discover_items", lambda _rule: [])

    result = center.scan_rule(rule_id)

    assert result["success"] is True
    assert result["found"] == 0
    assert result["preview"] is True
    assert center.get_rule(rule_id)["first_scan_completed"] == 0


def test_ytdlp_web_job_routes_to_bilibili_and_douyin(center):
    job_id = center.add_manual_job(
        "https://www.youtube.com/watch?v=test123",
        ["bilibili", "douyin"],
    )

    job = center.get_job(job_id)
    assert job["source_platform"] == "youtube"
    assert json.loads(job["target_platforms"]) == ["bilibili", "douyin"]
    assert job["bilibili_publish_status"] == "pending"
    assert job["douyin_publish_status"] == "pending"
    assert job["x_publish_status"] == "skipped"
    assert job["youtube_publish_status"] == "skipped"


def test_ytdlp_web_job_rejects_internal_addresses(center):
    with pytest.raises(ValueError, match="内网"):
        center.add_manual_job(
            "http://127.0.0.1:8080/private-video",
            ["bilibili"],
        )


def test_chinese_source_cannot_republish_to_chinese_source_platform(center):
    with pytest.raises(ValueError, match="重复搬运"):
        center.add_manual_job(
            "https://www.bilibili.com/video/BV1sameplatform",
            ["bilibili"],
        )


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


def test_tiktok_source_keeps_egress_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:17890")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:17890")
    monkeypatch.setenv("NO_PROXY", "localhost")

    env = transfer_module._source_direct_env("tiktok")

    assert env["HTTP_PROXY"] == "http://proxy.example:17890"
    assert env["HTTPS_PROXY"] == "http://proxy.example:17890"
    assert ".tiktok.com" not in env["NO_PROXY"]


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


def test_default_youtube_visibility_is_public():
    assert config_module.DEFAULT_CONFIG["TRANSFER_YOUTUBE_PRIVACY"] == "public"


def test_bilibili_and_douyin_media_variants_preserve_source(
    tmp_path, monkeypatch
):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(
        preflight_module,
        "probe_media",
        lambda *_args, **_kwargs: {
            "path": str(video_path),
            "duration": 60,
            "size_bytes": 5,
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "pix_fmt": "yuv420p",
            "audio_channels": 2,
            "has_audio": True,
        },
    )

    _, variants = preflight_module.prepare_platform_variants(
        str(video_path),
        str(tmp_path),
        ["bilibili", "douyin"],
    )

    assert variants["bilibili"]["path"] == str(video_path)
    assert variants["douyin"]["path"] == str(video_path)
    assert variants["bilibili"]["status"] == "ready"
    assert variants["douyin"]["status"] == "ready"


def test_tiktok_media_variant_preserves_source(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(
        preflight_module,
        "probe_media",
        lambda *_args, **_kwargs: {
            "path": str(video_path),
            "duration": 60,
            "size_bytes": 5,
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "pix_fmt": "yuv420p",
            "audio_channels": 2,
            "has_audio": True,
        },
    )

    _, variants = preflight_module.prepare_platform_variants(
        str(video_path),
        str(tmp_path),
        ["tiktok"],
    )

    assert variants["tiktok"]["path"] == str(video_path)
    assert variants["tiktok"]["status"] == "ready"


def test_x_web_intent_prefills_publish_text():
    url = transfer_module.build_x_web_intent_url("世界杯观察：三个结论")

    assert url.startswith("https://x.com/intent/post?")
    assert "%E4%B8%96%E7%95%8C%E6%9D%AF" in url


def test_manual_x_confirmation_completes_cross_platform_job(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1manualx",
        ["x", "youtube"],
    )
    center._update_job(
        job_id,
        status="ready",
        x_publish_status="manual_ready",
        youtube_publish_status="completed",
        youtube_video_id="youtube-video-id",
    )

    result = center.mark_x_manually_published(
        job_id,
        "https://x.com/allice/status/123456789",
    )

    assert result["status"] == "completed"
    assert result["x_publish_status"] == "completed"
    assert result["x_post_id"] == "https://x.com/allice/status/123456789"
    assert result["progress_percent"] == 100


def test_bilibili_publish_uses_server_uploader(center, tmp_path, monkeypatch):
    job_id = center.add_manual_job(
        "https://www.youtube.com/watch?v=bili-upload",
        ["bilibili"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"bilibili": {"status": "ready", "path": str(video_path)}}
        ),
        source_attribution="原作者\nhttps://youtube.com/watch?v=bili-upload",
        watermark_status="third_party_preserved",
        recreation_status="approved",
        bilibili_title="测试投稿",
        bilibili_description="来源说明",
        bilibili_partition_id="21",
    )
    monkeypatch.setattr(
        center,
        "_publish_bilibili",
        lambda *_args, **_kwargs: "BV1serverupload",
    )

    result = center.publish_job(job_id)

    assert result["status"] == "completed"
    assert result["bilibili_publish_status"] == "completed"
    assert result["bilibili_post_id"] == "BV1serverupload"


def test_douyin_publish_is_free_manual_handoff(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.youtube.com/watch?v=douyin-upload",
        ["douyin"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"douyin": {"status": "ready", "path": str(video_path)}}
        ),
        source_attribution="原作者\nhttps://youtube.com/watch?v=douyin-upload",
        watermark_status="third_party_preserved",
        recreation_status="approved",
        douyin_text="测试抖音文案",
    )

    ready = center.publish_job(job_id)
    assert ready["status"] == "ready"
    assert ready["douyin_publish_status"] == "manual_ready"

    completed = center.mark_douyin_manually_published(
        job_id,
        "https://www.douyin.com/video/1234567890",
    )
    assert completed["status"] == "completed"
    assert completed["douyin_publish_status"] == "completed"


def test_douyin_openapi_publish_keeps_explicit_review_gate(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.youtube.com/watch?v=douyin-openapi",
        ["douyin"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"douyin": {"status": "ready", "path": str(video_path)}}
        ),
        source_attribution="原作者\nhttps://youtube.com/watch?v=douyin-openapi",
        watermark_status="third_party_preserved",
        recreation_status="approved",
        douyin_publish_status="manual_ready",
        douyin_text="已审核的抖音文案",
    )
    received = {}

    def publisher(path, text, progress_callback):
        received.update({"path": path, "text": text})
        progress_callback(1.0)
        return {"item_id": "douyin-item-id", "video_id": "douyin-video-id"}

    assert center._claim_active_job(job_id) is True
    center._publish_douyin_openapi_guarded(job_id, publisher)

    completed = center.get_job(job_id)
    assert received == {
        "path": str(video_path),
        "text": "已审核的抖音文案",
    }
    assert completed["status"] == "completed"
    assert completed["douyin_publish_status"] == "completed"
    assert completed["douyin_post_id"] == "douyin-item-id"


def test_tiktok_publish_is_free_manual_handoff(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.youtube.com/watch?v=tiktok-upload",
        ["tiktok"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"tiktok": {"status": "ready", "path": str(video_path)}}
        ),
        source_attribution="原作者\nhttps://youtube.com/watch?v=tiktok-upload",
        watermark_status="third_party_preserved",
        recreation_status="approved",
        tiktok_text="测试 TikTok 文案",
    )

    ready = center.publish_job(job_id)
    assert ready["status"] == "ready"
    assert ready["tiktok_publish_status"] == "manual_ready"

    completed = center.mark_tiktok_manually_published(
        job_id,
        "https://www.tiktok.com/@creator/video/6718335390845095173",
    )
    assert completed["status"] == "completed"
    assert completed["tiktok_publish_status"] == "completed"


def test_youtube_connection_requires_verified_channel(center, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    token_path = config_dir / "youtube_transfer_token.json"
    token_path.write_text(
        json.dumps({"scopes": [transfer_module.YOUTUBE_UPLOAD_SCOPE]}),
        encoding="utf-8",
    )

    state = transfer_module.youtube_connection_state()
    assert state["status"] == "reconnect_required"
    assert state["connected"] is False

    token_path.write_text(
        json.dumps({"scopes": list(transfer_module.YOUTUBE_SCOPES)}),
        encoding="utf-8",
    )
    (config_dir / "youtube_transfer_channel.json").write_text(
        json.dumps(
            {
                "channel_id": "UC123",
                "channel_title": "Allice",
                "verified_at": "2026-07-30T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    state = transfer_module.youtube_connection_state()
    assert state["connected"] is True
    assert state["channel_title"] == "Allice"


def test_youtube_oauth_redirect_uses_stable_public_https_url():
    redirect_uri = transfer_module.build_youtube_oauth_redirect_uri(
        "https://transfer.sg99.online/"
    )
    assert redirect_uri == (
        "https://transfer.sg99.online/transfer-center/youtube/callback"
    )

    proxied_redirect_uri = transfer_module.build_youtube_oauth_redirect_uri(
        request_host="transfer.sg99.online",
        request_scheme="http",
    )
    assert proxied_redirect_uri == redirect_uri

    with pytest.raises(ValueError, match="HTTPS"):
        transfer_module.build_youtube_oauth_redirect_uri(
            "http://transfer.sg99.online"
        )


def test_youtube_signup_error_waits_for_reconnect_without_retry(
    center, tmp_path, monkeypatch
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1youtubeauth",
        ["youtube"],
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "youtube_transfer_token.json").write_text(
        json.dumps({"scopes": list(transfer_module.YOUTUBE_SCOPES)}),
        encoding="utf-8",
    )
    channel_path = config_dir / "youtube_transfer_channel.json"
    channel_path.write_text(
        json.dumps({"channel_id": "UC123", "channel_title": "Wrong account"}),
        encoding="utf-8",
    )
    center._update_job(
        job_id,
        status="ready",
        local_video_path=str(video_path),
        platform_variants_json=json.dumps(
            {"youtube": {"status": "ready", "path": str(video_path)}}
        ),
        source_attribution="原账号\nhttps://example.com/source",
        watermark_status="none",
        recreation_status="approved",
    )
    monkeypatch.setattr(
        center,
        "_publish_youtube",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("401 youtubeSignupRequired Unauthorized")
        ),
    )

    result = center.publish_job(job_id)

    assert result["status"] == "ready"
    assert result["youtube_publish_status"] == "waiting_auth"
    assert result["next_retry_at"] is None
    assert result["last_retry_stage"] == ""
    assert "重新连接" in result["error_message"]
    assert not channel_path.exists()


def test_verified_channel_restores_waiting_jobs_to_publishable(center):
    job_id = center.add_manual_job(
        "https://www.douyin.com/video/1234567890123456123",
        ["youtube"],
    )
    center._update_job(
        job_id,
        status="ready",
        youtube_publish_status="waiting_auth",
        next_retry_at="2026-07-30T00:00:00+00:00",
        last_retry_stage="publish",
        error_message="YouTube 授权失效",
    )

    restored = center.mark_youtube_reconnected()
    result = center.get_job(job_id)

    assert restored == 1
    assert result["status"] == "ready"
    assert result["youtube_publish_status"] == "pending"
    assert result["next_retry_at"] is None
    assert result["last_retry_stage"] == ""
    assert result["error_message"] == ""
    assert result["progress_message"] == "YouTube 频道已连接，等待发布"


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
    with pytest.raises(ValueError, match="人工确认"):
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
                "processing_mode": "professional",
                "original_contribution": "加入三段原创口播、事实核验、案例分析、重新编排镜头，并在结尾给出全新的独立结论。",
                "watermark_status": "none",
                "recreation_confirmed": "on",
                "x_text": "原创观点核对",
                "cover_preflight_confirmed": "on",
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
            "processing_mode": "professional",
            "recreation_mode": "commentary",
            "original_angle": "验证原观点在国内场景是否成立",
            "original_contribution": "成片加入三段原创口播、事实核验、国内案例分析和重新编排后的独立结论。",
            "watermark_status": "none",
            "watermark_note": "",
            "recreation_confirmed": "on",
            "x_text": "经过验证，我对这个观点有三个不同判断。",
            "youtube_title": "深度验证",
            "youtube_description": "原创分析",
            "cover_preflight_confirmed": "on",
        },
        approve=True,
    )

    assert result["status"] == "ready"
    assert result["recreation_status"] == "approved"
    assert result["source_attribution"].startswith("原账号")


def test_direct_transfer_can_be_approved_without_uploading_new_media(
    center, tmp_path, monkeypatch
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1direct",
        ["youtube"],
    )
    video_path = tmp_path / "original.mp4"
    video_path.write_bytes(b"original")
    monkeypatch.setattr(
        transfer_module,
        "prepare_platform_variants",
        lambda source_path, output_dir, targets: (
            {"duration": 30, "width": 1920, "height": 1080},
            {"youtube": {"status": "ready", "path": source_path, "issues": []}},
        ),
    )
    center._update_job(
        job_id,
        status="review",
        local_video_path=str(video_path),
        original_video_path=str(video_path),
        source_attribution="原作者\nhttps://www.bilibili.com/video/BV1direct",
        platform_variants_json=json.dumps(
            {"youtube": {"status": "ready", "path": str(video_path), "issues": []}}
        ),
    )

    result = center.save_recreation_review(
        job_id,
        {
            "source_attribution": "原作者\nhttps://www.bilibili.com/video/BV1direct",
            "processing_mode": "direct",
            "watermark_status": "third_party_preserved",
            "watermark_note": "画面保留原作者账号",
            "publish_confirmed": "on",
            "youtube_title": "原视频转发说明",
            "cover_preflight_confirmed": "on",
        },
        approve=True,
    )

    assert result["status"] == "ready"
    assert result["processing_mode"] == "direct"
    assert result["recreation_completed"] == 0


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

    assert url.startswith("https://video.sg99.online/app/?")
    assert "project_id=project-123" in url
    assert "studio=intelligence" in url
    assert "workflow=professional" in url

    professional_url = center.money_printer_url(
        center.get_job(job_id), workflow="professional"
    )
    assert "studio=intelligence" in professional_url
    assert "workflow=professional" in professional_url


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
    assert result["processing_mode"] == "professional"
    assert calls[0][0] == "POST"
    assert calls[1][2]["files"]["file"][0] == "source.mp4"
    assert calls[2][2]["json"] == {"asset_id": "asset-456"}


def test_recreation_plan_contains_ready_to_voice_commentary(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1commentary",
        ["youtube"],
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    center._update_job(
        job_id,
        title="测试主题",
        source_uploader="原账号",
        local_video_path=str(source_path),
        original_video_path=str(source_path),
    )

    result = center.generate_recreation_draft(job_id)

    assert len(result["commentary_script"]) >= 80
    assert "来源" in result["commentary_script"]


def test_recreation_plan_reads_local_subtitle_timecodes(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1timedsubtitle",
        ["youtube"],
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    (tmp_path / "source.zh.srt").write_text(
        "1\n00:00:04,000 --> 00:00:08,000\n第一个观点\n\n"
        "2\n00:00:20,000 --> 00:00:26,000\n第二个观点\n",
        encoding="utf-8",
    )
    center._update_job(
        job_id,
        title="字幕测试",
        source_uploader="原账号",
        local_video_path=str(source_path),
        original_video_path=str(source_path),
        duration=30,
    )

    result = center.generate_recreation_draft(job_id)
    plan = json.loads(result["recreation_plan_json"])

    assert plan["transcript_source"] == "source.zh.srt"
    assert plan["transcript_cue_count"] == 2
    assert plan["segment_plan"][0]["source_start"] == 4.0
    assert plan["segment_plan"][1]["source_end"] == 26.0


def test_sync_recreation_plan_updates_existing_shots_without_paid_generation(
    center, monkeypatch
):
    calls = []

    def fake_api(session, base_url, headers, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "shots": [
                    {"shot_id": "shot-1"},
                    {"shot_id": "shot-2"},
                    {"shot_id": "shot-3"},
                ]
            }
        return {"shot_id": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(center, "_money_printer_json", fake_api)
    plan = {
        "segment_plan": [
            {
                "stage": "开场",
                "source_start": 5,
                "duration": 3,
                "action": "trim",
                "narration": "原创开场",
                "visual": "结果画面",
            },
            {
                "stage": "结论",
                "source_start": 40,
                "duration": 8,
                "action": "keep",
                "narration": "独立结论",
                "visual": "结论卡",
            },
        ],
        "broll_suggestions": ["产品录屏"],
        "ai_visual_prompts": ["无文字背景"],
        "platform_versions": {
            "douyin": {"format": "9:16", "edit_note": "前三秒给结果"}
        },
    }

    summary = center._sync_recreation_plan_to_money_printer(
        object(), "http://mpt.local", {"x-api-key": "test"}, "project-1", plan, aspect="9:16"
    )

    put_calls = [call for call in calls if call[0] == "PUT"]
    assert summary["mapped_shots"] == 2
    assert summary["excluded_shots"] == 1
    assert summary["paid_generation_triggered"] is False
    assert put_calls[0][2]["json"]["source_start"] == 5.0
    assert put_calls[0][2]["json"]["asset_hint"] == "产品录屏"
    assert put_calls[0][2]["json"]["image_prompt"] == "无文字背景"
    assert put_calls[-1][2]["json"]["included"] is False
    assert not any("/generate" in path for _, path, _ in calls)


def test_money_printer_connection_loads_private_credential(center, monkeypatch, tmp_path):
    credential_path = tmp_path / "mpt_internal_credentials.json"
    credential_path.write_text('{"api_key":"internal-test-key"}', encoding="utf-8")
    monkeypatch.setattr(
        transfer_module,
        "get_app_subdir",
        lambda name: str(tmp_path) if name == "config" else str(tmp_path / name),
    )

    base_url, headers = center._money_printer_connection()

    assert base_url == "http://172.17.0.1:8080"
    assert headers == {"x-api-key": "internal-test-key"}


def test_transfer_notification_messages_include_review_link():
    payload = {
        "task_id": "job-123",
        "title": "待审核视频",
        "targets": "bilibili、douyin",
        "status": "review",
        "review_url": "https://transfer.sg99.online/transfer-center/jobs/job-123/review",
        "error_message": "渲染失败",
    }

    review = build_notification_message(
        NotificationEvent(EVENT_TRANSFER_REVIEW_READY, payload)
    )
    published = build_notification_message(
        NotificationEvent(EVENT_TRANSFER_PUBLISHED, payload)
    )
    failed = build_notification_message(
        NotificationEvent(EVENT_TRANSFER_FAILED, payload)
    )

    assert "待审核" in review.title
    assert payload["review_url"] in review.markdown
    assert "发布完成" in published.title
    assert "渲染失败" in failed.markdown


def test_maintenance_backs_up_db_and_only_cleans_old_completed_jobs(center, tmp_path):
    completed_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1oldcompleted", ["youtube"]
    )
    review_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1oldreview", ["youtube"]
    )
    completed_dir = tmp_path / "downloads" / "transfer" / completed_id
    review_dir = tmp_path / "downloads" / "transfer" / review_id
    completed_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    (completed_dir / "video.mp4").write_bytes(b"completed")
    (review_dir / "video.mp4").write_bytes(b"review")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET status='completed', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(completed_dir / "video.mp4"), completed_id),
        )
        conn.execute(
            "UPDATE transfer_jobs SET status='review', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(review_dir / "video.mp4"), review_id),
        )

    result = center.run_maintenance()

    assert os.path.isfile(result["backup"])
    assert result["cleaned_jobs"] == 1
    assert not completed_dir.exists()
    assert review_dir.exists()
    assert center.get_job(completed_id)["media_cleaned_at"]
    assert center.get_job(review_id)["local_video_path"].endswith("video.mp4")


def test_performance_summary_uses_latest_platform_snapshot(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1performance", ["youtube"]
    )
    center._update_job(
        job_id,
        title="效果测试",
        youtube_video_id="youtube-123",
        status="completed",
    )
    center.record_performance(
        job_id,
        {"platform": "youtube", "views": 100, "likes": 2, "comments": 1, "shares": 0},
    )
    center.record_performance(
        job_id,
        {
            "platform": "youtube",
            "views": 1000,
            "likes": 60,
            "comments": 20,
            "shares": 10,
            "followers_delta": 8,
        },
    )

    summary = center.get_performance_summary(days=7)

    assert len(summary["records"]) == 1
    assert summary["totals"]["views"] == 1000
    assert summary["totals"]["engagement_rate"] == 9.0
    assert summary["top_platform"] == "youtube"
    assert any("互动率较高" in item for item in summary["suggestions"])


def test_performance_rejects_platform_without_published_post(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1notpublished", ["youtube"]
    )
    with pytest.raises(ValueError, match="尚无已发布"):
        center.record_performance(job_id, {"platform": "youtube", "views": 1})


def test_sync_money_printer_render_downloads_and_rechecks_media(
    center, tmp_path, monkeypatch
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1mptsync",
        ["x", "youtube"],
    )
    center._update_job(
        job_id,
        mpt_project_id="project-123",
        mpt_workflow="quick",
        mpt_status="ready",
    )

    class FakeResponse:
        def __init__(self, *, payload=None, content=b""):
            self._payload = payload
            self._content = content
            self.headers = {"Content-Length": str(len(content))} if content else {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def iter_content(self, chunk_size):
            yield self._content

    class FakeSession:
        trust_env = True

        def get(self, url, **kwargs):
            if url.endswith("/api/v1/projects/project-123/renders"):
                return FakeResponse(
                    payload={
                        "status": 200,
                        "data": {
                            "renders": [
                                {
                                    "render_id": "render-456",
                                    "status": "ready",
                                    "output_asset": {
                                        "content_url": "/api/v1/assets/asset-789/content"
                                    },
                                }
                            ]
                        },
                    }
                )
            if url.endswith("/api/v1/assets/asset-789/content"):
                return FakeResponse(content=b"finished-video")
            raise AssertionError(url)

    monkeypatch.setattr(transfer_module.requests, "Session", FakeSession)
    monkeypatch.setattr(
        transfer_module,
        "prepare_platform_variants",
        lambda source_path, output_dir, targets: (
            {"duration": 65, "width": 1920, "height": 1080, "has_audio": True},
            {
                "x": {"status": "ready", "path": source_path, "issues": []},
                "youtube": {"status": "ready", "path": source_path, "issues": []},
            },
        ),
    )

    result = center.sync_money_printer_render(job_id)

    assert result["mpt_status"] == "imported"
    assert result["processing_mode"] == "quick"
    assert result["recreation_completed"] == 1
    assert result["recreation_status"] == "draft"
    assert result["local_video_path"].endswith("mpt-render-render-456.mp4")
    assert open(result["local_video_path"], "rb").read() == b"finished-video"
