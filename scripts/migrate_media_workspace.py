#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan or execute verified, copy-only migration to an external media disk.

The default mode is read-only planning.  ``--execute-copy`` copies into new
destinations, hashes source and destination, and keeps every source intact.  It
never changes runtime configuration, symlinks, services, or deletes old data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.media_workspace import (  # noqa: E402
    DEFAULT_MIN_FREE_GB,
    MediaWorkspaceConfig,
    build_tree_copy_plan,
    execute_tree_copy_plan,
    validate_media_workspace,
    verify_tree_copy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely plan or copy video workspace data to a verified APFS volume."
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--volume-uuid", required=True)
    parser.add_argument(
        "--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB
    )
    parser.add_argument(
        "--source-downloads", default=str(PROJECT_ROOT / "downloads")
    )
    parser.add_argument("--source-temp", default=str(PROJECT_ROOT / "temp"))
    parser.add_argument(
        "--mpt-storage-source",
        help="Optional quiesced MoneyPrinter shared/storage source. No service is stopped here.",
    )
    parser.add_argument(
        "--mpt-storage-destination",
        help="Optional new MPT copy destination; must be supplied with --mpt-storage-source.",
    )
    parser.add_argument(
        "--confirm-mpt-quiesced",
        action="store_true",
        help=(
            "Required for an MPT execute-copy. Confirms its writers were stopped and "
            "a consistent database backup was taken outside this script."
        ),
    )
    parser.add_argument(
        "--execute-copy",
        action="store_true",
        help="Copy and verify into new destinations. Sources remain untouched.",
    )
    parser.add_argument(
        "--receipt",
        help="Optional new JSON receipt path. Existing files are never overwritten.",
    )
    return parser


def _new_receipt(path: str, payload: dict) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if bool(args.mpt_storage_source) != bool(args.mpt_storage_destination):
        raise SystemExit(
            "--mpt-storage-source and --mpt-storage-destination must be supplied together"
        )
    if args.execute_copy and args.mpt_storage_source and not args.confirm_mpt_quiesced:
        raise SystemExit(
            "MPT copy requires --confirm-mpt-quiesced after its writers are stopped and backed up"
        )

    workspace = MediaWorkspaceConfig.from_values(
        args.workspace_root,
        args.volume_uuid,
        min_free_gb=args.min_free_gb,
    )
    destinations = [
        ("video-downloads", args.source_downloads, workspace.root / "downloads"),
        ("video-temp", args.source_temp, workspace.root / "temp"),
    ]
    if args.mpt_storage_source:
        destinations.append(
            (
                "moneyprinter-storage-copy",
                args.mpt_storage_source,
                Path(os.path.abspath(os.path.expanduser(args.mpt_storage_destination))),
            )
        )

    workspace_text = str(workspace.root)
    for _label, _source, destination in destinations:
        destination_text = str(Path(destination))
        try:
            inside_workspace = (
                os.path.commonpath((workspace_text, destination_text)) == workspace_text
            )
        except ValueError:
            inside_workspace = False
        if not inside_workspace or destination_text == workspace_text:
            raise SystemExit(
                f"copy destination must be a child of the verified workspace: {destination}"
            )

    plans = [
        build_tree_copy_plan(source, destination, label=label)
        for label, source, destination in destinations
    ]
    required_bytes = sum(plan.total_bytes for plan in plans)
    status = validate_media_workspace(
        workspace,
        create=args.execute_copy,
        write_probe=args.execute_copy,
        required_bytes=required_bytes,
    )

    payload = {
        "schema": "video-transfer.media-workspace-copy.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "copy-and-verify" if args.execute_copy else "plan-only",
        "workspace": {
            "root": str(status.root),
            "mount_point": str(status.mount_point),
            "volume_uuid": status.volume_uuid,
            "total_bytes": status.total_bytes,
            "free_bytes": status.free_bytes,
            "min_free_bytes": status.min_free_bytes,
        },
        "plans": [plan.public_summary() for plan in plans],
        "source_deletion_performed": False,
        "runtime_switch_performed": False,
    }

    if args.execute_copy:
        results = []
        for plan in plans:
            result = execute_tree_copy_plan(
                plan,
                reserve_bytes=workspace.min_free_bytes,
            )
            if not verify_tree_copy(plan):
                raise RuntimeError(
                    f"Post-copy verification failed for {plan.label}; source remains intact."
                )
            results.append(
                {
                    "label": result.label,
                    "destination": str(result.destination),
                    "file_count": result.file_count,
                    "total_bytes": result.total_bytes,
                    "manifest_sha256": result.manifest_sha256,
                    "source_preserved": result.source_preserved,
                    "activated": result.activated,
                }
            )
        payload["results"] = results

    if args.receipt:
        _new_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
