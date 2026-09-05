import json
from datetime import datetime, timezone

import pytest

from modules.config_snapshots import (
    create_config_snapshot,
    list_config_snapshots,
    load_config_snapshot,
    safe_config_values,
)


def test_safe_config_values_omits_credentials():
    safe = safe_config_values(
        {
            "AUTO_MODE_ENABLED": True,
            "OPENAI_API_KEY": "secret",
            "NOTIFY_TELEGRAM_BOT_TOKEN": "secret",
            "COOKIECLOUD_PASSWORD": "secret",
            "YOUTUBE_PROXY_USERNAME": "user",
            "VIDEO_CPU_PRESET": "medium",
        }
    )

    assert safe == {
        "AUTO_MODE_ENABLED": True,
        "VIDEO_CPU_PRESET": "medium",
    }


def test_snapshot_roundtrip_is_atomic_private_and_bounded(tmp_path):
    first = create_config_snapshot(
        {"AUTO_MODE_ENABLED": True, "OPENAI_API_KEY": "never-store"},
        directory=tmp_path,
        keep=1,
        now=datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc),
    )
    second = create_config_snapshot(
        {"AUTO_MODE_ENABLED": False, "VIDEO_CPU_PRESET": "veryfast"},
        directory=tmp_path,
        keep=1,
        now=datetime(2026, 8, 31, 1, 2, 4, tzinfo=timezone.utc),
    )

    files = list(tmp_path.glob("config-*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert first["snapshot_id"] != second["snapshot_id"]
    assert list_config_snapshots(directory=tmp_path)[0]["snapshot_id"] == second["snapshot_id"]
    assert load_config_snapshot(second["snapshot_id"], directory=tmp_path) == {
        "AUTO_MODE_ENABLED": False,
        "VIDEO_CPU_PRESET": "veryfast",
    }
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert "OPENAI_API_KEY" not in payload["config"]


def test_snapshot_id_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="无效的设置快照"):
        load_config_snapshot("../../config-secret", directory=tmp_path)
