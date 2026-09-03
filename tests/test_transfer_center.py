import json
import os
import sqlite3
import zipfile
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

import modules.config_manager as config_module
import modules.media_preflight as preflight_module
import modules.notifications as notifications_module
import modules.transfer_center as transfer_module
from modules.notifications import (
    EVENT_TRANSFER_BACKUP_COMPLETED,
    EVENT_TRANSFER_BACKUP_FAILED,
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


@pytest.mark.parametrize("processing_mode", ["direct", "quick", "professional"])
def test_manual_job_keeps_selected_processing_mode(center, processing_mode):
    job_id = center.add_manual_job(
        f"https://example.com/video-{processing_mode}",
        ["bilibili"],
        processing_mode=processing_mode,
    )

    assert center.get_job(job_id)["processing_mode"] == processing_mode


def test_manual_job_detects_x_post_and_routes_cross_platform(center):
    url = "https://x.com/lengxiaohua/status/2093551021578699090?s=20"
    job_id = center.add_manual_job(url, ["youtube", "bilibili", "douyin"])

    job = center.get_job(job_id)
    assert job["source_platform"] == "x"
    assert json.loads(job["target_platforms"]) == ["youtube", "bilibili", "douyin"]
    assert job["x_publish_status"] == "skipped"


def test_manual_job_detects_legacy_twitter_post(center):
    job_id = center.add_manual_job(
        "https://twitter.com/lengxiaohua/status/2093551021578699090",
        ["youtube"],
    )

    assert center.get_job(job_id)["source_platform"] == "x"


def test_x_source_cannot_republish_to_x(center):
    with pytest.raises(ValueError, match="X来源不能再次发布到同一平台"):
        center.add_manual_job(
            "https://x.com/lengxiaohua/status/2093551021578699090",
            ["x"],
        )


def test_ytdlp_command_uses_active_python_runtime():
    command = transfer_module._yt_dlp_command()

    assert command[0] == transfer_module.sys.executable
    assert command[1:] == ["-m", "yt_dlp"]


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


def test_existing_generic_x_job_is_migrated_to_x_source(center):
    job_id = center.add_manual_job(
        "https://example.com/public-video",
        ["bilibili"],
    )
    with center._connect() as connection:
        connection.execute(
            "UPDATE transfer_jobs SET source_platform=?, source_url=? WHERE id=?",
            (
                "web",
                "https://x.com/lengxiaohua/status/2093551021578699090",
                job_id,
            ),
        )

    migrated = transfer_module.TransferCenter(config_provider=lambda: {})

    assert migrated.get_job(job_id)["source_platform"] == "x"


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

    assert url.startswith("http://127.0.0.1:8080/app/?")
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


def test_recreation_regeneration_reuses_saved_visual_analysis(center, tmp_path):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1visualcontext",
        ["youtube"],
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    visual_analysis = {
        "status": "ok",
        "summary": "人物在室内展示产品",
        "frames": [],
        "suggested_segments": [
            {"start": 2.0, "end": 8.0, "reason": "动作完整", "score": 0.9}
        ],
        "warnings": [],
        "frame_count": 6,
        "elapsed_seconds": 2.4,
    }
    center._update_job(
        job_id,
        local_video_path=str(source_path),
        original_video_path=str(source_path),
        recreation_plan_json=json.dumps(
            {"visual_analysis": visual_analysis}, ensure_ascii=False
        ),
    )

    enriched = center._recreation_input_job(center.get_job(job_id))

    assert enriched["visual_analysis"] == visual_analysis


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
    assert put_calls[0][2]["json"]["image_prompt"] == "结果画面；无文字背景"
    assert put_calls[-1][2]["json"]["included"] is False
    assert not any("/generate" in path for _, path, _ in calls)


def test_local_replacement_materialization_uses_only_zero_cost_provider(
    center, monkeypatch
):
    calls = []

    def fake_api(session, base_url, headers, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and "/projects/" in path:
            return {"shots": [{"shot_id": "shot-1", "versions": []}]}
        if method == "POST" and path.endswith("/generate"):
            return {"version": {"version_id": "version-local-1"}}
        if method == "GET" and path.endswith("/shots/shot-1"):
            return {
                "shot_id": "shot-1",
                "versions": [
                    {
                        "version_id": "version-local-1",
                        "provider": "local_motion",
                        "status": "ready",
                    }
                ],
            }
        if method == "POST" and path.endswith("/versions/version-local-1/select"):
            return {"shot_id": "shot-1", "selected_version_id": "version-local-1"}
        if method == "PUT" and path.endswith("/shots/shot-1"):
            return {"shot_id": "shot-1", "source_start": 0}
        raise AssertionError((method, path))

    monkeypatch.setattr(center, "_money_printer_json", fake_api)
    summary = center._materialize_local_replacement_shots(
        object(),
        "http://mpt.local",
        {"x-api-key": "test"},
        "project-1",
        {
            "segment_plan": [
                {
                    "stage": "开场信息卡",
                    "source_start": 0,
                    "duration": 4,
                    "action": "replace",
                    "visual": "简洁的三步流程画面",
                }
            ]
        },
        aspect="9:16",
    )

    generate_body = next(
        kwargs["json"] for method, path, kwargs in calls if path.endswith("/generate")
    )
    assert summary["status"] == "completed"
    assert summary["completed_shots"] == 1
    assert summary["provider"] == "local_motion"
    assert summary["billing_mode"] == "local_compute"
    assert summary["cost_cny"] == 0.0
    assert summary["paid_generation_triggered"] is False
    assert generate_body["provider"] == "local_motion"
    assert "confirm_paid_generation" not in generate_body
    reset_body = next(
        kwargs["json"] for method, path, kwargs in calls if method == "PUT"
    )
    assert reset_body["source_start"] == 0


def test_local_replacement_failure_keeps_source_as_fallback(center, monkeypatch):
    def fake_api(session, base_url, headers, method, path, **kwargs):
        if method == "GET":
            return {"shots": [{"shot_id": "shot-1", "versions": []}]}
        raise RuntimeError("本地 FFmpeg 不可用")

    monkeypatch.setattr(center, "_money_printer_json", fake_api)
    summary = center._materialize_local_replacement_shots(
        object(),
        "http://mpt.local",
        {},
        "project-1",
        {
            "segment_plan": [
                {
                    "stage": "结尾信息卡",
                    "source_start": 50,
                    "duration": 5,
                    "action": "replace",
                    "visual": "独立结论卡",
                }
            ]
        },
        aspect="16:9",
    )

    assert summary["status"] == "partial"
    assert summary["completed_shots"] == 0
    assert summary["fallback_shots"] == 1
    assert "已保留原片" in summary["warnings"][0]


def test_local_selection_failure_restores_original_source_start(center, monkeypatch):
    put_values = []

    def fake_api(session, base_url, headers, method, path, **kwargs):
        if method == "GET" and "/projects/" in path:
            return {"shots": [{"shot_id": "shot-1", "versions": []}]}
        if method == "POST" and path.endswith("/generate"):
            return {"version": {"version_id": "version-local-1"}}
        if method == "GET" and path.endswith("/shots/shot-1"):
            return {
                "versions": [
                    {"version_id": "version-local-1", "status": "ready"}
                ]
            }
        if method == "PUT":
            put_values.append(kwargs["json"]["source_start"])
            return {"shot_id": "shot-1"}
        if method == "POST" and path.endswith("/select"):
            raise RuntimeError("质检未通过")
        raise AssertionError((method, path))

    monkeypatch.setattr(center, "_money_printer_json", fake_api)
    summary = center._materialize_local_replacement_shots(
        object(),
        "http://mpt.local",
        {},
        "project-1",
        {
            "segment_plan": [
                {
                    "stage": "中段信息卡",
                    "source_start": 40,
                    "duration": 5,
                    "action": "replace",
                    "visual": "关键数据卡",
                }
            ]
        },
        aspect="16:9",
    )

    assert summary["fallback_shots"] == 1
    assert put_values == [0, 40.0]


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


def test_runtime_capacity_blocks_large_writes_below_configured_floor(center, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        transfer_module.shutil,
        "disk_usage",
        lambda _path: usage(100 * 1024**3, 95 * 1024**3, 5 * 1024**3),
    )
    center._config_provider = lambda: {"TRANSFER_MIN_FREE_DISK_GB": 8}

    status = center.runtime_capacity()

    assert status["ready"] is False
    assert status["free_gb"] == 5.0
    assert status["minimum_free_gb"] == 8.0
    with pytest.raises(RuntimeError, match="至少需要保留 8.0 GB"):
        center._assert_runtime_capacity("下载原片")


def test_runtime_health_reports_authenticated_money_printer(center, monkeypatch):
    center._config_provider = lambda: {
        "TRANSFER_MPT_INTERNAL_URL": "http://moneyprinter-video-worker:8080",
        "TRANSFER_MPT_API_KEY": "test-key",
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": 200, "message": "success", "data": {"items": []}}

    class FakeSession:
        trust_env = True

        def get(self, url, **kwargs):
            assert url == "http://moneyprinter-video-worker:8080/api/v1/projects"
            assert kwargs["headers"] == {"x-api-key": "test-key"}
            assert kwargs["params"] == {"limit": 1}
            assert self.trust_env is False
            return FakeResponse()

    monkeypatch.setattr(transfer_module.requests, "Session", FakeSession)

    status = center.money_printer_health()

    assert status == {
        "configured": True,
        "reachable": True,
        "ready": True,
        "message": "超级印钞机制作端可用",
    }


def test_runtime_health_explains_material_package_fallback_without_credentials(center):
    status = center.money_printer_health()

    assert status["configured"] is False
    assert status["ready"] is False
    assert "二剪素材包" in status["message"]


def test_runtime_health_includes_redacted_local_visual_status(center, monkeypatch):
    monkeypatch.setattr(center, "runtime_capacity", lambda: {"ready": True})
    monkeypatch.setattr(center, "money_printer_health", lambda: {"ready": True})
    monkeypatch.setattr(center, "backup_health", lambda: {"ready": True})
    monkeypatch.setattr(
        transfer_module,
        "local_visual_health",
        lambda config: {
            "status": "available",
            "enabled": True,
            "available": True,
            "model": "Qwen3-VL-2B-Instruct-4bit",
            "message": "本地视觉服务可用",
        },
    )

    result = center.runtime_health()

    assert result["local_vision"]["available"] is True
    assert result["local_vision"]["model"] == "Qwen3-VL-2B-Instruct-4bit"


def test_transfer_notification_messages_include_configured_review_link():
    payload = {
        "task_id": "job-123",
        "title": "待审核视频",
        "targets": "bilibili、douyin",
        "status": "review",
        "review_url": "http://127.0.0.1:15188/transfer-center/jobs/job-123/review",
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


def test_local_transfer_notification_uses_macbook_instruction_without_retired_link(
    center, monkeypatch
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1localnotify", ["youtube"]
    )
    center._update_job(job_id, title="本地待审核", status="review")
    captured = []
    monkeypatch.setattr(
        notifications_module,
        "emit_notification_event",
        captured.append,
    )

    center._emit_transfer_notification("review_ready", job_id)

    assert len(captured) == 1
    payload = captured[0].as_payload()
    message = build_notification_message(captured[0])
    assert payload["review_url"] == ""
    assert "MacBook" in message.markdown
    assert "transfer.sg99.online" not in message.markdown


def test_transfer_backup_notification_reports_verified_path_and_failure():
    payload = {
        "task_id": "job-backup",
        "title": "成片备份",
        "backup_path": "/115-视频备份/视频搬运/2026/08/job-backup",
        "backup_bytes": 10 * 1024 * 1024,
        "error_message": "OpenList unavailable",
    }

    completed = build_notification_message(
        NotificationEvent(EVENT_TRANSFER_BACKUP_COMPLETED, payload)
    )
    failed = build_notification_message(
        NotificationEvent(EVENT_TRANSFER_BACKUP_FAILED, payload)
    )

    assert "115备份完成" in completed.title
    assert payload["backup_path"] in completed.markdown
    assert "10.0 MB" in completed.markdown
    assert "OpenList unavailable" in failed.markdown


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
            "UPDATE transfer_jobs SET status='completed', backup_status='completed', updated_at=?, local_video_path=? WHERE id=?",
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


def test_maintenance_requires_completed_status_and_verified_or_disabled_backup(
    center, tmp_path
):
    completed_disabled_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1backupdisabled", ["youtube"]
    )
    completed_pending_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1backuppending", ["youtube"]
    )
    failed_backed_up_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1failedbackup", ["youtube"]
    )
    job_dirs = {}
    for job_id in (
        completed_disabled_id,
        completed_pending_id,
        failed_backed_up_id,
    ):
        job_dir = tmp_path / "downloads" / "transfer" / job_id
        job_dir.mkdir(parents=True)
        video_path = job_dir / "video.mp4"
        video_path.write_bytes(job_id.encode())
        job_dirs[job_id] = (job_dir, video_path)

    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET status='completed', backup_status='disabled', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(job_dirs[completed_disabled_id][1]), completed_disabled_id),
        )
        conn.execute(
            "UPDATE transfer_jobs SET status='completed', backup_status='pending', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(job_dirs[completed_pending_id][1]), completed_pending_id),
        )
        conn.execute(
            "UPDATE transfer_jobs SET status='failed', backup_status='completed', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(job_dirs[failed_backed_up_id][1]), failed_backed_up_id),
        )

    result = center.run_maintenance()

    assert result["cleaned_jobs"] == 1
    assert not job_dirs[completed_disabled_id][0].exists()
    assert job_dirs[completed_pending_id][0].exists()
    assert job_dirs[failed_backed_up_id][0].exists()


