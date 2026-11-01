#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fail-closed local media workspace and verified copy helpers.

Only disposable, high-volume media directories (``downloads`` and ``temp``)
are eligible for routing to an external volume.  Databases, configuration,
cookies and credentials deliberately remain under the application root.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence


WORKSPACE_ROOT_ENV = "VIDEO_TRANSFER_MEDIA_WORKSPACE_ROOT"
VOLUME_UUID_ENV = "VIDEO_TRANSFER_MEDIA_VOLUME_UUID"
MIN_FREE_GB_ENV = "VIDEO_TRANSFER_MEDIA_MIN_FREE_GB"

ROUTED_SUBDIRS = frozenset({"downloads", "temp"})
DEFAULT_MIN_FREE_GB = 60.0
_GIB = 1024**3


class MediaWorkspaceError(RuntimeError):
    """Base class for media workspace safety failures."""


class MediaWorkspaceConfigurationError(MediaWorkspaceError):
    """The workspace configuration is incomplete or unsafe."""


class MediaWorkspaceUnavailableError(MediaWorkspaceError):
    """The configured external volume is not currently available."""


class MediaWorkspaceIdentityError(MediaWorkspaceError):
    """The mounted volume is not the explicitly configured volume."""


class MediaWorkspaceCapacityError(MediaWorkspaceError):
    """The configured workspace does not have the required free space."""


class MediaWorkspaceCopyError(MediaWorkspaceError):
    """A verified copy could not be completed safely."""


def _normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip())).upper()
    except (AttributeError, TypeError, ValueError) as exc:
        raise MediaWorkspaceConfigurationError(
            "External media volume UUID is missing or invalid."
        ) from exc


def _gb_to_bytes(value: object) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaWorkspaceConfigurationError(
            "Media workspace minimum free space must be a number."
        ) from exc
    if parsed < 0:
        raise MediaWorkspaceConfigurationError(
            "Media workspace minimum free space cannot be negative."
        )
    return int(parsed * _GIB)


@dataclass(frozen=True)
class MediaWorkspaceConfig:
    root: Path
    volume_uuid: str
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GB * _GIB)

    @classmethod
    def from_values(
        cls,
        root: os.PathLike[str] | str,
        volume_uuid: str,
        *,
        min_free_gb: object = DEFAULT_MIN_FREE_GB,
    ) -> "MediaWorkspaceConfig":
        root_text = os.path.expandvars(os.path.expanduser(str(root or "").strip()))
        if not root_text or not os.path.isabs(root_text):
            raise MediaWorkspaceConfigurationError(
                "Media workspace root must be an absolute path on /Volumes."
            )
        return cls(
            root=Path(os.path.normpath(root_text)),
            volume_uuid=_normalize_uuid(volume_uuid),
            min_free_bytes=_gb_to_bytes(min_free_gb),
        )


@dataclass(frozen=True)
class ExternalVolumeIdentity:
    mount_point: Path
    volume_uuid: str
    internal: bool
    writable: bool
    filesystem_type: str


@dataclass(frozen=True)
class MediaWorkspaceStatus:
    root: Path
    mount_point: Path
    volume_uuid: str
    total_bytes: int
    free_bytes: int
    min_free_bytes: int
    required_bytes: int
    writable: bool

    @property
    def ready(self) -> bool:
        return self.writable and self.free_bytes >= (
            self.min_free_bytes + self.required_bytes
        )


