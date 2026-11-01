#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Evidence-based monetization eligibility, duration rules, and revenue.

There are intentionally no built-in platform payout rates or eligibility
assumptions.  Every account/program eligibility and every dated duration rule
must carry a source and ``checked_at`` timestamp.  Unknown revenue is stored as
NULL and is never silently converted to zero.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


PLATFORMS = frozenset({"youtube", "bilibili", "douyin", "tiktok", "x"})
ELIGIBILITY_STATES = frozenset(
    {"unknown", "pending", "eligible", "ineligible", "suspended"}
)
REVENUE_KINDS = frozenset({"estimated", "settled"})


@dataclass(frozen=True)
class DurationAssessment:
    account_id: str
    platform: str
    program_key: str
    result: str
    reason: str
    duration_seconds: float
    eligibility_status: str
    rule_id: str = ""
    rule_source: str = ""
    rule_checked_at: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_platform(platform: str) -> str:
    value = str(platform or "").strip().lower()
    if value not in PLATFORMS:
        raise ValueError(f"unsupported monetization platform: {value or '<empty>'}")
    return value


def _required_text(value: Any, label: str, maximum: int = 1000) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized[:maximum]


def _normalize_currency(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        raise ValueError("currency must be a three-letter ISO-style code")
    return normalized


def _normalize_region(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized and not re.fullmatch(r"[A-Z0-9-]{2,12}", normalized):
        raise ValueError("invalid account region")
    return normalized


def _timestamp(value: str, label: str) -> str:
    normalized = _required_text(value, label, 64)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _optional_timestamp(value: str | None, label: str) -> str:
    # Billing date inputs denote calendar dates, not a browser-local instant.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()):
        return _timestamp(str(value).strip() + "T00:00:00+00:00", label)
    return _timestamp(value, label) if str(value or "").strip() else ""


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("revenue amount must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError("revenue amount must be a finite decimal")
    return format(number, "f")


class MonetizationStore:
    """Append-only evidence store for account monetization and revenue."""

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
                CREATE TABLE IF NOT EXISTS monetization_accounts (
                    account_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    external_account_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    settlement_currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, external_account_id)
                );

                CREATE TABLE IF NOT EXISTS monetization_eligibility_history (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    program_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_ref TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    CHECK (status IN ('unknown', 'pending', 'eligible', 'ineligible', 'suspended')),
                    FOREIGN KEY(account_id) REFERENCES monetization_accounts(account_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_monetization_eligibility_latest
                    ON monetization_eligibility_history(account_id, program_key, checked_at DESC);

                CREATE TABLE IF NOT EXISTS monetization_duration_rules (
                    rule_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    program_key TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'video',
                    min_duration_seconds REAL,
                    max_duration_seconds REAL,
                    source TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    effective_from TEXT NOT NULL DEFAULT '',
                    effective_until TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    CHECK (min_duration_seconds IS NULL OR min_duration_seconds >= 0),
                    CHECK (max_duration_seconds IS NULL OR max_duration_seconds >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_monetization_rules_lookup
                    ON monetization_duration_rules(
                        platform, program_key, region, content_type, checked_at DESC
                    );

                CREATE TABLE IF NOT EXISTS monetization_revenue_records (
                    record_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    publication_attempt_id TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    program_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount_decimal TEXT,
                    amount_known INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    period_start TEXT NOT NULL DEFAULT '',
                    period_end TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    CHECK (kind IN ('estimated', 'settled')),
                    CHECK (amount_known IN (0, 1)),
                    CHECK (
                        (amount_known=0 AND amount_decimal IS NULL)
                        OR (amount_known=1 AND amount_decimal IS NOT NULL)
                    ),
                    FOREIGN KEY(account_id) REFERENCES monetization_accounts(account_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_monetization_revenue_lookup
                    ON monetization_revenue_records(job_id, platform, checked_at DESC);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if "amount_known" in result:
            result["amount_known"] = bool(result["amount_known"])
        return result

    def register_account(
        self,
        *,
        platform: str,
        external_account_id: str,
        settlement_currency: str,
        region: str = "",
        display_name: str = "",
        account_id: str = "",
    ) -> str:
        normalized_platform = _normalize_platform(platform)
        normalized_external_id = _required_text(
            external_account_id, "external_account_id", 300
        )
        currency = _normalize_currency(settlement_currency)
        normalized_region = _normalize_region(region)
        normalized_account_id = str(account_id or uuid.uuid4()).strip()
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT account_id FROM monetization_accounts
                WHERE platform=? AND external_account_id=?
                """,
                (normalized_platform, normalized_external_id),
            ).fetchone()
            if existing:
                normalized_account_id = str(existing["account_id"])
                connection.execute(
                    """
                    UPDATE monetization_accounts
                    SET display_name=?, region=?, settlement_currency=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        str(display_name or "").strip()[:300],
                        normalized_region,
                        currency,
                        now,
                        normalized_account_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO monetization_accounts (
                        account_id, platform, external_account_id, display_name,
                        region, settlement_currency, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_account_id,
                        normalized_platform,
                        normalized_external_id,
                        str(display_name or "").strip()[:300],
                        normalized_region,
                        currency,
                        now,
                        now,
                    ),
                )
        return normalized_account_id

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monetization_accounts WHERE account_id=?",
                (str(account_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def list_accounts(
        self, *, platform: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        """List configured accounts without inferring program eligibility."""

        normalized_platform = _normalize_platform(platform) if platform else ""
        where = "WHERE platform=?" if normalized_platform else ""
        params: list[Any] = [normalized_platform] if normalized_platform else []
        params.append(max(1, min(2000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM monetization_accounts {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def record_eligibility(
        self,
        *,
        account_id: str,
        program_key: str,
        status: str,
        source: str,
        checked_at: str,
        reason: str = "",
        evidence_ref: str = "",
    ) -> str:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError("monetization account does not exist")
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in ELIGIBILITY_STATES:
            raise ValueError("invalid monetization eligibility status")
        normalized_program = _required_text(program_key, "program_key", 200)
        normalized_source = _required_text(source, "source", 1000)
        normalized_checked_at = _timestamp(checked_at, "checked_at")
        record_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monetization_eligibility_history (
                    id, account_id, platform, program_key, status, source,
                    checked_at, reason, evidence_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    account["account_id"],
                    account["platform"],
                    normalized_program,
                    normalized_status,
                    normalized_source,
                    normalized_checked_at,
                    str(reason or "").strip()[:1000],
                    str(evidence_ref or "").strip()[:1000],
                    _utc_now(),
                ),
            )
        return record_id

    def latest_eligibility(
        self, account_id: str, program_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM monetization_eligibility_history
                WHERE account_id=? AND program_key=?
                ORDER BY checked_at DESC, created_at DESC LIMIT 1
                """,
                (
                    str(account_id or "").strip(),
                    str(program_key or "").strip(),
                ),
            ).fetchone()
        return self._row(row)

    def record_duration_rule(
        self,
        *,
        platform: str,
        program_key: str,
        source: str,
        checked_at: str,
        min_duration_seconds: float | None = None,
        max_duration_seconds: float | None = None,
        region: str = "",
        content_type: str = "video",
        effective_from: str = "",
        effective_until: str = "",
        note: str = "",
    ) -> str:
        normalized_platform = _normalize_platform(platform)
        normalized_program = _required_text(program_key, "program_key", 200)
        normalized_source = _required_text(source, "source", 1000)
        normalized_checked_at = _timestamp(checked_at, "checked_at")
        normalized_region = _normalize_region(region)
        normalized_content_type = _required_text(content_type, "content_type", 100).lower()
        if min_duration_seconds is None and max_duration_seconds is None:
            raise ValueError("a duration rule needs a minimum or maximum")
        minimum = float(min_duration_seconds) if min_duration_seconds is not None else None
        maximum = float(max_duration_seconds) if max_duration_seconds is not None else None
        if minimum is not None and minimum < 0:
            raise ValueError("minimum duration cannot be negative")
        if maximum is not None and maximum < 0:
            raise ValueError("maximum duration cannot be negative")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum duration cannot exceed maximum duration")
        starts = _optional_timestamp(effective_from, "effective_from")
        ends = _optional_timestamp(effective_until, "effective_until")
        if starts and ends and starts > ends:
            raise ValueError("effective_from cannot be after effective_until")
        rule_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monetization_duration_rules (
                    rule_id, platform, program_key, region, content_type,
                    min_duration_seconds, max_duration_seconds, source,
                    checked_at, effective_from, effective_until, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    normalized_platform,
                    normalized_program,
                    normalized_region,
                    normalized_content_type,
                    minimum,
                    maximum,
                    normalized_source,
                    normalized_checked_at,
                    starts,
                    ends,
                    str(note or "").strip()[:1000],
                    _utc_now(),
                ),
            )
        return rule_id

    def assess_duration(
        self,
        *,
        account_id: str,
        program_key: str,
        duration_seconds: float,
        content_type: str = "video",
        at: str | None = None,
    ) -> DurationAssessment:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError("monetization account does not exist")
        normalized_program = _required_text(program_key, "program_key", 200)
        duration = float(duration_seconds)
        if duration < 0:
            raise ValueError("duration cannot be negative")
        eligibility = self.latest_eligibility(account_id, normalized_program)
        eligibility_status = str((eligibility or {}).get("status") or "unknown")
        common = {
            "account_id": str(account["account_id"]),
            "platform": str(account["platform"]),
            "program_key": normalized_program,
            "duration_seconds": duration,
            "eligibility_status": eligibility_status,
        }
        if eligibility_status in {"ineligible", "suspended"}:
            return DurationAssessment(
                **common,
                result="ineligible",
                reason=f"account program status is {eligibility_status}",
            )
        if eligibility_status != "eligible":
            return DurationAssessment(
                **common,
                result="unknown",
                reason="account eligibility has not been verified as eligible",
            )
        at_value = _timestamp(at, "at") if at else _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM monetization_duration_rules
                WHERE platform=? AND program_key=? AND content_type=?
                  AND region IN (?, '')
                  AND (effective_from='' OR effective_from<=?)
                  AND (effective_until='' OR effective_until>=?)
                ORDER BY CASE WHEN region=? THEN 0 ELSE 1 END,
                         checked_at DESC, created_at DESC
                LIMIT 1
                """,
                (
                    account["platform"],
                    normalized_program,
                    str(content_type or "video").strip().lower(),
                    account["region"],
                    at_value,
                    at_value,
                    account["region"],
                ),
            ).fetchone()
        if row is None:
            return DurationAssessment(
                **common,
                result="unknown",
                reason="no sourced, current duration rule is recorded",
            )
        rule = dict(row)
        minimum = rule.get("min_duration_seconds")
        maximum = rule.get("max_duration_seconds")
        result = "eligible"
        reasons: list[str] = []
        if minimum is not None and duration < float(minimum):
            result = "ineligible"
            reasons.append(f"duration is below {float(minimum):g} seconds")
        if maximum is not None and duration > float(maximum):
            result = "ineligible"
            reasons.append(f"duration exceeds {float(maximum):g} seconds")
        return DurationAssessment(
            **common,
            result=result,
            reason="; ".join(reasons) if reasons else "duration matches the recorded rule",
            rule_id=str(rule["rule_id"]),
            rule_source=str(rule["source"]),
            rule_checked_at=str(rule["checked_at"]),
        )

    def record_revenue(
        self,
        *,
        job_id: str,
        account_id: str,
        program_key: str,
        kind: str,
        source: str,
        checked_at: str,
        amount: Any | None,
        currency: str = "",
        publication_attempt_id: str = "",
        period_start: str = "",
        period_end: str = "",
        note: str = "",
    ) -> str:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError("monetization account does not exist")
        normalized_job = _required_text(job_id, "job_id", 300)
        normalized_program = _required_text(program_key, "program_key", 200)
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in REVENUE_KINDS:
            raise ValueError("revenue kind must be estimated or settled")
        normalized_source = _required_text(source, "source", 1000)
        normalized_checked_at = _timestamp(checked_at, "checked_at")
        normalized_currency = _normalize_currency(
            currency or str(account["settlement_currency"])
        )
        amount_known = amount is not None
        amount_text = _decimal_text(amount) if amount_known else None
        if normalized_kind == "settled" and not amount_known:
            raise ValueError("settled revenue must have a known amount")
        starts = _optional_timestamp(period_start, "period_start")
        ends = _optional_timestamp(period_end, "period_end")
        if bool(starts) != bool(ends):
            raise ValueError("账期开始与结束需要同时填写，或同时留空作为累计快照")
        if starts and ends and starts > ends:
            raise ValueError("period_start cannot be after period_end")
        record_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT period_start,period_end FROM monetization_revenue_records "
                "WHERE job_id=? AND account_id=? AND program_key=? AND kind=? AND currency=?",
                (normalized_job, account["account_id"], normalized_program, normalized_kind, normalized_currency),
            ).fetchall()
            for row in prior:
                old_start, old_end = row["period_start"], row["period_end"]
                if (old_start, old_end) == (starts, ends):
                    continue  # Same period is a revised snapshot, not extra income.
                if not starts or not old_start or (starts <= old_end and ends >= old_start):
                    raise ValueError("该视频已有累计记录或重叠账期，请沿用相同账期登记修订值，不能重复累计")
            connection.execute(
                """
                INSERT INTO monetization_revenue_records (
                    record_id, job_id, publication_attempt_id, account_id,
                    platform, program_key, kind, amount_decimal, amount_known,
                    currency, period_start, period_end, source, checked_at,
                    note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    normalized_job,
                    str(publication_attempt_id or "").strip()[:300],
                    account["account_id"],
                    account["platform"],
                    normalized_program,
                    normalized_kind,
                    amount_text,
                    int(amount_known),
                    normalized_currency,
                    starts,
                    ends,
                    normalized_source,
                    normalized_checked_at,
                    str(note or "").strip()[:1000],
                    _utc_now(),
                ),
            )
        return record_id

    def get_revenue_record(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monetization_revenue_records WHERE record_id=?",
                (str(record_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def summarize_revenue(
        self,
        *,
        job_id: str = "",
        account_id: str = "",
        program_key: str = "",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("job_id", job_id),
            ("account_id", account_id),
            ("program_key", program_key),
        ):
            normalized = str(value or "").strip()
            if normalized:
                clauses.append(f"{column}=?")
                params.append(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT kind, currency, amount_decimal, amount_known FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY job_id,account_id,program_key,kind,currency,period_start,period_end
                        ORDER BY checked_at DESC,rowid DESC
                    ) AS period_rank FROM monetization_revenue_records {where}
                ) WHERE period_rank=1 ORDER BY checked_at ASC
                """,
                params,
            ).fetchall()
        currencies: dict[str, dict[str, Any]] = {}
        for row in rows:
            currency = str(row["currency"])
            bucket = currencies.setdefault(
                currency,
                {
                    "estimated_known_total": Decimal("0"),
                    "estimated_known_count": 0,
                    "estimated_unknown_count": 0,
                    "settled_known_total": Decimal("0"),
                    "settled_known_count": 0,
                },
            )
            kind = str(row["kind"])
            if not bool(row["amount_known"]):
                bucket[f"{kind}_unknown_count"] = (
                    int(bucket.get(f"{kind}_unknown_count", 0)) + 1
                )
                continue
            bucket[f"{kind}_known_total"] += Decimal(str(row["amount_decimal"]))
            bucket[f"{kind}_known_count"] += 1
        serialized: dict[str, dict[str, Any]] = {}
        for currency, bucket in currencies.items():
            serialized[currency] = {
                key: format(value, "f") if isinstance(value, Decimal) else value
                for key, value in bucket.items()
            }
        return {
            "record_count": len(rows),
            "unknown_amount_count": sum(
                not bool(row["amount_known"]) for row in rows
            ),
            "currencies": serialized,
        }