def test_maintenance_honors_one_day_retention_after_verified_backup(center, tmp_path):
    center._config_provider = lambda: {
        "TRANSFER_COMPLETED_MEDIA_RETENTION_DAYS": 1,
        "TRANSFER_DB_BACKUP_RETENTION_DAYS": 14,
    }
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1onedayretention", ["youtube"]
    )
    job_dir = tmp_path / "downloads" / "transfer" / job_id
    job_dir.mkdir(parents=True)
    video_path = job_dir / "video.mp4"
    video_path.write_bytes(b"verified-backup")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET status='completed', backup_status='completed', updated_at=?, local_video_path=? WHERE id=?",
            (old, str(video_path), job_id),
        )

    result = center.run_maintenance()

    assert result["cleaned_jobs"] == 1
    assert not job_dir.exists()


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


def test_performance_summary_calculates_retention_profit_and_visual_mix(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1businessloop", ["youtube"]
    )
    center._update_job(
        job_id,
        title="收益闭环测试",
        youtube_video_id="youtube-business-1",
        status="completed",
        recreation_plan_json=json.dumps(
            {
                "timeline_sync": {"mapped_shots": 4},
                "material_fulfillment": {"completed_shots": 2},
            },
            ensure_ascii=False,
        ),
    )
    center.record_performance(
        job_id,
        {
            "platform": "youtube",
            "impressions": 2000,
            "views": 1000,
            "likes": 50,
            "comments": 10,
            "shares": 5,
            "followers_delta": 8,
            "average_view_duration": 42.5,
            "completion_rate": 40,
            "retention_3s": 60,
            "revenue_cny": 50,
            "production_cost_cny": 5,
            "monetization_status": "settled",
        },
    )

    summary = center.get_performance_summary(days=7)
    record = summary["records"][0]

    assert record["ctr"] == 50.0
    assert record["net_revenue_cny"] == 45.0
    assert record["source_visual_ratio"] == 50.0
    assert record["local_visual_ratio"] == 50.0
    assert summary["totals"]["average_view_duration"] == 42.5
    assert summary["totals"]["completion_rate"] == 40.0
    assert summary["totals"]["follow_conversion_rate"] == 0.8
    assert summary["totals"]["net_rpm_cny"] == 45.0
    assert "结果" in summary["strategy"]["hook_guidance"]


