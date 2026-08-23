import json
from pathlib import Path

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


def test_candidate_pool_promotes_and_dismisses_without_auto_download(center):
    promoted_id = center._upsert_candidate(
        {
            "platform": "bilibili",
            "source_id": "BV1hotcandidate",
            "source_url": "https://www.bilibili.com/video/BV1hotcandidate",
            "title": "公开热榜候选",
            "heat_score": 120000,
            "metrics": {"views": 120000},
        }
    )
    dismissed_id = center._upsert_candidate(
        {
            "platform": "douyin",
            "source_id": "1234567890123456789",
            "source_url": "https://www.douyin.com/video/1234567890123456789",
            "title": "待忽略候选",
        }
    )

    job_id = center.promote_hot_candidate(promoted_id, ["youtube"])
    assert center.get_job(job_id)["status"] == "discovered"
    assert center.dismiss_hot_candidate(dismissed_id) is True
    assert center.list_hot_candidates() == []


def test_candidate_refresh_keeps_partial_success(center, monkeypatch):
    monkeypatch.setattr(
        center,
        "_fetch_bilibili_hot_candidates",
        lambda limit: [
            {
                "platform": "bilibili",
                "source_id": "BV1partial",
                "source_url": "https://www.bilibili.com/video/BV1partial",
                "title": "部分成功",
            }
        ],
    )
    monkeypatch.setattr(
        center,
        "_fetch_douyin_hot_candidates",
        lambda limit: (_ for _ in ()).throw(RuntimeError("暂时不可用")),
    )

    result = center.refresh_hot_candidates("all", 10)

    assert result["success"] is True
    assert result["refreshed"] == 1
    assert "抖音" in result["errors"][0]


def test_bilibili_hot_candidates_fall_back_to_popular_feed(center, monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("ranking/v2"):
                return FakeResponse({"code": -352, "message": "risk control"})
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "bvid": "BV1fallback",
                                "title": "备用热门流",
                                "owner": {"name": "作者"},
                                "stat": {"view": 88, "like": 9},
                            }
                        ]
                    },
                }
            )

    session = FakeSession()
    monkeypatch.setattr(center, "_requests_session", lambda platform: session)

    items = center._fetch_bilibili_hot_candidates(5)

    assert items[0]["source_id"] == "BV1fallback"
    assert items[0]["heat_score"] == 88
    assert session.calls[-1].endswith("popular")


def _build_authorized_archive(center, tmp_path):
    account_url = "https://space.bilibili.com/123456"
    source_id = center.save_allowed_source(
        {
            "platform": "bilibili",
            "account_url": account_url,
            "display_name": "本人账号",
            "rights_basis": "owned",
            "rights_note": "本人运营账号的本地归档",
            "enabled": True,
        }
    )
    rule_id = center.save_rule(
        {
            "platform": "bilibili",
            "discovery_mode": "account",
            "source_value": account_url,
            "target_platforms": ["youtube"],
            "auto_prepare": False,
        }
    )
    job_id, created = center._insert_discovered_job(
        center.get_rule(rule_id),
        {
            "id": "BV1archive",
            "url": "https://www.bilibili.com/video/BV1archive",
            "title": "账号历史视频",
            "uploader": "本人账号",
        },
    )
    assert created is True
    media_dir = tmp_path / "media" / job_id
    media_dir.mkdir(parents=True)
    video = media_dir / "video.mp4"
    video.write_bytes(b"video")
    (media_dir / "video.zh-CN.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第一句\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二句\n",
        encoding="utf-8",
    )
    center._update_job(job_id, status="review", local_video_path=str(video))
    return source_id, job_id


def test_authorized_archive_builds_resumable_manifest_and_markdown(
    center, tmp_path
):
    source_id, job_id = _build_authorized_archive(center, tmp_path)

    manifest = center.build_archive_manifest(source_id, persist=True)

    assert manifest["total_items"] == 1
    assert manifest["video_ready"] == 1
    assert manifest["transcript_ready"] == 1
    transcript = Path(manifest["items"][0]["transcript_path"])
    assert transcript.is_file()
    assert "第一句" in transcript.read_text(encoding="utf-8")
    assert Path(manifest["manifest_path"]).is_file()
    assert json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))["items"][0]["job_id"] == job_id


def test_archive_resume_does_not_claim_transcription_when_asr_is_disabled(
    center, tmp_path
):
    source_id, job_id = _build_authorized_archive(center, tmp_path)
    transcript = Path(center._ensure_archive_markdown(job_id))
    transcript.unlink()
    for subtitle in transcript.parent.glob("*.srt"):
        subtitle.unlink()

    result = center.resume_archive(source_id)

    assert result["transcriptions_started"] == 0
    assert result["remaining"] == 1
