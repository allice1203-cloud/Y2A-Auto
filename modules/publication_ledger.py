#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Durable publication attempts and read-only publication probes.

The ledger deliberately separates a platform submission from a publicly visible
post.  A caller must mark an attempt as ``dispatching`` immediately before the
first mutating platform request.  If the process exits in that state, recovery
changes it to ``unknown`` and requires remote reconciliation; it must never be
blindly uploaded again.

This module does not publish anything.  The two probe helpers only read public
YouTube/Bilibili state and accept injectable clients for deterministic tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


PLATFORMS = frozenset({"youtube", "bilibili", "douyin", "tiktok", "x"})
PUBLICATION_STATES = frozenset(
    {
        "prepared",
        "dispatching",
        "unknown",
        "submitted",
        "processing",
        "public",
        "rejected",
        "restricted",
        "failed",
    }
)
REMOTE_STATES = frozenset(
    {"submitted", "processing", "public", "rejected", "restricted"}
)
UNRESOLVED_STATES = frozenset(
    {"dispatching", "unknown", "submitted", "processing"}
)
TERMINAL_REMOTE_STATES = frozenset({"public", "rejected", "restricted"})
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|cookie|credential)",
    re.IGNORECASE,
)


class PublicationLedgerError(RuntimeError):
    """Base error for publication-ledger contract violations."""


class UnsafeRetryError(PublicationLedgerError):
    """Raised when dispatch could duplicate a platform publication."""


class ReconciliationRequiredError(UnsafeRetryError):
    """Raised when a remote outcome must be checked before another upload."""

    def __init__(self, attempt_id: str, state: str, reason: str = ""):
        self.attempt_id = attempt_id
        self.state = state
        self.reason = reason or "remote publication outcome is unresolved"
        super().__init__(
            f"publication attempt {attempt_id} is {state}; "
            f"reconciliation is required before retry: {self.reason}"
        )


class UploadNotDispatchedError(PublicationLedgerError):
    """The uploader failed before its first mutating remote request.

    Upload callables may raise this only while they can prove that no remote
    write was attempted.  The ledger then records a retry-safe local failure
    and re-raises the same exception for the caller's normal error handling.
    Every other exception remains an uncertain remote outcome.
    """


@dataclass(frozen=True)
class PublicationDecision:
    attempt_id: str
    action: str
    state: str
    request_fingerprint: str
    asset_sha256: str
    reason: str = ""

    @property
    def should_dispatch(self) -> bool:
        return self.action == "dispatch"