def test_performance_strategy_compares_local_visual_completion(center):
    for suffix, local_ratio, completion in (
        ("local", 40, 60),
        ("source", 5, 35),
    ):
        job_id = center.add_manual_job(
            f"https://www.bilibili.com/video/BV1strategy{suffix}", ["youtube"]
        )
        center._update_job(
            job_id,
            youtube_video_id=f"youtube-{suffix}",
            status="completed",
        )
        center.record_performance(
            job_id,
            {
                "platform": "youtube",
                "views": 1000,
                "completion_rate": completion,
                "source_visual_ratio": 100 - local_ratio,
                "local_visual_ratio": local_ratio,
            },
        )

    summary = center.get_performance_summary(days=7)

    assert summary["strategy"]["target_local_visual_ratio"] == 45
    assert any("提到约 45%" in item for item in summary["suggestions"])


def test_performance_rejects_visual_ratios_over_one_hundred(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1invalidratio", ["youtube"]
    )
    center._update_job(job_id, youtube_video_id="youtube-invalid", status="completed")

    with pytest.raises(ValueError, match="合计不能超过"):
        center.record_performance(
            job_id,
            {
                "platform": "youtube",
                "views": 100,
                "source_visual_ratio": 80,
                "local_visual_ratio": 30,
            },
        )