def load_media_workspace_config(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[MediaWorkspaceConfig]:
    """Load non-secret workspace settings from the process environment.

    No setting means legacy project-local storage remains active.  A partial
    setting is an error so an unplugged or mistyped external path can never
    silently fall back to the internal disk.
    """

    source = os.environ if env is None else env
    root = str(source.get(WORKSPACE_ROOT_ENV, "") or "").strip()
    volume_uuid = str(source.get(VOLUME_UUID_ENV, "") or "").strip()
    if not root and not volume_uuid:
        return None
    if not root or not volume_uuid:
        raise MediaWorkspaceConfigurationError(
            f"{WORKSPACE_ROOT_ENV} and {VOLUME_UUID_ENV} must be configured together."
        )
    return MediaWorkspaceConfig.from_values(
        root,
        volume_uuid,
        min_free_gb=source.get(MIN_FREE_GB_ENV, DEFAULT_MIN_FREE_GB),
    )


def _configured_mount_point(root: Path) -> Path:
    parts = root.parts
    if len(parts) < 3 or parts[0] != os.sep or parts[1] != "Volumes":
        raise MediaWorkspaceConfigurationError(
            "Media workspace must be a child directory of an external /Volumes mount."
        )
    mount_point = Path(os.sep, "Volumes", parts[2])
    if root == mount_point:
        raise MediaWorkspaceConfigurationError(
            "Media workspace must use a dedicated directory below the volume root."
        )
    return mount_point


def inspect_external_volume(
    workspace_root: os.PathLike[str] | str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ExternalVolumeIdentity:
    """Read the mounted volume identity with ``diskutil`` without writing it."""

    root = Path(os.path.normpath(os.path.abspath(str(workspace_root))))
    mount_point = _configured_mount_point(root)
    if not mount_point.is_dir() or not os.path.ismount(mount_point):
        raise MediaWorkspaceUnavailableError(
            f"Configured external volume is not mounted: {mount_point}"
        )

    try:
        result = runner(
            ["diskutil", "info", "-plist", str(mount_point)],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaWorkspaceUnavailableError(
            f"Unable to inspect configured external volume: {mount_point}"
        ) from exc
    if result.returncode != 0:
        raise MediaWorkspaceUnavailableError(
            f"Unable to inspect configured external volume: {mount_point}"
        )

    try:
        payload = plistlib.loads(result.stdout)
        observed_mount = Path(str(payload.get("MountPoint") or ""))
        observed_uuid = _normalize_uuid(str(payload.get("VolumeUUID") or ""))
    except (MediaWorkspaceError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
        raise MediaWorkspaceUnavailableError(
            f"diskutil returned incomplete volume identity for {mount_point}"
        ) from exc

    if observed_mount != mount_point:
        raise MediaWorkspaceUnavailableError(
            f"Mounted volume path changed while being inspected: {mount_point}"
        )
    return ExternalVolumeIdentity(
        mount_point=mount_point,
        volume_uuid=observed_uuid,
        internal=bool(payload.get("Internal", True)),
        writable=bool(payload.get("Writable", False)),
        filesystem_type=str(payload.get("FilesystemType") or "").strip().lower(),
    )


def _nearest_existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir():
        raise MediaWorkspaceUnavailableError(
            f"No existing parent directory for media workspace: {path}"
        )
    return candidate


def probe_writable_directory(path: os.PathLike[str] | str) -> None:
    """Perform a zero-byte create/fsync/unlink probe in a target directory."""

    directory = Path(path)
    probe_path = directory / f".video-transfer-write-probe-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            probe_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.fsync(descriptor)
    except OSError as exc:
        raise MediaWorkspaceUnavailableError(
            f"Media workspace is not writable: {directory}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MediaWorkspaceUnavailableError(
                f"Unable to remove media workspace write probe: {probe_path}"
            ) from exc


def validate_media_workspace(
    config: MediaWorkspaceConfig,
    *,
    create: bool = False,
    write_probe: bool = True,
    required_bytes: int = 0,
    volume_inspector: Callable[[os.PathLike[str] | str], ExternalVolumeIdentity] = inspect_external_volume,
    disk_usage: Callable[[os.PathLike[str] | str], object] = shutil.disk_usage,
    writable_probe: Callable[[os.PathLike[str] | str], None] = probe_writable_directory,
) -> MediaWorkspaceStatus:
    """Validate mount identity, APFS, writability and remaining capacity."""

    if required_bytes < 0:
        raise MediaWorkspaceConfigurationError("Required copy size cannot be negative.")
    identity = volume_inspector(config.root)
    if identity.volume_uuid != config.volume_uuid:
        raise MediaWorkspaceIdentityError(
            "Mounted external volume UUID does not match the configured media volume."
        )
    if identity.internal:
        raise MediaWorkspaceIdentityError(
            "Configured media workspace resolved to an internal volume."
        )
    if identity.filesystem_type != "apfs":
        raise MediaWorkspaceIdentityError(
            "Configured media workspace must be on an APFS volume."
        )
    if not identity.writable:
        raise MediaWorkspaceUnavailableError("Configured external volume is read-only.")

    root = config.root
    mount_real = identity.mount_point.resolve(strict=True)
    root_parent = _nearest_existing_directory(root).resolve(strict=True)
    try:
        if os.path.commonpath((str(mount_real), str(root_parent))) != str(mount_real):
            raise MediaWorkspaceIdentityError(
                "Media workspace escapes the configured external volume."
            )
    except ValueError as exc:
        raise MediaWorkspaceIdentityError(
            "Media workspace is not on the configured external volume."
        ) from exc

    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaWorkspaceUnavailableError(
                f"Unable to create media workspace: {root}"
            ) from exc
    if root.exists() and not root.is_dir():
        raise MediaWorkspaceUnavailableError(
            f"Media workspace path is not a directory: {root}"
        )

    usage = disk_usage(identity.mount_point)
    total_bytes = int(getattr(usage, "total"))
    free_bytes = int(getattr(usage, "free"))
    minimum = config.min_free_bytes + int(required_bytes)
    if free_bytes < minimum:
        raise MediaWorkspaceCapacityError(
            "External media volume does not have enough free space for the configured "
            "reserve and requested copy."
        )

    writable_directory = root if root.is_dir() else root_parent
    if not os.access(writable_directory, os.W_OK):
        raise MediaWorkspaceUnavailableError(
            f"Media workspace parent is not writable: {writable_directory}"
        )
    if write_probe:
        if not root.is_dir():
            raise MediaWorkspaceUnavailableError(
                "Media workspace must exist before a write probe can run."
            )
        writable_probe(root)

    return MediaWorkspaceStatus(
        root=root,
        mount_point=identity.mount_point,
        volume_uuid=identity.volume_uuid,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        min_free_bytes=config.min_free_bytes,
        required_bytes=int(required_bytes),
        writable=True,
    )


def resolve_configured_media_subdir(
    subdir_name: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve ``downloads``/``temp`` to the configured external workspace.

    Returns ``None`` only when no external workspace is configured.  Any
    configured-but-unavailable state raises instead of falling back internally.
    """

    normalized = str(subdir_name or "").strip()
    if normalized not in ROUTED_SUBDIRS:
        raise MediaWorkspaceConfigurationError(
            f"Only {sorted(ROUTED_SUBDIRS)} may use the external media workspace."
        )
    config = load_media_workspace_config(env)
    if config is None:
        return None

    status = validate_media_workspace(config, create=True, write_probe=True)
    target = config.root / normalized
    if target.is_symlink():
        raise MediaWorkspaceIdentityError(
            "Media workspace subdirectory must not be a symbolic link."
        )
    try:
        target.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise MediaWorkspaceUnavailableError(
            f"Unable to prepare media workspace directory: {target}"
        ) from exc
    if not target.is_dir():
        raise MediaWorkspaceUnavailableError(
            f"Media workspace subdirectory is invalid: {target}"
        )
    # Validate the actual child, not just its parent.  A downloads/temp symlink
    # or nested mount could otherwise send large writes back to the internal SSD.
    if (
        target.is_symlink()
        or target.resolve(strict=True).parent != config.root.resolve(strict=True)
        or target.stat().st_dev != status.mount_point.stat().st_dev
    ):
        raise MediaWorkspaceIdentityError(
            "Media workspace subdirectory escapes the configured external volume."
        )
    probe_writable_directory(target)
    return str(target)


@dataclass(frozen=True)
class FileManifestEntry:
    relative_path: str
    size: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True)
class TreeCopyPlan:
    label: str
    source: Path
    destination: Path
    directories: tuple[str, ...]
    files: tuple[FileManifestEntry, ...]
    total_bytes: int
    manifest_sha256: str

    def public_summary(self) -> dict:
        return {
            "label": self.label,
            "source": str(self.source),
            "destination": str(self.destination),
            "directory_count": len(self.directories),
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "manifest_sha256": self.manifest_sha256,
            "source_will_be_deleted": False,
            "destination_will_be_activated": False,
        }


@dataclass(frozen=True)
class TreeCopyResult:
    label: str
    source: Path
    destination: Path
    file_count: int
    total_bytes: int
    manifest_sha256: str
    source_preserved: bool = True
    activated: bool = False


def _hash_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _inventory_tree(root: Path) -> tuple[tuple[str, ...], tuple[FileManifestEntry, ...]]:
    directories: list[str] = []
    files: list[FileManifestEntry] = []
    for current_text, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        dir_names.sort()
        file_names.sort()
        for name in list(dir_names):
            path = current / name
            if path.is_symlink():
                raise MediaWorkspaceCopyError(
                    f"Copy source contains a symlinked directory: {path}"
                )
            directories.append(path.relative_to(root).as_posix())
        for name in file_names:
            path = current / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise MediaWorkspaceCopyError(
                    f"Copy source contains a symlinked file: {path}"
                )
            if not stat.S_ISREG(info.st_mode):
                raise MediaWorkspaceCopyError(
                    f"Copy source contains a non-regular file: {path}"
                )
            files.append(
                FileManifestEntry(
                    relative_path=path.relative_to(root).as_posix(),
                    size=int(info.st_size),
                    modified_ns=int(info.st_mtime_ns),
                    sha256=_hash_file(path),
                )
            )
    return tuple(sorted(set(directories))), tuple(files)


def _manifest_digest(
    directories: Sequence[str], files: Sequence[FileManifestEntry]
) -> str:
    payload = {
        "directories": list(directories),
        "files": [
            {
                "path": item.relative_path,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_tree_copy_plan(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    label: str = "media",
) -> TreeCopyPlan:
    """Inventory and hash a source tree without modifying either tree."""

    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_dir():
        raise MediaWorkspaceCopyError(f"Copy source is not a directory: {source_path}")
    destination_path = Path(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(destination))))
    )
    if destination_path.exists():
        raise MediaWorkspaceCopyError(
            f"Copy destination already exists; refusing to merge or overwrite: {destination_path}"
        )
    try:
        if os.path.commonpath((str(source_path), str(destination_path))) == str(source_path):
            raise MediaWorkspaceCopyError(
                "Copy destination cannot be inside the source directory."
            )
    except ValueError as exc:
        raise MediaWorkspaceCopyError("Copy source and destination are incompatible.") from exc

    directories, files = _inventory_tree(source_path)
    return TreeCopyPlan(
        label=str(label or "media"),
        source=source_path,
        destination=destination_path,
        directories=directories,
        files=files,
        total_bytes=sum(item.size for item in files),
        manifest_sha256=_manifest_digest(directories, files),
    )


def _source_snapshot_matches(plan: TreeCopyPlan) -> bool:
    directories, files = _inventory_tree(plan.source)
    return directories == plan.directories and files == plan.files


def _content_matches_plan(root: Path, plan: TreeCopyPlan) -> bool:
    directories, files = _inventory_tree(root)
    if directories != plan.directories:
        return False
    return [
        (item.relative_path, item.size, item.sha256) for item in files
    ] == [
        (item.relative_path, item.size, item.sha256) for item in plan.files
    ]


def execute_tree_copy_plan(
    plan: TreeCopyPlan,
    *,
    reserve_bytes: int = 0,
    disk_usage: Callable[[os.PathLike[str] | str], object] = shutil.disk_usage,
) -> TreeCopyResult:
    """Copy to a new destination, verify both trees, and preserve the source."""

    if reserve_bytes < 0:
        raise MediaWorkspaceConfigurationError("Copy reserve cannot be negative.")
    if plan.destination.exists():
        raise MediaWorkspaceCopyError(
            f"Copy destination already exists: {plan.destination}"
        )
    if not _source_snapshot_matches(plan):
        raise MediaWorkspaceCopyError(
            "Copy source changed after the plan was created; create a new plan."
        )

    destination_parent = plan.destination.parent
    try:
        destination_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaWorkspaceCopyError(
            f"Unable to create copy destination parent: {destination_parent}"
        ) from exc
    usage = disk_usage(destination_parent)
    if int(getattr(usage, "free")) < plan.total_bytes + int(reserve_bytes):
        raise MediaWorkspaceCapacityError(
            "Copy destination does not have enough free space for the data and reserve."
        )

    staging = destination_parent / (
        f".{plan.destination.name}.copying-{uuid.uuid4().hex}"
    )
    try:
        staging.mkdir(mode=0o700)
        for relative in plan.directories:
            (staging / relative).mkdir(parents=True, exist_ok=True)
        for entry in plan.files:
            source_path = plan.source / entry.relative_path
            destination_path = staging / entry.relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path, follow_symlinks=False)

        if not _content_matches_plan(staging, plan):
            raise MediaWorkspaceCopyError(
                "Destination verification failed; the original source remains untouched."
            )
        if not _source_snapshot_matches(plan):
            raise MediaWorkspaceCopyError(
                "Copy source changed during the copy; the original source remains untouched."
            )
        os.replace(staging, plan.destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return TreeCopyResult(
        label=plan.label,
        source=plan.source,
        destination=plan.destination,
        file_count=len(plan.files),
        total_bytes=plan.total_bytes,
        manifest_sha256=plan.manifest_sha256,
    )


def verify_tree_copy(plan: TreeCopyPlan) -> bool:
    """Re-hash both the preserved source and copied destination."""

    return (
        plan.source.is_dir()
        and plan.destination.is_dir()
        and _source_snapshot_matches(plan)
        and _content_matches_plan(plan.destination, plan)
    )
