"""Build a new local MPT release without touching its developer worktree.

The active release is copied with its existing shared-resource symlinks. A
narrow, asserted overlay separates the media root from the database root.
This script does not activate the release or modify any credentials/data.
"""
from pathlib import Path
import argparse
import json
import re
import shutil


EXTERNAL_HELPER = '''"""Validated external media; SQLite and credentials stay on the internal disk."""
import os
from pathlib import Path
from .media_workspace_guard import MediaWorkspaceConfig, validate_media_workspace

def media_root(legacy_root):
    root = os.environ.get("MPT_MEDIA_WORKSPACE_ROOT", "")
    volume_uuid = os.environ.get("MPT_MEDIA_VOLUME_UUID", "")
    if not root and not volume_uuid:
        return legacy_root
    config = MediaWorkspaceConfig.from_values(root, volume_uuid, min_free_gb=0)
    validate_media_workspace(config, create=False, write_probe=False)
    return str(config.root)

def database_root(legacy_root):
    root = os.environ.get("MPT_DATABASE_ROOT", "") or legacy_root
    if not os.path.isabs(root) or Path(root).resolve().is_relative_to(Path("/Volumes")):
        raise ValueError("MPT databases must remain on the internal disk")
    return root

def require_media_child(root, path):
    base = Path(root).resolve()
    target = Path(path).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Media path escapes its configured root")
    return str(target)
'''


def build_release(source: Path, destination: Path, guard: Path) -> dict:
    if destination.exists() or destination.is_symlink():
        raise ValueError("New release destination must not already exist")
    if not (source / "main.py").is_file() or not (source / "app/utils/utils.py").is_file():
        raise ValueError("Not an existing MPT application release")
    original = (source / "app/utils/utils.py").read_text()
    anchor = '    d = os.path.join(root_dir(), "storage")\n'
    if original.count(anchor) != 1:
        raise ValueError("MPT storage implementation changed; review the overlay first")
    changes = {}
    pattern = re.compile(r'\{utils\.storage_dir\(create=True\)\}(/[A-Za-z0-9_]+\.db)')
    for path in sorted((source / "app/services").glob("*.py")):
        text = path.read_text()
        patched, count = pattern.subn(r'{utils.database_dir(create=True)}\1', text)
        if path.name == "object_storage.py":
            old = 'f"{storage_root}/video_objects.db"'
            if patched.count(old) != 1:
                raise ValueError("Object storage database constructor changed")
            patched = patched.replace(old, 'f"{utils.database_dir(create=True)}/video_objects.db"')
            count += 1
        if count:
            changes[str(path.relative_to(source))] = (patched, count)
    if sum(count for _, count in changes.values()) != 22:
        raise ValueError("Expected exactly 22 database constructors; refusing partial migration")
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"))
    # Release files can be read-only. Relax only the new, private copy being
    # patched, never the active release or its shared-resource symlinks.
    for relative in ["app/utils/utils.py", *changes]:
        (destination / relative).chmod(0o644)
    (destination / "app/utils").chmod(0o755)
    utils = original.replace(anchor, anchor + '    from .external_media import media_root, require_media_child\n    d = media_root(d)\n    base = d\n')
    utils = utils.replace('    if create and not os.path.exists(d):\n',
        '    d = require_media_child(base, d)\n    if create and not os.path.exists(d):\n', 1)
    database_helper = '''
def database_dir(create: bool = False):
    from .external_media import database_root
    d = database_root(os.path.join(root_dir(), "storage"))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


'''
    utils = utils.replace('def resource_dir(sub_dir: str = ""):', database_helper + 'def resource_dir(sub_dir: str = ""):')
    (destination / "app/utils/utils.py").write_text(utils)
    (destination / "app/utils/external_media.py").write_text(EXTERNAL_HELPER)
    shutil.copy2(guard, destination / "app/utils/media_workspace_guard.py")
    for relative, (text, _) in changes.items():
        (destination / relative).write_text(text)
    return {"source": str(source), "destination": str(destination),
            "database_constructors": 22, "changed_services": sorted(changes),
            "activated": False, "credentials_changed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    guard = Path(__file__).resolve().parents[1] / "modules/media_workspace.py"
    print(json.dumps(build_release(args.source.resolve(), args.destination, guard), ensure_ascii=False, indent=2))
