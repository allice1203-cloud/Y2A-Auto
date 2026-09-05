from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_runner_uses_complete_isolated_source_copy():
    script = (ROOT / "scripts" / "run_release_tests.sh").read_text(
        encoding="utf-8"
    )

    assert "mktemp -d" in script
    assert '"$PROJECT_ROOT/" "$TEST_ROOT/"' in script
    assert '--volume "$TEST_ROOT:/workspace"' in script
    assert "pytest==8.3.5" in script
    assert "cache_dir=/tmp/pytest_cache" in script
    assert "--user root" not in script
    assert "python -m pip install --user" in script


def test_release_runner_excludes_runtime_credentials_and_data():
    script = (ROOT / "scripts" / "run_release_tests.sh").read_text(
        encoding="utf-8"
    )

    for runtime_dir in (
        "config/",
        "db/",
        "cookies/",
        "downloads/",
        "logs/",
        "temp/",
        "backups/",
    ):
        assert f"--exclude='{runtime_dir}'" in script