def test_performance_rejects_platform_without_published_post(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1notpublished", ["youtube"]
    )
    with pytest.raises(ValueError, match="尚无已发布"):
        center.record_performance(job_id, {"platform": "youtube", "views": 1})


def test_fetch_youtube_video_statistics_batches_and_normalizes_counts():
    calls = []

    class FakeRequest:
        def __init__(self, ids):
            self.ids = ids

        def execute(self):
            return {
                "items": [
                    {
                        "id": video_id,
                        "statistics": {
                            "viewCount": "100",
                            "likeCount": "8",
                            "commentCount": "3",
                        },
                    }
                    for video_id in self.ids
                ]
            }

    class FakeVideos:
        def list(self, **kwargs):
            assert kwargs["part"] == "statistics"
            ids = kwargs["id"].split(",")
            calls.append(ids)
            return FakeRequest(ids)

    class FakeService:
        def videos(self):
            return FakeVideos()

    video_ids = [f"video-{index}" for index in range(51)]
    result = transfer_module.fetch_youtube_video_statistics(
        video_ids, service=FakeService()
    )

    assert len(calls) == 2
    assert len(calls[0]) == 50
    assert result["video-50"] == {"views": 100, "likes": 8, "comments": 3}


def test_fetch_bilibili_video_statistics_normalizes_public_counters():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "stat": {"view": 900, "like": 70, "reply": 12, "share": 9}
                },
            }

    class FakeSession:
        def get(self, url, **kwargs):
            assert url.endswith("/x/web-interface/view")
            assert kwargs["params"] == {"bvid": "BV1xx411c7mD"}
            return FakeResponse()

    result = transfer_module.fetch_bilibili_video_statistics(
        "BV1xx411c7mD", session=FakeSession()
    )

    assert result == {"views": 900, "likes": 70, "comments": 12, "shares": 9}


def test_sync_performance_preserves_manual_metrics_and_skips_unchanged(center):
    job_id = center.add_manual_job(
        "https://www.douyin.com/video/1234567890123456789",
        ["youtube", "bilibili"],
    )
    center._update_job(
        job_id,
        youtube_video_id="youtube-sync-1",
        bilibili_post_id="BV1xx411c7mD",
        status="completed",
    )
    center.record_performance(
        job_id,
        {
            "platform": "youtube",
            "views": 10,
            "shares": 7,
            "completion_rate": 42,
            "revenue_cny": 12.5,
            "production_cost_cny": 1.5,
            "note": "24小时手工数据",
        },
    )

    def youtube_fetcher(video_ids):
        assert video_ids == ["youtube-sync-1"]
        return {
            "youtube-sync-1": {"views": 120, "likes": 8, "comments": 3}
        }

    def bilibili_fetcher(bvid):
        assert bvid == "BV1xx411c7mD"
        return {"views": 900, "likes": 70, "comments": 12, "shares": 9}

    first = center.sync_performance_metrics(
        youtube_fetcher=youtube_fetcher,
        bilibili_fetcher=bilibili_fetcher,
    )
    youtube_snapshot = center._latest_performance_snapshot(job_id, "youtube")

    assert first["synced"] == 2
    assert first["failed"] == 0
    assert youtube_snapshot["views"] == 120
    assert youtube_snapshot["shares"] == 7
    assert youtube_snapshot["completion_rate"] == 42
    assert youtube_snapshot["revenue_cny"] == 12.5
    assert youtube_snapshot["production_cost_cny"] == 1.5
    assert "24小时手工数据" in youtube_snapshot["note"]

    second = center.sync_performance_metrics(
        youtube_fetcher=youtube_fetcher,
        bilibili_fetcher=bilibili_fetcher,
    )

    assert second["synced"] == 0
    assert second["unchanged"] == 2


