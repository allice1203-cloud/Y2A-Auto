from modules.local_proxy import configure_local_system_proxy


def test_stale_loopback_proxy_is_refreshed_from_macos_settings():
    environ = {
        "HTTP_PROXY": "http://127.0.0.1:1082",
        "HTTPS_PROXY": "http://127.0.0.1:1082",
        "NO_PROXY": "localhost",
    }

    status = configure_local_system_proxy(
        environ,
        proxy_provider=lambda: {
            "http": "http://127.0.0.1:7892",
            "https": "http://127.0.0.1:7892",
        },
    )

    assert status["configured"] is True
    assert environ["HTTP_PROXY"] == "http://127.0.0.1:7892"
    assert environ["HTTPS_PROXY"] == "http://127.0.0.1:7892"
    assert environ["http_proxy"] == "http://127.0.0.1:7892"
    assert environ["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_remote_explicit_proxy_is_never_replaced():
    environ = {"HTTPS_PROXY": "https://proxy.example:8443"}

    status = configure_local_system_proxy(
        environ,
        proxy_provider=lambda: {"https": "http://127.0.0.1:7892"},
    )

    assert status["configured"] is False
    assert environ["HTTPS_PROXY"] == "https://proxy.example:8443"


def test_credential_or_non_loopback_system_proxy_is_rejected():
    environ = {}

    status = configure_local_system_proxy(
        environ,
        proxy_provider=lambda: {
            "http": "http://user:secret@127.0.0.1:7892",
            "https": "http://192.168.1.2:7892",
        },
    )

    assert status["configured"] is False
    assert "HTTP_PROXY" not in environ
    assert "HTTPS_PROXY" not in environ
