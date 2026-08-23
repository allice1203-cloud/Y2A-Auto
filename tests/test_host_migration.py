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
    "ops/com.video-transfer-channel.cookie-sync.plist",
    "scripts/sync_source_cookies_to_server.sh",
)


class HostMigrationTests(unittest.TestCase):
    def test_active_files_do_not_reference_retired_hosts(self):
        for relative_path in ACTIVE_TEXT_FILES:
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIsNone(
                RETIRED_HOST_PATTERN.search(content),
                msg=f"retired host reference in {relative_path}",
            )

    def test_hong_kong_compose_is_the_only_host_specific_compose(self):
        self.assertTrue((ROOT / "docker-compose.hk.yml").is_file())
        retired_name = "docker-compose." + "win" + "mini.yml"
        self.assertFalse((ROOT / retired_name).exists())

    def test_cookie_sync_defaults_to_hong_kong_vps(self):
        content = (ROOT / "scripts/sync_source_cookies_to_server.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("VIDEO_TRANSFER_SYNC_HOST:-vps-hk", content)
        self.assertIn("/home/ubuntu/apps/video-transfer-channel/cookies/", content)


if __name__ == "__main__":
    unittest.main()
