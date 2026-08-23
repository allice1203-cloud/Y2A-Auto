import os
import time
from urllib.parse import parse_qs, urlparse

import pytest

from modules import douyin_openapi


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        douyin_openapi,
        "get_app_subdir",
        lambda name: str(tmp_path / name),
    )
    return tmp_path


def token_payload(scope="video.create.bind"):
    return {
        "data": {
            "error_code": 0,
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "open_id": "open-id",
            "scope": scope,
            "expires_in": 1296000,
            "refresh_expires_in": 2592000,
        },
        "message": "success",
    }


def configure_and_authorize(private_config, http=None):
    douyin_openapi.save_douyin_app_credentials("client-key", "client-secret")
    return douyin_openapi.exchange_douyin_code(
        "authorization-code",
        http=http or FakeHttp([token_payload()]),
    )


def test_redirect_uri_is_stable_https():
    assert douyin_openapi.build_douyin_oauth_redirect_uri(
        "https://transfer.sg99.online"
    ) == "https://transfer.sg99.online/transfer-center/douyin/callback"
    assert douyin_openapi.build_douyin_oauth_redirect_uri(
        request_host="transfer.sg99.online",
        request_scheme="http",
    ).startswith("https://")


def test_credentials_are_private_and_blank_secret_is_preserved(private_config):
    douyin_openapi.save_douyin_app_credentials("first-key", "first-secret")
    douyin_openapi.save_douyin_app_credentials("second-key", "")
    credentials = douyin_openapi.load_douyin_app_credentials()
    assert credentials == {
        "client_key": "second-key",
        "client_secret": "first-secret",
    }
    path = private_config / "config" / "douyin_openapi_app.json"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_authorization_url_uses_publish_scope(private_config):
    douyin_openapi.save_douyin_app_credentials("client-key", "client-secret")
    url = douyin_openapi.build_douyin_authorization_url(
        "https://transfer.sg99.online/transfer-center/douyin/callback",
        "csrf-state",
    )
    query = parse_qs(urlparse(url).query)
    assert query == {
        "client_key": ["client-key"],
        "response_type": ["code"],
        "scope": ["video.create.bind"],
        "redirect_uri": [
            "https://transfer.sg99.online/transfer-center/douyin/callback"
        ],
        "state": ["csrf-state"],
    }


def test_exchange_code_saves_server_side_token(private_config):
    douyin_openapi.save_douyin_app_credentials("client-key", "client-secret")
    http = FakeHttp([token_payload()])
    token = douyin_openapi.exchange_douyin_code("code", http=http)
    assert token["open_id"] == "open-id"
    assert douyin_openapi.douyin_connection_state()["connected"] is True
    token_path = private_config / "config" / "douyin_openapi_token.json"
    assert os.stat(token_path).st_mode & 0o777 == 0o600
    assert http.calls[0][1]["data"]["grant_type"] == "authorization_code"


def test_expired_access_token_is_refreshed(private_config):
    configure_and_authorize(private_config)
    _, token_path = douyin_openapi._paths()
    token = douyin_openapi.load_douyin_token()
    token["expires_at"] = time.time() - 1
    douyin_openapi._write_private_json(token_path, token)
    refreshed = token_payload()["data"]
    refreshed["access_token"] = "new-access-token"
    http = FakeHttp([{"data": refreshed, "message": "success"}])
    result = douyin_openapi.get_valid_douyin_token(http=http)
    assert result["access_token"] == "new-access-token"
    assert http.calls[0][0] == douyin_openapi.REFRESH_TOKEN_URL


def test_direct_upload_then_create_video(private_config, tmp_path):
    configure_and_authorize(private_config)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video-data")
    http = FakeHttp(
        [
            {
                "data": {
                    "error_code": 0,
                    "video": {"video_id": "encrypted-video-id"},
                },
                "extra": {"error_code": 0},
            },
            {
                "data": {
                    "error_code": 0,
                    "item_id": "item-id",
                    "video_id": "public-video-id",
                },
                "extra": {"error_code": 0},
            },
        ]
    )
    result = douyin_openapi.publish_douyin_video(
        str(video_path), "测试标题", http=http
    )
    assert result == {"item_id": "item-id", "video_id": "public-video-id"}
    assert [call[0] for call in http.calls] == [
        douyin_openapi.UPLOAD_URL,
        douyin_openapi.CREATE_URL,
    ]
    assert http.calls[1][1]["json"]["video_id"] == "encrypted-video-id"
    assert http.calls[1][1]["params"]["open_id"] == "open-id"


def test_large_video_uses_part_upload(private_config, tmp_path, monkeypatch):
    configure_and_authorize(private_config)
    monkeypatch.setattr(douyin_openapi, "DIRECT_UPLOAD_LIMIT", 4)
    monkeypatch.setattr(douyin_openapi, "PART_SIZE", 5)
    video_path = tmp_path / "large.mp4"
    video_path.write_bytes(b"0123456789")
    http = FakeHttp(
        [
            {"data": {"error_code": 0, "upload_id": "upload-id"}},
            {"data": {"error_code": 0}},
            {"data": {"error_code": 0}},
            {
                "data": {
                    "error_code": 0,
                    "video": {"video_id": "encrypted-id"},
                }
            },
            {
                "data": {
                    "error_code": 0,
                    "item_id": "item-id",
                    "video_id": "video-id",
                }
            },
        ]
    )
    result = douyin_openapi.publish_douyin_video(str(video_path), "标题", http=http)
    assert result["item_id"] == "item-id"
    assert [call[0] for call in http.calls] == [
        douyin_openapi.PART_INIT_URL,
        douyin_openapi.PART_UPLOAD_URL,
        douyin_openapi.PART_UPLOAD_URL,
        douyin_openapi.PART_COMPLETE_URL,
        douyin_openapi.CREATE_URL,
    ]
    assert http.calls[2][1]["params"]["part_number"] == 2


def test_missing_publish_capability_has_actionable_error(private_config, tmp_path):
    configure_and_authorize(private_config)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    http = FakeHttp(
        [
            {
                "data": {
                    "error_code": 28001018,
                    "description": "应用未获得该能力",
                }
            }
        ]
    )
    with pytest.raises(douyin_openapi.DouyinOpenApiError) as exc_info:
        douyin_openapi.publish_douyin_video(str(video_path), "标题", http=http)
    assert "代替用户发布内容到抖音" in str(exc_info.value)
