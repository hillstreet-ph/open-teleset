#!/usr/bin/env python3
"""Fail-closed consent, suppression, and approval checks for outreach.

The policy store is server-owned. Identifiers are represented by SHA-256 hashes so
the file and audit records do not need to contain Telegram usernames, chat IDs,
or account IDs in plaintext.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_POLICY_FILE = "./accounts/outreach_policy.json"


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
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        if not isinstance(data.get("approvals"), dict):
            return None
        if not isinstance(data.get("consents"), list):
            return None
        if not isinstance(data.get("suppressions"), list):
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

        if source == "scraped":
            return PolicyDecision(False, "scraped_source_forbidden", action, subject_ref)

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
