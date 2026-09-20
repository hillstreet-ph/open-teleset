#!/usr/bin/env python3
"""Fail-closed consent, suppression, and approval checks for outreach.

The policy store is server-owned. Identifiers are represented by SHA-256 hashes so
the file and audit records do not need to contain Telegram usernames, chat IDs,
or account IDs in plaintext.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_POLICY_FILE = "./accounts/outreach_policy.json"
ACTIONS = {"personal_send", "member_add", "batch_send", "scheduled_send"}


def valid_bounds(count, maximum, delay, minimum):
    return (type(count) is int and 1 <= count <= maximum
            and type(delay) in (int, float) and math.isfinite(delay)
            and minimum <= delay <= 3600)


def _hashes(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(x, str) and re.fullmatch(r"[a-f0-9]{64}", x) for x in value))


def hash_identifier(value: object) -> str:
    """Return the stable, normalized identifier used by the policy store."""
    normalized = str(value).strip().lower().lstrip("@")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    action: str
    subject_ref: str

    def audit_summary(self) -> str:
        outcome = "allow" if self.allowed else "deny"
        return f"outreach_policy {outcome} action={self.action} reason={self.reason} subject={self.subject_ref}"


class OutreachPolicy:
    """Read and evaluate a versioned server-side outreach policy document."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.getenv("OUTREACH_POLICY_FILE", DEFAULT_POLICY_FILE))

    def _load(self) -> Optional[dict]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            return None
        if not isinstance(data.get("approvals"), dict):
            return None
        if not isinstance(data.get("consents"), list):
            return None
        if not isinstance(data.get("suppressions"), list):
            return None
        for key, record in data["approvals"].items():
            if (not isinstance(key, str) or not key.strip() or not isinstance(record, dict)
                    or not isinstance(record.get("action"), str) or record["action"] not in ACTIONS
                    or _parse_time(record.get("expires_at")) is None
                    or not _hashes(record.get("account_hashes"))):
                return None
        for record in data["consents"]:
            if (not isinstance(record, dict) or not _hashes([record.get("subject_hash")])
                    or not _hashes(record.get("account_hashes"))
                    or not isinstance(record.get("actions"), list) or not record["actions"]
                    or any(not isinstance(a, str) or a not in ACTIONS for a in record["actions"])
                    or record.get("source") != "explicit"
                    or _parse_time(record.get("granted_at")) is None
                    or _parse_time(record.get("expires_at")) is None):
                return None
        for record in data["suppressions"]:
            if (not isinstance(record, dict) or not _hashes([record.get("subject_hash")])
                    or type(record.get("active")) is not bool):
                return None
        return data

    @staticmethod
    def _record_is_current(record: dict, now: datetime) -> bool:
        granted_at = _parse_time(record.get("granted_at"))
        expires_at = _parse_time(record.get("expires_at"))
        return bool(granted_at and expires_at and granted_at <= now < expires_at)

    @staticmethod
    def _account_matches(record: dict, account_ref: str) -> bool:
        allowed = record.get("account_hashes")
        return isinstance(allowed, list) and account_ref in allowed

    def authorize(
        self,
        *,
        action: str,
        subject: object,
        account_id: object,
        approval_id: Optional[str],
        source: str = "explicit",
        now: Optional[datetime] = None,
    ) -> PolicyDecision:
        subject_hash = hash_identifier(subject)
        subject_ref = subject_hash[:12]
        account_ref = hash_identifier(account_id)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        if source != "explicit":
            return PolicyDecision(False, "scraped_source_forbidden", action, subject_ref)
        if action not in ACTIONS:
            return PolicyDecision(False, "action_invalid", "unknown", subject_ref)

        data = self._load()
        if data is None:
            return PolicyDecision(False, "policy_unavailable", action, subject_ref)

        for record in data["suppressions"]:
            if (
                isinstance(record, dict)
                and record.get("subject_hash") == subject_hash
                and record.get("active") is True
            ):
                return PolicyDecision(False, "suppressed", action, subject_ref)

        if not isinstance(approval_id, str) or not approval_id.strip():
            return PolicyDecision(False, "approval_missing", action, subject_ref)
        approval = data["approvals"].get(approval_id)
        if not isinstance(approval, dict):
            return PolicyDecision(False, "approval_unknown", action, subject_ref)
        if approval.get("action") != action:
            return PolicyDecision(False, "approval_action_mismatch", action, subject_ref)
        approval_expiry = _parse_time(approval.get("expires_at"))
        if approval_expiry is None or current_time >= approval_expiry:
            return PolicyDecision(False, "approval_expired", action, subject_ref)
        if not self._account_matches(approval, account_ref):
            return PolicyDecision(False, "approval_account_mismatch", action, subject_ref)

        for consent in data["consents"]:
            if not isinstance(consent, dict):
                continue
            if consent.get("subject_hash") != subject_hash:
                continue
            if action not in consent.get("actions", []):
                continue
            if consent.get("source") != "explicit":
                continue
            if not self._account_matches(consent, account_ref):
                continue
            if self._record_is_current(consent, current_time):
                return PolicyDecision(True, "consent_current", action, subject_ref)

        return PolicyDecision(False, "consent_missing_or_invalid", action, subject_ref)

    def authorize_many(
        self,
        *,
        action: str,
        subjects: Iterable[object],
        account_ids: Iterable[object],
        approval_id: Optional[str],
        source: str = "explicit",
        now: Optional[datetime] = None,
    ) -> list[PolicyDecision]:
        subjects, account_ids = list(subjects), list(account_ids)
        return [
            self.authorize(
                action=action,
                subject=subject,
                account_id=account_id,
                approval_id=approval_id,
                source=source,
                now=now,
            )
            for account_id in account_ids
            for subject in subjects
        ]