def test_scheduled_checkpoint_records_unchanged_counters_as_real_sample(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1samplestable", ["youtube"]
    )
    center._update_job(
        job_id,
        youtube_video_id="youtube-stable-1",
        status="completed",
    )
    center.record_performance(
        job_id,
        {"platform": "youtube", "views": 100, "likes": 8, "comments": 2},
    )

    result = center.sync_performance_metrics(
        youtube_fetcher=lambda _ids: {
            "youtube-stable-1": {"views": 100, "likes": 8, "comments": 2}
        },
        bilibili_fetcher=lambda _bvid: {},
        job_platforms={(job_id, "youtube")},
        sample_contexts={(job_id, "youtube"): {"checkpoint_hours": 24}},
    )

    with center._connect() as conn:
        rows = conn.execute(
            """
            SELECT checkpoint_hours, sync_source FROM transfer_metrics
            WHERE job_id=? ORDER BY recorded_at, rowid
            """,
            (job_id,),
        ).fetchall()
    assert result["synced"] == 1
    assert [dict(row) for row in rows] == [
        {"checkpoint_hours": 0, "sync_source": "manual"},
        {"checkpoint_hours": 24, "sync_source": "scheduled"},
    ]


def test_sync_performance_marks_platforms_without_data_permission_manual(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1xx411c7mD", ["douyin"]
    )
    center._update_job(
        job_id,
        douyin_post_id="douyin-item-id",
        status="completed",
    )

    result = center.sync_performance_metrics(
        youtube_fetcher=lambda _ids: {},
        bilibili_fetcher=lambda _bvid: {},
    )

    assert result["manual"] == 1
    assert result["manual_platforms"] == [
        {
            "platform": "douyin",
            "count": 1,
            "reason": "当前只有发布授权，数据权限需平台另行审核",
        }
    ]


def test_performance_checkpoints_are_persistent_and_idempotent(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1checkpoint1", ["youtube"]
    )
    center._update_job(
        job_id,
        youtube_video_id="youtube-checkpoint-1",
        status="completed",
    )
    published_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="seconds")
    job = center.get_job(job_id)

    first = center._register_performance_checkpoints(
        job, "youtube", published_at=published_at
    )
    second = center._register_performance_checkpoints(
        job, "youtube", published_at=published_at
    )

    with center._connect() as conn:
        rows = conn.execute(
            """
            SELECT checkpoint_hours, status FROM transfer_metric_checkpoints
            WHERE job_id=? AND platform='youtube' ORDER BY checkpoint_hours
            """,
            (job_id,),
        ).fetchall()
    assert first == 3
    assert second == 0
    assert [row["checkpoint_hours"] for row in rows] == [24, 72, 168]
    assert {row["status"] for row in rows} == {"pending"}


def test_performance_checkpoints_supersede_pending_old_post(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1checkpoint2", ["youtube"]
    )
    center._update_job(job_id, youtube_video_id="youtube-old", status="completed")
    center._register_performance_checkpoints(center.get_job(job_id), "youtube")
    center._update_job(job_id, youtube_video_id="youtube-new")

    inserted = center._register_performance_checkpoints(
        center.get_job(job_id), "youtube"
    )

    with center._connect() as conn:
        rows = conn.execute(
            """
            SELECT post_id, status, COUNT(*) AS total
            FROM transfer_metric_checkpoints WHERE job_id=?
            GROUP BY post_id, status ORDER BY post_id
            """,
            (job_id,),
        ).fetchall()
    assert inserted == 3
    assert [dict(row) for row in rows] == [
        {"post_id": "youtube-new", "status": "pending", "total": 3},
        {"post_id": "youtube-old", "status": "superseded", "total": 3},
    ]