@dataclass(frozen=True)
class PublicationProbe:
    platform: str
    remote_id: str
    state: str
    publicly_visible: bool | None
    checked_at: str
    reason: str = ""
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublicationSubmission:
    """Normalized response returned by an upload callable."""

    remote_id: str
    state: str = "submitted"
    remote_url: str = ""
    response: Mapping[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_platform(platform: str) -> str:
    value = str(platform or "").strip().lower()
    if value not in PLATFORMS:
        raise ValueError(f"unsupported publication platform: {value or '<empty>'}")
    return value


def _redact_sensitive(value: Any) -> Any:
    """Return a JSON-compatible structure without storing credentials."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _redact_sensitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError("publication asset does not exist")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publication_fingerprint(
    *,
    job_id: str,
    platform: str,
    account_id: str,
    asset_sha256: str,
    request_payload: Mapping[str, Any] | None = None,
) -> str:
    normalized = {
        "schema": "sg99.publication-request.v1",
        "job_id": str(job_id or "").strip(),
        "platform": _normalize_platform(platform),
        "account_id": str(account_id or "").strip(),
        "asset_sha256": str(asset_sha256 or "").strip().lower(),
        "request": request_payload or {},
    }
    if not normalized["job_id"] or not normalized["account_id"]:
        raise ValueError("job_id and account_id are required")
    if not re.fullmatch(r"[0-9a-f]{64}", normalized["asset_sha256"]):
        raise ValueError("asset_sha256 must be a SHA-256 digest")
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


class PublicationLedger:
    """SQLite-backed at-most-once dispatch guard and publication state log."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _raw_connect(self) -> sqlite3.Connection:
        """Bare connection; the caller is responsible for closing it."""
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Context-managed connection that is **always closed** on exit.

        ``with sqlite3.Connection`` only commits/rolls back the transaction and
        leaves the file descriptor open, which leaks handles until the process
        hits its descriptor limit.  Yielding through this helper keeps every
        existing ``with self._connect() as connection:`` call site unchanged.
        """
        connection = self._raw_connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS publication_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    asset_path TEXT NOT NULL,
                    asset_sha256 TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    request_payload_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    remote_id TEXT NOT NULL DEFAULT '',
                    remote_url TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    dispatch_count INTEGER NOT NULL DEFAULT 0,
                    reconciliation_required INTEGER NOT NULL DEFAULT 0,
                    safe_to_retry INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    dispatch_started_at TEXT,
                    response_recorded_at TEXT,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (state IN (
                        'prepared', 'dispatching', 'unknown', 'submitted',
                        'processing', 'public', 'rejected', 'restricted', 'failed'
                    )),
                    CHECK (reconciliation_required IN (0, 1)),
                    CHECK (safe_to_retry IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS idx_publication_attempt_lookup
                    ON publication_attempts(job_id, platform, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_publication_attempt_fingerprint
                    ON publication_attempts(job_id, platform, request_fingerprint, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_publication_reconcile
                    ON publication_attempts(reconciliation_required, updated_at ASC);

                CREATE TABLE IF NOT EXISTS publication_state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL,
                    from_state TEXT NOT NULL DEFAULT '',
                    to_state TEXT NOT NULL,
                    source TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES publication_attempts(attempt_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_publication_history_attempt
                    ON publication_state_history(attempt_id, id ASC);
                """
            )

    @staticmethod
    def _decision(row: sqlite3.Row | Mapping[str, Any]) -> PublicationDecision:
        state = str(row["state"])
        safe_to_retry = bool(row["safe_to_retry"])
        if state == "prepared" or (state == "failed" and safe_to_retry):
            action = "dispatch"
            reason = "request has not reached the platform"
        elif state in UNRESOLVED_STATES:
            action = "reconcile"
            reason = "remote outcome must be reconciled before any retry"
        elif state == "public":
            action = "complete"
            reason = "publication is publicly visible"
        else:
            action = "blocked"
            reason = "terminal platform outcome requires a new reviewed request"
        return PublicationDecision(
            attempt_id=str(row["attempt_id"]),
            action=action,
            state=state,
            request_fingerprint=str(row["request_fingerprint"]),
            asset_sha256=str(row["asset_sha256"]),
            reason=reason,
        )

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("request_payload_json", "response_json"):
            try:
                result[key.removesuffix("_json")] = json.loads(result.get(key) or "{}")
            except (TypeError, ValueError):
                result[key.removesuffix("_json")] = {}
        result["reconciliation_required"] = bool(result["reconciliation_required"])
        result["safe_to_retry"] = bool(result["safe_to_retry"])
        return result

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?",
                (str(attempt_id or "").strip(),),
            ).fetchone()
        return self._row_dict(row)

    def list_reconciliation_required(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publication_attempts
                WHERE reconciliation_required=1
                ORDER BY updated_at ASC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._row_dict(row) or {} for row in rows]

    def list_latest(
        self, job_id: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Return one latest attempt per job/platform for status-page rendering."""

        normalized_job_id = str(job_id or "").strip()
        where = "WHERE job_id=?" if normalized_job_id else ""
        params: list[Any] = [normalized_job_id] if normalized_job_id else []
        params.append(max(1, min(2000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT publication_attempts.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY job_id, platform
                               ORDER BY created_at DESC, rowid DESC
                           ) AS latest_rank
                    FROM publication_attempts
                    {where}
                ) ranked
                WHERE latest_rank=1
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = self._row_dict(row) or {}
            item.pop("latest_rank", None)
            results.append(item)
        return results

    def prepare_attempt(
        self,
        *,
        job_id: str,
        platform: str,
        account_id: str,
        asset_path: str | os.PathLike[str],
        request_payload: Mapping[str, Any] | None = None,
    ) -> PublicationDecision:
        normalized_job_id = str(job_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        normalized_platform = _normalize_platform(platform)
        if not normalized_job_id or not normalized_account_id:
            raise ValueError("job_id and account_id are required")
        resolved_asset = str(Path(asset_path).expanduser().resolve())
        asset_digest = sha256_file(resolved_asset)
        safe_request_json = _canonical_json(request_payload or {})
        fingerprint = publication_fingerprint(
            job_id=normalized_job_id,
            platform=normalized_platform,
            account_id=normalized_account_id,
            asset_sha256=asset_digest,
            request_payload=json.loads(safe_request_json),
        )
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact = connection.execute(
                """
                SELECT * FROM publication_attempts
                WHERE job_id=? AND platform=? AND request_fingerprint=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (normalized_job_id, normalized_platform, fingerprint),
            ).fetchone()
            if exact is not None:
                connection.commit()
                return self._decision(exact)

            existing_remote = connection.execute(
                """
                SELECT * FROM publication_attempts
                WHERE job_id=? AND platform=?
                  AND state IN (
                      'dispatching', 'unknown', 'submitted', 'processing',
                      'public', 'restricted'
                  )
                ORDER BY updated_at DESC LIMIT 1
                """,
                (normalized_job_id, normalized_platform),
            ).fetchone()
            if existing_remote is not None:
                connection.commit()
                if str(existing_remote["state"]) in {"public", "restricted"}:
                    return self._decision(existing_remote)
                return PublicationDecision(
                    attempt_id=str(existing_remote["attempt_id"]),
                    action="reconcile",
                    state=str(existing_remote["state"]),
                    request_fingerprint=str(existing_remote["request_fingerprint"]),
                    asset_sha256=str(existing_remote["asset_sha256"]),
                    reason="an earlier request may already exist remotely",
                )

            attempt_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO publication_attempts (
                    attempt_id, job_id, platform, account_id, asset_path,
                    asset_sha256, request_fingerprint, request_payload_json,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    attempt_id,
                    normalized_job_id,
                    normalized_platform,
                    normalized_account_id,
                    resolved_asset,
                    asset_digest,
                    fingerprint,
                    safe_request_json,
                    now,
                    now,
                ),
            )
            self._append_history(
                connection,
                attempt_id,
                "",
                "prepared",
                "local",
                {"request_fingerprint": fingerprint, "asset_sha256": asset_digest},
                now,
            )
            row = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            connection.commit()
        return self._decision(row)

    @staticmethod
    def _append_history(
        connection: sqlite3.Connection,
        attempt_id: str,
        from_state: str,
        to_state: str,
        source: str,
        details: Mapping[str, Any] | None,
        changed_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO publication_state_history (
                attempt_id, from_state, to_state, source, details_json, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                from_state,
                to_state,
                str(source or "system")[:80],
                _canonical_json(details or {}),
                changed_at,
            ),
        )

    def begin_dispatch(
        self,
        attempt_id: str,
        *,
        asset_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically enter the uncertain window before the first remote write."""

        normalized_id = str(attempt_id or "").strip()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise ValueError("publication attempt does not exist")
            allowed = row["state"] == "prepared" or (
                row["state"] == "failed" and bool(row["safe_to_retry"])
            )
            if not allowed:
                raise UnsafeRetryError(
                    f"attempt is {row['state']}; reconcile it before any retry"
                )
            current_path = str(
                Path(asset_path or row["asset_path"]).expanduser().resolve()
            )
            if sha256_file(current_path) != row["asset_sha256"]:
                raise PublicationLedgerError(
                    "publication asset changed after the request fingerprint was prepared"
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE publication_attempts
                SET state='dispatching', dispatch_count=dispatch_count+1,
                    reconciliation_required=1, safe_to_retry=0,
                    dispatch_started_at=?, updated_at=?, last_error=''
                WHERE attempt_id=?
                """,
                (now, now, normalized_id),
            )
            self._append_history(
                connection,
                normalized_id,
                str(row["state"]),
                "dispatching",
                "local",
                {"dispatch_count": int(row["dispatch_count"] or 0) + 1},
                now,
            )
            updated = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?", (normalized_id,)
            ).fetchone()
            connection.commit()
        return self._row_dict(updated) or {}

    def record_platform_response(
        self,
        attempt_id: str,
        *,
        state: str,
        remote_id: str = "",
        remote_url: str = "",
        response: Mapping[str, Any] | None = None,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist an acknowledged remote response without claiming monetization."""

        normalized_state = str(state or "").strip().lower()
        if normalized_state not in REMOTE_STATES:
            raise ValueError("state must be a remote publication state")
        normalized_remote_id = str(remote_id or "").strip()
        if normalized_state in {"submitted", "processing", "public", "restricted"} and not normalized_remote_id:
            raise ValueError("remote_id is required for an acknowledged publication")
        return self._transition(
            attempt_id,
            normalized_state,
            source="platform_response",
            remote_id=normalized_remote_id,
            remote_url=remote_url,
            response=response,
            checked_at=checked_at,
            reconciliation_required=normalized_state in {"submitted", "processing"},
            safe_to_retry=False,
            allowed_from={"dispatching", "unknown", "submitted", "processing", "public", "restricted"},
        )

    def mark_outcome_unknown(
        self,
        attempt_id: str,
        error: Any,
        *,
        response: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            attempt_id,
            "unknown",
            source="uncertain_failure",
            response=response,
            last_error=str(error or "remote outcome is unknown")[:1000],
            reconciliation_required=True,
            safe_to_retry=False,
            allowed_from={"dispatching", "unknown", "submitted", "processing"},
        )

    def record_dispatch_failure(
        self,
        attempt_id: str,
        error: Any,
        *,
        definitely_not_sent: bool = False,
    ) -> dict[str, Any]:
        """Record failure; retry is safe only with explicit pre-send certainty."""

        if not definitely_not_sent:
            return self.mark_outcome_unknown(attempt_id, error)
        return self._transition(
            attempt_id,
            "failed",
            source="pre_dispatch_failure",
            last_error=str(error or "request was not sent")[:1000],
            reconciliation_required=False,
            safe_to_retry=True,
            allowed_from={"dispatching"},
        )

    def record_reconciliation(
        self,
        attempt_id: str,
        probe: PublicationProbe,
        *,
        confirmed_absent: bool = False,
    ) -> dict[str, Any]:
        """Record a read-only probe result and decide whether retry is safe."""

        current = self.get_attempt(attempt_id)
        if current is None:
            raise ValueError("publication attempt does not exist")
        if probe.platform != current["platform"]:
            raise ValueError("probe platform does not match publication attempt")
        if probe.state == "unknown":
            if confirmed_absent:
                return self._transition(
                    attempt_id,
                    "failed",
                    source="reconciliation",
                    response=probe.to_dict(),
                    checked_at=probe.checked_at,
                    last_error="authoritative reconciliation confirmed no remote publication",
                    reconciliation_required=False,
                    safe_to_retry=True,
                    allowed_from=set(UNRESOLVED_STATES) | {"failed"},
                )
            return self._transition(
                attempt_id,
                "unknown",
                source="reconciliation",
                response=probe.to_dict(),
                checked_at=probe.checked_at,
                last_error=probe.reason,
                reconciliation_required=True,
                safe_to_retry=False,
                allowed_from=set(UNRESOLVED_STATES) | {"failed"},
            )
        if probe.state not in REMOTE_STATES:
            raise ValueError("probe returned an unsupported publication state")
        return self._transition(
            attempt_id,
            probe.state,
            source="reconciliation",
            remote_id=probe.remote_id,
            response=probe.to_dict(),
            checked_at=probe.checked_at,
            last_error=probe.reason if probe.state in {"rejected", "restricted"} else "",
            reconciliation_required=probe.state in {"submitted", "processing"},
            safe_to_retry=False,
            allowed_from=set(UNRESOLVED_STATES) | {"public", "restricted", "failed"},
        )

    def _transition(
        self,
        attempt_id: str,
        to_state: str,
        *,
        source: str,
        remote_id: str = "",
        remote_url: str = "",
        response: Mapping[str, Any] | None = None,
        checked_at: str | None = None,
        last_error: str = "",
        reconciliation_required: bool,
        safe_to_retry: bool,
        allowed_from: set[str],
    ) -> dict[str, Any]:
        if to_state not in PUBLICATION_STATES:
            raise ValueError("invalid publication state")
        normalized_id = str(attempt_id or "").strip()
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise ValueError("publication attempt does not exist")
            if str(row["state"]) not in allowed_from:
                raise PublicationLedgerError(
                    f"invalid publication transition: {row['state']} -> {to_state}"
                )
            resolved_remote_id = str(remote_id or row["remote_id"] or "").strip()
            resolved_remote_url = str(remote_url or row["remote_url"] or "").strip()
            connection.execute(
                """
                UPDATE publication_attempts
                SET state=?, remote_id=?, remote_url=?, response_json=?,
                    reconciliation_required=?, safe_to_retry=?, last_error=?,
                    response_recorded_at=CASE WHEN ?='platform_response' THEN ? ELSE response_recorded_at END,
                    last_checked_at=COALESCE(?, last_checked_at), updated_at=?
                WHERE attempt_id=?
                """,
                (
                    to_state,
                    resolved_remote_id,
                    resolved_remote_url,
                    _canonical_json(response or {}),
                    int(reconciliation_required),
                    int(safe_to_retry),
                    str(last_error or "")[:1000],
                    source,
                    now,
                    checked_at,
                    now,
                    normalized_id,
                ),
            )
            self._append_history(
                connection,
                normalized_id,
                str(row["state"]),
                to_state,
                source,
                response or {"remote_id": resolved_remote_id},
                now,
            )
            updated = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id=?", (normalized_id,)
            ).fetchone()
            connection.commit()
        return self._row_dict(updated) or {}

    def recover_interrupted_attempts(self) -> int:
        """Convert crash-window attempts to unknown; never make them retryable."""

        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT attempt_id FROM publication_attempts WHERE state='dispatching'"
            ).fetchall()
            for row in rows:
                attempt_id = str(row["attempt_id"])
                connection.execute(
                    """
                    UPDATE publication_attempts
                    SET state='unknown', reconciliation_required=1, safe_to_retry=0,
                        last_error='process interrupted during remote dispatch', updated_at=?
                    WHERE attempt_id=?
                    """,
                    (now, attempt_id),
                )
                self._append_history(
                    connection,
                    attempt_id,
                    "dispatching",
                    "unknown",
                    "startup_recovery",
                    {"reason": "process interrupted during remote dispatch"},
                    now,
                )
            connection.commit()
        return len(rows)

    @staticmethod
    def _normalize_submission(value: Any, platform: str = "") -> PublicationSubmission:
        if isinstance(value, PublicationSubmission):
            submission = value
        elif isinstance(value, Mapping):
            remote_id = next(
                (
                    str(value.get(key) or "").strip()
                    for key in (
                        "remote_id",
                        "video_id",
                        "item_id",
                        "post_id",
                        "bvid",
                        "aid",
                        "id",
                        "url",
                    )
                    if str(value.get(key) or "").strip()
                ),
                "",
            )
            submission = PublicationSubmission(
                remote_id=remote_id,
                state=str(value.get("publication_state") or "submitted").strip().lower(),
                remote_url=str(value.get("remote_url") or value.get("url") or "").strip(),
                response=value,
            )
        else:
            submission = PublicationSubmission(remote_id=str(value or "").strip())
        remote_id = str(submission.remote_id or "").strip()
        remote_url = str(submission.remote_url or "").strip()
        if platform == "bilibili":
            match = re.search(r"(?:/video/)?(BV[0-9A-Za-z]{10,16})", remote_id)
            if match:
                if remote_id.startswith(("http://", "https://")) and not remote_url:
                    remote_url = remote_id
                remote_id = match.group(1)
        submission = PublicationSubmission(
            remote_id=remote_id,
            state=submission.state,
            remote_url=remote_url,
            response=submission.response,
        )
        if submission.state not in REMOTE_STATES:
            raise PublicationLedgerError(
                "upload callable returned an invalid publication_state"
            )
        if not str(submission.remote_id or "").strip():
            raise PublicationLedgerError(
                "upload callable returned no stable remote publication id"
            )
        return submission

    def dispatch_upload(
        self,
        job_id: str,
        platform: str,
        account_id: str,
        asset_path: str | os.PathLike[str],
        upload_callable: Callable[[], Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Safely wrap one mutating uploader call.

        ``upload_callable`` takes no arguments; callers can close over their
        existing uploader inputs.  Its return value may be a
        :class:`PublicationSubmission`, a response mapping containing a stable
        platform id, or a string id.  A successful API response is recorded as
        ``submitted`` by default, never as ``public``.

        Any exception after dispatch begins is conservatively recorded as
        ``unknown`` and re-raised as :class:`ReconciliationRequiredError`.
        """

        if not callable(upload_callable):
            raise ValueError("upload_callable must be callable")
        decision = self.prepare_attempt(
            job_id=job_id,
            platform=platform,
            account_id=account_id,
            asset_path=asset_path,
            request_payload=metadata,
        )
        if decision.action == "reconcile":
            raise ReconciliationRequiredError(
                decision.attempt_id, decision.state, decision.reason
            )
        if decision.action == "complete":
            return self.get_attempt(decision.attempt_id) or {}
        if not decision.should_dispatch:
            raise PublicationLedgerError(
                f"publication attempt is {decision.state}: {decision.reason}"
            )

        self.begin_dispatch(decision.attempt_id, asset_path=asset_path)
        try:
            submission = self._normalize_submission(
                upload_callable(), _normalize_platform(platform)
            )
            return self.record_platform_response(
                decision.attempt_id,
                state=submission.state,
                remote_id=submission.remote_id,
                remote_url=submission.remote_url,
                response=submission.response,
            )
        except UploadNotDispatchedError as exc:
            self.record_dispatch_failure(
                decision.attempt_id,
                exc,
                definitely_not_sent=True,
            )
            raise
        except Exception as exc:
            # Once the callable was entered, a transport exception cannot prove
            # that the remote service did not accept the request.
            self.mark_outcome_unknown(decision.attempt_id, exc)
            raise ReconciliationRequiredError(
                decision.attempt_id,
                "unknown",
                str(exc),
            ) from exc


def probe_youtube_publication(
    video_id: str,
    *,
    service: Any | None = None,
    api_key: str = "",
    session: Any | None = None,
    expected_account_id: str = "",
) -> PublicationProbe:
    """Read YouTube processing/visibility state without changing the video."""

    normalized_id = str(video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", normalized_id):
        raise ValueError("invalid YouTube video id")
    if service is not None:
        payload = (
            service.videos()
            .list(part="snippet,status,processingDetails,contentDetails", id=normalized_id)
            .execute()
        )
    else:
        if not str(api_key or "").strip():
            raise ValueError("a read-only YouTube service or API key is required")
        if session is None:
            import requests

            session = requests.Session()
        response = session.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,status,processingDetails,contentDetails",
                "id": normalized_id,
                "key": str(api_key).strip(),
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
    checked_at = _utc_now()
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not items:
        return PublicationProbe(
            platform="youtube",
            remote_id=normalized_id,
            state="unknown",
            publicly_visible=None,
            checked_at=checked_at,
            reason="video is absent from this read scope; it may be private, deleted, or unavailable",
            details={},
        )
    item = items[0] if isinstance(items[0], Mapping) else {}
    snippet = item.get("snippet") if isinstance(item.get("snippet"), Mapping) else {}
    owner_id = str(snippet.get("channelId") or "")
    if str(item.get("id") or "") != normalized_id or (expected_account_id and owner_id != expected_account_id):
        raise ValueError("平台作品编号或所属账号不匹配，不能关联为本次投稿")
    status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
    processing = (
        item.get("processingDetails")
        if isinstance(item.get("processingDetails"), Mapping)
        else {}
    )
    content = (
        item.get("contentDetails")
        if isinstance(item.get("contentDetails"), Mapping)
        else {}
    )
    upload_status = str(status.get("uploadStatus") or "").lower()
    processing_status = str(processing.get("processingStatus") or "").lower()
    privacy = str(status.get("privacyStatus") or "").lower()
    region_restriction = (
        content.get("regionRestriction")
        if isinstance(content.get("regionRestriction"), Mapping)
        else {}
    )
    reason = str(status.get("rejectionReason") or status.get("failureReason") or "")
    if upload_status in {"failed", "rejected"} or processing_status == "terminated":
        state = "rejected"
        visible: bool | None = False
    elif region_restriction:
        state = "restricted"
        visible = privacy == "public"
    elif upload_status == "processed" or processing_status == "succeeded":
        state = "public" if privacy == "public" else "restricted"
        visible = privacy == "public"
    elif upload_status in {"uploaded", "processing"} or processing_status == "processing":
        state = "processing"
        visible = False
    else:
        state = "submitted"
        visible = privacy == "public" if privacy else None
    return PublicationProbe(
        platform="youtube",
        remote_id=str(item.get("id") or normalized_id),
        state=state,
        publicly_visible=visible,
        checked_at=checked_at,
        reason=reason,
        details={
            "upload_status": upload_status,
            "processing_status": processing_status,
            "privacy_status": privacy,
            "region_restriction": _redact_sensitive(region_restriction),
            "account_id": owner_id,
        },
    )


def probe_bilibili_publication(
    bvid: str,
    *,
    session: Any | None = None,
    expected_account_id: str = "",
) -> PublicationProbe:
    """Read the Bilibili public video endpoint; absence remains unknown."""

    normalized_bvid = str(bvid or "").strip()
    if not re.fullmatch(r"BV[0-9A-Za-z]{10,16}", normalized_bvid):
        raise ValueError("invalid Bilibili bvid")
    if session is None:
        import requests

        session = requests.Session()
        session.trust_env = False
    response = session.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": normalized_bvid},
        timeout=(10, 30),
    )
    response.raise_for_status()
    payload = response.json()
    checked_at = _utc_now()
    payload = payload if isinstance(payload, Mapping) else {}
    code = int(payload.get("code", -1))
    if code != 0:
        return PublicationProbe(
            platform="bilibili",
            remote_id=normalized_bvid,
            state="unknown",
            publicly_visible=None,
            checked_at=checked_at,
            reason=str(payload.get("message") or f"Bilibili public API code {code}"),
            details={"code": code},
        )
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    returned_bvid = str(data.get("bvid") or "")
    owner = data.get("owner") if isinstance(data.get("owner"), Mapping) else {}
    owner_id = str(owner.get("mid") or "")
    if returned_bvid != normalized_bvid or (expected_account_id and owner_id != expected_account_id):
        raise ValueError("平台作品编号或所属账号不匹配，不能关联为本次投稿")
    return PublicationProbe(
        platform="bilibili",
        remote_id=returned_bvid,
        state="public",
        publicly_visible=True,
        checked_at=checked_at,
        details={
            "aid": str(data.get("aid") or ""),
            "state": data.get("state"),
            "account_id": owner_id,
        },
    )
