from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicSecurityTests(unittest.TestCase):
    def test_system_health_requires_login(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertRegex(
            source,
            re.compile(
                r"@app\.route\(['\"]?/system_health['\"]?\)\s*"
                r"@login_required\s*def system_health\(\):"
            ),
        )

    def test_transfer_intelligence_routes_require_login(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        route_fragments = (
            "/transfer-center/candidates/refresh",
            "/transfer-center/candidates/<candidate_id>/promote",
            "/transfer-center/candidates/<candidate_id>/dismiss",
            "/transfer-center/archives/<source_id>/refresh",
            "/transfer-center/archives/<source_id>/resume",
            "/transfer-center/archives/<source_id>/manifest",
        )
        for route in route_fragments:
            self.assertRegex(
                source,
                re.compile(
                    rf"@app\.route\(['\"]{re.escape(route)}['\"](?:, methods=\[[^\]]+\])?\)\s*"
                    r"@login_required\s*def "
                ),
                msg=f"missing login protection for {route}",
            )


if __name__ == "__main__":
    unittest.main()