def test_due_performance_sync_samples_latest_and_marks_older_windows_missed(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1autosync001", ["youtube"]
    )
    center._update_job(
        job_id,
        youtube_video_id="youtube-auto-sync-1",
        status="completed",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(
        timespec="seconds"
    )
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET updated_at=? WHERE id=?", (old, job_id)
        )
    fetch_calls = []

    def youtube_fetcher(video_ids):
        fetch_calls.append(video_ids)
        return {
            "youtube-auto-sync-1": {"views": 500, "likes": 20, "comments": 4}
        }

    first = center.sync_due_performance_metrics(
        youtube_fetcher=youtube_fetcher,
        bilibili_fetcher=lambda _bvid: {},
    )
    restarted = transfer_module.TransferCenter(config_provider=lambda: {})
    second = restarted.sync_due_performance_metrics(
        youtube_fetcher=lambda _ids: pytest.fail("已完成检查点不应重复同步"),
        bilibili_fetcher=lambda _bvid: {},
    )

    assert fetch_calls == [["youtube-auto-sync-1"]]
    assert first["due_checkpoints"] == 3
    assert first["completed"] == 1
    assert first["missed"] == 2
    assert first["synced"] == 1
    assert second["due_checkpoints"] == 0
    with center._connect() as conn:
        rows = conn.execute(
            """
            SELECT status, attempt_count FROM transfer_metric_checkpoints
            WHERE job_id=? ORDER BY checkpoint_hours
            """,
            (job_id,),
        ).fetchall()
        metric = conn.execute(
            """
            SELECT checkpoint_hours, sync_source FROM transfer_metrics
            WHERE job_id=? ORDER BY recorded_at DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    assert len(rows) == 3
    assert [row["status"] for row in rows] == ["missed", "missed", "complete"]
    assert [row["attempt_count"] for row in rows] == [0, 0, 1]
    assert dict(metric) == {"checkpoint_hours": 168, "sync_source": "scheduled"}


def test_due_performance_sync_failure_uses_six_hour_retry_cooldown(center):
    job_id = center.add_manual_job(
        "https://www.douyin.com/video/1234567890123456001", ["bilibili"]
    )
    center._update_job(
        job_id,
        bilibili_post_id="BV1xx411c7mD",
        status="completed",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(
        timespec="seconds"
    )
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET updated_at=? WHERE id=?", (old, job_id)
        )

    first = center.sync_due_performance_metrics(
        youtube_fetcher=lambda _ids: {},
        bilibili_fetcher=lambda _bvid: (_ for _ in ()).throw(
            RuntimeError("temporary unavailable")
        ),
    )
    second = center.sync_due_performance_metrics(
        youtube_fetcher=lambda _ids: {},
        bilibili_fetcher=lambda _bvid: pytest.fail("冷却期内不应重试"),
    )

    assert first["due_checkpoints"] == 3
    assert first["failed"] == 1
    assert first["missed"] == 2
    assert second["due_checkpoints"] == 0
    status = center.get_performance_sync_status()
    assert status["retry"] == 1
    assert status["pending"] == 1
    assert status["missed"] == 2


def test_due_performance_sync_respects_disabled_config(center):
    center._config_provider = lambda: {
        "TRANSFER_PERFORMANCE_AUTO_SYNC_ENABLED": False
    }

    result = center.sync_due_performance_metrics(
        youtube_fetcher=lambda _ids: pytest.fail("关闭后不应请求平台"),
        bilibili_fetcher=lambda _bvid: pytest.fail("关闭后不应请求平台"),
    )

    assert result == {
        "enabled": False,
        "due_checkpoints": 0,
        "completed": 0,
        "failed": 0,
    }


def test_growth_analysis_feeds_timed_samples_back_into_topic_strategy(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1growthloop1", ["youtube"]
    )
    center._update_job(
        job_id,
        title="AI 工具实测",
        youtube_video_id="youtube-growth-1",
        status="completed",
    )
    for checkpoint_hours, views, likes, revenue in (
        (24, 100, 10, 0),
        (72, 300, 25, 0),
        (168, 450, 40, 30),
    ):
        center.record_performance(
            job_id,
            {
                "platform": "youtube",
                "views": views,
                "likes": likes,
                "comments": 2,
                "shares": 1,
                "revenue_cny": revenue,
                "production_cost_cny": 5,
                "checkpoint_hours": checkpoint_hours,
                "sync_source": "scheduled",
            },
        )

    growth = center.get_performance_growth(days=30)
    summary = center.get_performance_summary(days=7)

    assert growth["checkpoint_samples"] == 3
    assert growth["paired_growth"] == 2
    assert growth["rising"] == 2
    assert growth["profitable_7d"] == 1
    assert growth["repeat_candidates"][0]["title"] == "AI 工具实测"
    assert growth["repeat_candidates"][0]["growth_24h_72h"] == 200.0
    assert "AI 工具实测" in summary["strategy"]["topic_guidance"]
    assert any("增长对照" in item for item in summary["suggestions"])


def test_growth_followup_candidate_stays_in_pool_until_human_promotes(center):
    parent_job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1followup001", ["youtube"]
    )
    center._update_job(
        parent_job_id,
        title="AI 剪辑工具实测",
        duration=95,
        youtube_video_id="youtube-followup-1",
        status="completed",
    )
    for checkpoint_hours, views, likes in (
        (24, 200, 20),
        (72, 500, 42),
    ):
        center.record_performance(
            parent_job_id,
            {
                "platform": "youtube",
                "views": views,
                "likes": likes,
                "comments": 5,
                "shares": 2,
                "checkpoint_hours": checkpoint_hours,
                "sync_source": "scheduled",
            },
        )

    first = center.generate_growth_followup_candidates()
    second = center.generate_growth_followup_candidates()
    candidates = center.list_hot_candidates()

    assert first["created"] == 1
    assert second["updated"] == 1
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["metrics"]["candidate_type"] == "growth_followup"
    assert candidate["metrics"]["recommended_target_platform"] == "youtube"
    assert candidate["metrics"]["suggested_duration"] == "45-60 秒"
    assert candidate["job_id"] == ""

    job_id = center.promote_hot_candidate(candidate["id"], ["youtube"])
    job = center.get_job(job_id)

    assert job_id != parent_job_id
    assert job["status"] == "discovered"
    assert job["local_video_path"] == ""
    assert job["recreation_status"] == "draft"
    assert job["progress_percent"] == 12
    plan = json.loads(job["recreation_plan_json"])
    assert plan["generated_by"] == "growth_followup_local_draft"
    assert len(plan["draft_storyboard"]) == 5
    assert len(plan["material_checklist"]) == 5
    assert len(job["commentary_script"]) >= 100
    assert "换一个场景" in job["original_angle"]
    assert "建议时长：45-60 秒" in job["original_contribution"]
    assert center.list_hot_candidates() == []

    after_promote = center.generate_growth_followup_candidates()
    assert after_promote["skipped"] == 1
    assert center.list_hot_candidates() == []


def test_growth_followup_draft_can_be_edited_before_downloading(center):
    parent_job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1draftedit01", ["youtube"]
    )
    center._update_job(
        parent_job_id,
        title="续作草稿测试",
        youtube_video_id="youtube-draft-edit-1",
        status="completed",
    )
    center.record_performance(
        parent_job_id,
        {
            "platform": "youtube",
            "views": 200,
            "likes": 20,
            "comments": 4,
            "checkpoint_hours": 24,
            "sync_source": "scheduled",
        },
    )
    center.generate_growth_followup_candidates()
    candidate = center.list_hot_candidates()[0]
    job_id = center.promote_hot_candidate(candidate["id"], ["youtube"])

    result = center.save_recreation_review(
        job_id,
        {
            "source_attribution": "参考来源\nhttps://www.bilibili.com/video/BV1draftedit01",
            "processing_mode": "professional",
            "recreation_mode": "commentary",
            "original_angle": "改成本地场景对比",
            "original_contribution": "尚未完成",
            "commentary_script": "这是人工修改后的口播草稿。",
            "storyboard_text": "开场｜先给结果｜对比画面｜实拍\n结尾｜给出建议｜结论卡｜信息卡",
            "material_checklist_text": "- [ ] 实拍\n- [ ] 信息卡",
            "material_ready": ["实拍"],
        },
        approve=False,
    )
    plan = json.loads(result["recreation_plan_json"])

    assert result["status"] == "discovered"
    assert result["local_video_path"] == ""
    assert result["recreation_status"] == "draft"
    assert result["watermark_status"] == "unreviewed"
    assert result["commentary_script"] == "这是人工修改后的口播草稿。"
    assert len(plan["draft_storyboard"]) == 2
    assert plan["material_checklist"] == ["实拍", "信息卡"]
    assert plan["material_readiness"] == {"实拍": True, "信息卡": False}

    regenerated = center._preserve_growth_concept_draft(
        plan,
        {
            "generated_by": "safe_fallback",
            "commentary_script": "下载后重新生成的文本",
            "segment_plan": [
                {
                    "stage": "精确时间线",
                    "source_start": 3,
                    "source_end": 8,
                    "duration": 5,
                }
            ],
        },
    )

    assert regenerated["commentary_script"] == "这是人工修改后的口播草稿。"
    assert regenerated["segment_plan"][0]["source_start"] == 3
    assert regenerated["draft_stage"] == "source_ready"
    assert regenerated["concept_source"] == "growth_followup"
    assert regenerated["material_readiness"] == {"实拍": True, "信息卡": False}
    assert regenerated["material_gate_enabled"] is True


def test_material_readiness_blocks_production_until_every_item_is_ready(
    center, tmp_path
):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1materialgate", ["youtube"]
    )
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    blocked_plan = {
        "material_checklist": ["实拍", "信息卡"],
        "material_readiness": {"实拍": True, "信息卡": False},
        "material_gate_enabled": True,
    }
    center._update_job(
        job_id,
        local_video_path=str(video_path),
        original_video_path=str(video_path),
        recreation_plan_json=json.dumps(blocked_plan, ensure_ascii=False),
        mpt_project_id="project-ready",
        mpt_asset_id="asset-ready",
    )

    with pytest.raises(ValueError, match="素材准备尚未完成（1/2）"):
        center.send_to_money_printer(job_id)
    with pytest.raises(ValueError, match="素材准备尚未完成（1/2）"):
        center.recreate_with_money_printer_async(job_id)

    blocked_plan["material_readiness"]["信息卡"] = True
    center._update_job(
        job_id,
        recreation_plan_json=json.dumps(blocked_plan, ensure_ascii=False),
    )
    result = center.send_to_money_printer(job_id)

    assert result["mpt_project_id"] == "project-ready"
    assert result["mpt_asset_id"] == "asset-ready"


def test_material_bindings_auto_verify_and_unbind_without_deleting_file(
    center, tmp_path
):
    class Upload:
        filename = "新实拍.mp4"

        @staticmethod
        def save(path):
            with open(path, "wb") as handle:
                handle.write(b"material-video")

    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1materialbind", ["youtube"]
    )
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    plan = {
        "material_checklist": ["新实拍", "信息卡"],
        "material_readiness": {"新实拍": False, "信息卡": False},
        "material_bindings": {},
        "material_gate_enabled": True,
    }
    center._update_job(
        job_id,
        local_video_path=str(video_path),
        original_video_path=str(video_path),
        recreation_plan_json=json.dumps(plan, ensure_ascii=False),
        mpt_project_id="project-bound",
        mpt_asset_id="asset-bound",
    )
    item_keys = {
        item["label"]: item["key"]
        for item in transfer_module.material_readiness_summary(plan)["items"]
    }

    first_result = center.bind_material_assets(
        job_id,
        uploads={item_keys["新实拍"]: Upload()},
    )
    first_summary = center.get_material_readiness(job_id)
    saved_plan = json.loads(center.get_job(job_id)["recreation_plan_json"])
    uploaded_path = saved_plan["material_bindings"]["新实拍"]["path"]

    assert first_result == {"attached": 1, "unbound": 0}
    assert first_summary["ready"] == 1
    assert os.path.isfile(uploaded_path)
    assert os.stat(uploaded_path).st_mode & 0o777 == 0o600

    second_result = center.bind_material_assets(
        job_id,
        urls={item_keys["信息卡"]: "https://example.com/reference-card"},
    )
    ready_summary = center.get_material_readiness(job_id)
    package_path, manifest = center.export_material_package(job_id)
    production_job = center.send_to_money_printer(job_id)

    assert second_result == {"attached": 1, "unbound": 0}
    assert ready_summary["all_ready"] is True
    assert manifest["readiness"]["all_ready"] is True
    assert [item["source_type"] for item in manifest["items"]] == ["file", "url"]
    with zipfile.ZipFile(package_path) as archive:
        names = archive.namelist()
        stored_manifest = json.loads(archive.read("manifest.json"))
        assert "manifest.json" in names
        assert any(name.endswith("-新实拍.mp4") for name in names)
        assert stored_manifest["items"][1]["reference_url"] == (
            "https://example.com/reference-card"
        )
    assert production_job["mpt_project_id"] == "project-bound"

    unbind_result = center.bind_material_assets(
        job_id,
        removals={item_keys["新实拍"]},
    )
    after_unbind = center.get_material_readiness(job_id)

    assert unbind_result == {"attached": 0, "unbound": 1}
    assert after_unbind["blocking"] is True
    assert os.path.isfile(uploaded_path)
    with pytest.raises(ValueError, match="素材准备尚未完成（1/2）"):
        center.send_to_money_printer(job_id)


def test_material_binding_rejects_unsupported_file_and_private_url(center):
    class Upload:
        filename = "payload.exe"

        @staticmethod
        def save(path):
            raise AssertionError("不支持的扩展名不应写入磁盘")

    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1materialinvalid", ["youtube"]
    )
    plan = {
        "material_checklist": ["新实拍"],
        "material_readiness": {"新实拍": False},
        "material_bindings": {},
        "material_gate_enabled": True,
    }
    center._update_job(
        job_id,
        recreation_plan_json=json.dumps(plan, ensure_ascii=False),
    )
    item_key = transfer_module.material_readiness_summary(plan)["items"][0]["key"]

    with pytest.raises(ValueError, match="文件格式不支持"):
        center.bind_material_assets(job_id, uploads={item_key: Upload()})
    with pytest.raises(ValueError, match="不允许下载本机或内网地址"):
        center.bind_material_assets(
            job_id,
            urls={item_key: "http://127.0.0.1/private-material"},
        )


def test_growth_followup_candidate_requires_real_timed_sample(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1manualonly01", ["youtube"]
    )
    center._update_job(job_id, youtube_video_id="youtube-manual-only")
    center.record_performance(
        job_id,
        {"platform": "youtube", "views": 1000, "likes": 100},
    )

    result = center.generate_growth_followup_candidates()

    assert result["available"] == 0
    assert center.list_hot_candidates() == []


def test_24_hour_scheduler_creates_followup_candidate_without_starting_job(center):
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1autoidea001", ["youtube"]
    )
    center._update_job(
        job_id,
        title="自动续作测试",
        youtube_video_id="youtube-auto-idea-1",
        status="completed",
    )
    published_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(
        timespec="seconds"
    )
    with center._connect() as conn:
        conn.execute(
            "UPDATE transfer_jobs SET updated_at=? WHERE id=?",
            (published_at, job_id),
        )

    result = center.sync_due_performance_metrics(
        youtube_fetcher=lambda _ids: {
            "youtube-auto-idea-1": {"views": 300, "likes": 30, "comments": 6}
        },
        bilibili_fetcher=lambda _bvid: {},
    )
    candidates = center.list_hot_candidates()

    assert result["completed"] == 1
    assert result["growth_candidates"] == 1
    assert len(candidates) == 1
    assert candidates[0]["metrics"]["candidate_type"] == "growth_followup"
    assert candidates[0]["metrics"]["growth_24h_72h"] is None
    assert candidates[0]["job_id"] == ""
    assert center.get_job(job_id)["status"] == "completed"


def test_metric_schema_migration_adds_business_loop_columns(tmp_path):
    database = sqlite3.connect(tmp_path / "legacy.db")
    database.row_factory = sqlite3.Row
    database.executescript(
        """
        CREATE TABLE transfer_rules (id TEXT PRIMARY KEY);
        CREATE TABLE transfer_jobs (
            id TEXT PRIMARY KEY,
            local_video_path TEXT DEFAULT '',
            recreation_completed INTEGER DEFAULT 0,
            source_uploader TEXT DEFAULT '',
            source_url TEXT DEFAULT '',
            progress_message TEXT DEFAULT ''
        );
        CREATE TABLE transfer_metrics (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            platform TEXT,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            followers_delta INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            recorded_at TEXT
        );
        """
    )

    transfer_module.TransferCenter._migrate_schema(database)
    columns = {
        row["name"]
        for row in database.execute("PRAGMA table_info(transfer_metrics)").fetchall()
    }
    database.close()

    assert {
        "impressions",
        "completion_rate",
        "retention_3s",
        "revenue_cny",
        "production_cost_cny",
        "local_visual_ratio",
        "monetization_status",
        "checkpoint_hours",
        "sync_source",
    }.issubset(columns)


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
