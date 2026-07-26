import os

import pytest

from modules.source_login import (
    build_netscape_cookie_text,
    create_login_authorization,
    filter_platform_cookies,
    has_authenticated_session,
    validate_local_return_url,
    verify_login_authorization,
    write_netscape_cookie_file,
)


def test_filter_platform_cookies_excludes_unrelated_domains():
    cookies = [
        {"name": "sessionid", "value": "session", "domain": ".douyin.com"},
        {"name": "ttwid", "value": "visitor", "domain": ".douyin.com"},
        {"name": "SID", "value": "google", "domain": ".google.com"},
    ]

    filtered = filter_platform_cookies(cookies, "douyin")

    assert {item["name"] for item in filtered} == {"sessionid", "ttwid"}


@pytest.mark.parametrize(
    ("platform", "cookies", "expected"),
    [
        (
            "bilibili",
            [
                {"name": "SESSDATA", "domain": ".bilibili.com"},
                {"name": "bili_jct", "domain": ".bilibili.com"},
                {"name": "DedeUserID", "domain": ".bilibili.com"},
            ],
            True,
        ),
        (
            "douyin",
            [{"name": "sessionid_ss", "domain": ".douyin.com"}],
            True,
        ),
        (
            "douyin",
            [{"name": "ttwid", "domain": ".douyin.com"}],
            False,
        ),
    ],
)
def test_has_authenticated_session(platform, cookies, expected):
    assert has_authenticated_session(cookies, platform) is expected


def test_build_netscape_cookie_text_marks_http_only():
    text = build_netscape_cookie_text(
        [
            {
                "name": "sessionid",
                "value": "secret",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 123456,
            }
        ],
        "douyin",
    )

    assert text.startswith("# Netscape HTTP Cookie File")
    assert "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t123456\tsessionid\tsecret" in text


def test_write_cookie_file_is_private_and_atomic(tmp_path):
    cookie_path = tmp_path / "douyin_cookies.txt"

    count = write_netscape_cookie_file(
        [
            {
                "name": "sessionid",
                "value": "secret",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ],
        "douyin",
        cookie_path,
    )

    assert count == 1
    assert cookie_path.is_file()
    assert os.stat(cookie_path).st_mode & 0o777 == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "return_url",
    [
        "https://127.0.0.1:5188/transfer-center/source-login/result",
        "http://example.com:5188/transfer-center/source-login/result",
        "http://127.0.0.1:5191/transfer-center/source-login/result",
        "http://127.0.0.1:5188/settings",
    ],
)
def test_validate_local_return_url_rejects_untrusted_destination(return_url):
    with pytest.raises(ValueError, match="返回地址"):
        validate_local_return_url(return_url)


def test_validate_local_return_url_accepts_transfer_center_result():
    return_url = (
        "http://127.0.0.1:5188/transfer-center/source-login/result"
        "?platform=douyin"
    )

    assert validate_local_return_url(return_url) == return_url


def test_short_lived_login_authorization_round_trip():
    secret = "a" * 64
    return_url = (
        "http://127.0.0.1:5188/transfer-center/source-login/result"
        "?platform=douyin"
    )
    authorization = create_login_authorization(
        secret,
        "douyin",
        return_url,
        now=1000,
        nonce="fixed-nonce-value",
    )

    assert verify_login_authorization(
        secret,
        authorization,
        now=1040,
    ) == ("douyin", return_url)
    assert "secret" not in authorization


def test_login_authorization_rejects_tampering_and_expiry():
    secret = "a" * 64
    return_url = (
        "http://127.0.0.1:5188/transfer-center/source-login/result"
        "?platform=douyin"
    )
    authorization = create_login_authorization(
        secret,
        "douyin",
        return_url,
        now=1000,
        nonce="fixed-nonce-value",
    )

    with pytest.raises(ValueError, match="验证失败"):
        verify_login_authorization(
            secret,
            {**authorization, "platform": "bilibili"},
            now=1040,
        )
    with pytest.raises(ValueError, match="失效"):
        verify_login_authorization(secret, authorization, now=1200)
