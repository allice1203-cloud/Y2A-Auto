import json

import pytest

import modules.transfer_center as transfer_module
from modules.douyin_downloader import (
    DouyinDownloadError,
    download_douyin_video,
)


class FakeResponse:
    def __init__(self, url, *, status=200, headers=None, body=b""):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self.content = body
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.content

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        return response


def _router_page():
    payload = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "item_list": [
                        {
                            "desc": "示例视频",
                            "author": {"nickname": "授权作者"},
                            "video": {"play_addr": {"uri": "v0200fg10000example"}},
                        }
                    ]
                }
            }
        }
    }
    return f"<script>window._ROUTER_DATA = {json.dumps(payload)}</script>".encode()


def test_downloads_douyin_video_atomically(tmp_path):
    source = "https://www.douyin.com/video/1234567890123456789"
    page_url = "https://www.iesdouyin.com/share/video/1234567890123456789/"
    session = FakeSession(
        [
            FakeResponse(page_url, body=_router_page()),
            FakeResponse(
                "https://v3.douyinvod.com/example",
                headers={"content-type": "video/mp4", "content-length": "20"},
                body=b"\x00\x00\x00\x18ftypisomvideo-data",
            ),
        ]
    )
    output = tmp_path / "video.mp4"

    metadata = download_douyin_video(source, output, session=session)

    assert output.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert not (tmp_path / "video.mp4.part").exists()
    assert metadata["title"] == "示例视频"
    assert metadata["uploader"] == "授权作者"
    assert metadata["extractor"] == "douyin_public_fallback"
    assert session.calls[1][1]["stream"] is True
    assert session.calls[1][1]["allow_redirects"] is False


def test_rejects_non_douyin_source_without_network(tmp_path):
    session = FakeSession([])

    with pytest.raises(DouyinDownloadError, match="允许的抖音域名"):
        download_douyin_video(
            "https://example.com/video/1234567890123456789",
            tmp_path / "video.mp4",
            session=session,
        )

    assert session.calls == []


def test_rejects_redirect_outside_douyin(tmp_path):
    session = FakeSession(
        [
            FakeResponse(
                "https://v.douyin.com/example/",
                status=302,
                headers={"location": "http://127.0.0.1/private"},
            )
        ]
    )

    with pytest.raises(DouyinDownloadError, match="重定向地址"):
        download_douyin_video(
            "https://v.douyin.com/example/",
            tmp_path / "video.mp4",
            session=session,
        )


def test_removes_partial_file_when_video_exceeds_limit(tmp_path):
    source = "https://www.douyin.com/video/1234567890123456789"
    session = FakeSession(
        [
            FakeResponse(
                "https://www.iesdouyin.com/share/video/1234567890123456789/",
                body=_router_page(),
            ),
            FakeResponse(
                "https://v3.douyinvod.com/example",
                headers={"content-type": "video/mp4"},
                body=b"\x00\x00\x00\x18ftypisomvideo-data",
            ),
        ]
    )
    output = tmp_path / "video.mp4"

    with pytest.raises(DouyinDownloadError, match="大小限制"):
        download_douyin_video(source, output, session=session, max_bytes=8)

    assert not output.exists()
    assert not (tmp_path / "video.mp4.part").exists()


def test_transfer_center_uses_fallback_only_after_ytdlp_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transfer_module,
        "get_app_subdir",
        lambda name: str(tmp_path / name) if name else str(tmp_path),
    )
    center = transfer_module.TransferCenter(config_provider=lambda: {})
    job_id = center.add_manual_job(
        "https://www.douyin.com/video/1234567890123456789",
        ["youtube"],
    )

    class FailedProcess:
        stdout = iter(["ERROR: extractor unavailable\n"])

        @staticmethod
        def wait(timeout):
            del timeout
            return 1

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(transfer_module.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())

    def fake_fallback(source_url, output_path, **kwargs):
        del source_url, kwargs
        output_path.write_bytes(b"video")
        return {"title": "备用解析标题", "uploader": "授权作者"}

    monkeypatch.setattr(transfer_module, "download_douyin_video", fake_fallback)
    monkeypatch.setattr(
        transfer_module,
        "prepare_platform_variants",
        lambda path, output_dir, targets: (
            {"path": path, "width": 1080, "height": 1920},
            {target: {"status": "ready", "path": path} for target in targets},
        ),
    )
    monkeypatch.setattr(transfer_module, "build_distribution_plan", lambda *_: {})
    monkeypatch.setattr(
        transfer_module,
        "generate_recreation_plan",
        lambda *_args, **_kwargs: {"original_angle": "备用解析测试"},
    )

    job = center.prepare_job(job_id)

    assert job["status"] == "review"
    assert job["title"] == "备用解析标题"
    assert job["source_uploader"] == "授权作者"
    assert job["local_video_path"].endswith("video.mp4")
