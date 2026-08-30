import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

import modules.transfer_center as transfer_module
from modules.openlist_backup import (
    OpenListBackupClient,
    OpenListBackupError,
    safe_remote_name,
)


def _openlist_db(path: Path, token: str = "local-admin-token") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE x_setting_items (key TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO x_setting_items(key, value) VALUES('token', ?)",
            (token,),
        )


def test_openlist_client_rejects_non_loopback_endpoint(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        OpenListBackupClient(
            base_url="https://openlist.example.com",
            database_path=str(tmp_path / "data.db"),
        )


def test_openlist_client_uploads_with_local_token_and_verifies_size(tmp_path):
    database = tmp_path / "data.db"
    _openlist_db(database)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"finished-video")
    session = Mock()
    session.trust_env = True
    upload_response = Mock(ok=True, status_code=200)
    upload_response.json.return_value = {"code": 200, "message": "success"}
    list_response = Mock(ok=True, status_code=200)
    list_response.json.return_value = {
        "code": 200,
        "data": {"content": [{"name": "成片.mp4", "size": len(b"finished-video")}]},
    }
    session.put.return_value = upload_response
    session.post.return_value = list_response
    client = OpenListBackupClient(database_path=str(database), session=session)

    assert client.upload_file(str(source), "/115-视频备份/视频搬运/成片.mp4") == 14
    assert session.trust_env is False
    assert session.put.call_args.kwargs["headers"]["Authorization"] == "local-admin-token"
    assert "%E8%A7%86%E9%A2%91%E6%90%AC%E8%BF%90" in session.put.call_args.kwargs["headers"]["File-Path"]


def test_openlist_client_rejects_remote_size_mismatch(tmp_path):
    database = tmp_path / "data.db"
    _openlist_db(database)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"finished-video")
    session = Mock()
    response = Mock(ok=True, status_code=200)
    response.json.side_effect = [
        {"code": 200, "message": "success"},
        {"code": 200, "data": {"content": [{"name": "成片.mp4", "size": 1}]}},
    ]
    session.put.return_value = response
    session.post.return_value = response
    client = OpenListBackupClient(database_path=str(database), session=session)

    with pytest.raises(OpenListBackupError, match="大小校验"):
        client.upload_file(str(source), "/115-视频备份/视频搬运/成片.mp4")


def test_safe_remote_name_removes_path_characters():
    assert safe_remote_name(' A/B:*?"<>|  ') == "A-B"


def test_transfer_backup_records_verified_cloud_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transfer_module,
        "get_app_subdir",
        lambda name: str(tmp_path / name) if name else str(tmp_path),
    )
    center = transfer_module.TransferCenter(
        config_provider=lambda: {"TRANSFER_115_BACKUP_ENABLED": True}
    )
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1backupverified", ["youtube"]
    )
    video = tmp_path / "final.mp4"
    video.write_bytes(b"verified-video")
    center._update_job(
        job_id,
        title="115 备份测试",
        local_video_path=str(video),
        recreation_status="approved",
        reviewed_at="2026-08-30T12:00:00+00:00",
    )

    class FakeClient:
        remote_root = "/115-视频备份/视频搬运"

        def __init__(self):
            self.directories = []
            self.files = []

        def mkdir(self, path):
            self.directories.append(path)

        def upload_file(self, local_path, remote_path):
            self.files.append(remote_path)
            return Path(local_path).stat().st_size

        def upload_json(self, payload, remote_path):
            self.files.append(remote_path)
            assert payload["schema"] == "sg99.video-transfer-backup.v1"
            return len(json.dumps(payload, ensure_ascii=False).encode())

    client = FakeClient()
    monkeypatch.setattr(center, "_backup_client", lambda: client)
    monkeypatch.setattr(center, "_emit_backup_notification", lambda *args: None)

    result = center.backup_job(job_id)

    assert result["backup_status"] == "completed"
    assert result["backup_attempts"] == 1
    assert result["backup_verified_at"]
    assert result["backup_sha256"] == center._file_sha256(str(video))
    assert result["backup_remote_path"].startswith("/115-视频备份/视频搬运/")
    assert any(path.endswith("/成片.mp4") for path in client.files)
    assert any(path.endswith("/备份信息.json") for path in client.files)


def test_backup_failure_does_not_change_publish_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transfer_module,
        "get_app_subdir",
        lambda name: str(tmp_path / name) if name else str(tmp_path),
    )
    center = transfer_module.TransferCenter(
        config_provider=lambda: {"TRANSFER_115_BACKUP_ENABLED": True}
    )
    job_id = center.add_manual_job(
        "https://www.bilibili.com/video/BV1backupretry", ["youtube"]
    )
    center._update_job(job_id, status="ready", recreation_status="approved")
    center._schedule_backup_retry(job_id, 1, "temporary failure")

    job = center.get_job(job_id)
    assert job["status"] == "ready"
    assert job["backup_status"] == "failed"
    assert job["backup_next_retry_at"]
    assert "temporary failure" in job["backup_error"]
