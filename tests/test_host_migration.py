from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
RETIRED_HOST_PATTERN = re.compile(
    "(?:win|mac)" + r"[ _-]?" + "mini",
    re.IGNORECASE,
)
ACTIVE_TEXT_FILES = (
    "README.md",
    "docs/public-access.md",
    "Dockerfile.release-overlay",
    "Dockerfile.release-update",
    "modules/transfer_center.py",
    "templates/base.html",
    "templates/login.html",
    "templates/settings.html",
    "templates/tasks.html",
    "templates/transfer_center.html",
)


class HostMigrationTests(unittest.TestCase):
    def test_active_files_do_not_reference_retired_hosts(self):
        for relative_path in ACTIVE_TEXT_FILES:
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIsNone(
                RETIRED_HOST_PATTERN.search(content),
                msg=f"retired host reference in {relative_path}",
            )

    def test_host_specific_compose_files_are_absent(self):
        retired_names = (
            "docker-compose.hk.yml",
            "docker-compose." + "win" + "mini.yml",
        )
        for relative_path in retired_names:
            self.assertFalse((ROOT / relative_path).exists(), relative_path)

    def test_retired_remote_cookie_sync_and_tunnel_files_are_absent(self):
        retired_files = (
            "scripts/sync_source_cookies_to_server.sh",
            "ops/com.video-transfer-channel.cookie-sync.plist",
            "ops/video-transfer-channel-tunnel.service",
            "ops/video-transfer-channel-tunnel.winmini.yml",
        )
        for relative_path in retired_files:
            self.assertFalse((ROOT / relative_path).exists(), relative_path)


if __name__ == "__main__":
    unittest.main()