outreach_policy = OutreachPolicy()


class OutreachDenied(ValueError):
    """Safe to log: contains a policy reason, never recipient or message data."""


async def dispatch_outreach(*, action, subject, account_id, approval_id, send, delay=None):
    """Pace every attempt across processes, then revalidate immediately before sending.

    All workers for an account must share the mounted rate database. Failure to
    access it fails closed. The caller supplies a resolved, stable Telegram peer ID.
    """
    minimum = 35 if action == "member_add" else 3
    delay = minimum if delay is None else delay
    if not valid_bounds(1, 1, delay, minimum):
        raise OutreachDenied("invalid_rate_bounds")
    key = hash_identifier(account_id)
    rate_path = Path(os.getenv("OUTREACH_RATE_DB", "./accounts/outreach_rate.sqlite3"))
    while True:
        decision = outreach_policy.authorize(action=action, subject=subject,
                                             account_id=account_id, approval_id=approval_id)
        if not decision.allowed:
            raise OutreachDenied(decision.reason)
        try:
            rate_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(rate_path, timeout=1) as db:
                db.execute("CREATE TABLE IF NOT EXISTS attempts (account TEXT PRIMARY KEY, until REAL NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                row = db.execute("SELECT until FROM attempts WHERE account=?", (key,)).fetchone()
                wait = max(0, row[0] - now) if row else 0
                if not wait:
                    db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?)", (key, now + delay))
        except (OSError, sqlite3.Error):
            raise OutreachDenied("rate_store_unavailable") from None
        if not wait:
            break
        await asyncio.sleep(min(wait, 35))
    decision = outreach_policy.authorize(action=action, subject=subject,
                                         account_id=account_id, approval_id=approval_id)
    if not decision.allowed:
        raise OutreachDenied(decision.reason)
    try:
        from outreach_client import outreach_context
        with outreach_context(action=action, subject=subject, account_id=account_id,
                              approval_id=approval_id, delay=delay):
            result = await send()
    except Exception as exc:
        logging.getLogger("outreach.audit").warning(
            "outreach_attempt action=%s account=%s subject=%s outcome=%s",
            action, key[:12], decision.subject_ref, type(exc).__name__,
        )
        raise
    logging.getLogger("outreach.audit").info(
        "outreach_attempt action=%s account=%s subject=%s outcome=sent",
        action, key[:12], decision.subject_ref,
    )
    return result
