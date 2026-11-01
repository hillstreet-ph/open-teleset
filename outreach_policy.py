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
import fcntl
import tempfile
from contextvars import ContextVar
from contextlib import contextmanager
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_POLICY_FILE = "./accounts/outreach_policy.json"
ACTIONS = {"personal_send", "member_add", "batch_send", "scheduled_send"}
# Optional job cancellation fence, propagated through ConsentTelegramClient's
# nested dispatch so Stop/Pause also apply after its final pacing wait.
dispatch_guard = ContextVar("outreach_dispatch_guard", default=None)


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

    def record_personal_consents(self, subjects, account_id, operator_id):
        """Record an operator's declaration atomically, retaining every opt-out."""
        if not operator_id:
            raise OutreachDenied("authenticated_operator_required")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = self._load() if self.path.exists() else {
                    "version": 1, "approvals": {}, "consents": [], "suppressions": []}
                if data is None:
                    raise OutreachDenied("policy_unavailable")
                now = datetime.now(timezone.utc)
                # account_id may be a single account or the full selected set (rotation):
                # the operator's declaration covers every selected sender, so one record
                # lists them all and consent matches whichever account is used.
                account_ids = [account_id] if isinstance(account_id, str) else list(account_id)
                account_hashes = sorted({hash_identifier(a) for a in account_ids})
                for subject in subjects:
                    subject_hash = hash_identifier(subject)
                    if any(r["subject_hash"] == subject_hash and r["active"] for r in data["suppressions"]):
                        continue
                    data["consents"] = [r for r in data["consents"] if not (
                        r.get("recorded_via") == "bulk_send_form" and r["subject_hash"] == subject_hash
                        and r["account_hashes"] == account_hashes)]
                    data["consents"].append({
                        "subject_hash": subject_hash, "account_hashes": account_hashes,
                        "actions": ["personal_send"], "source": "explicit",
                        "granted_at": now.isoformat(), "expires_at": (now + timedelta(days=30)).isoformat(),
                        "recorded_via": "bulk_send_form", "operator_hash": hash_identifier(operator_id),
                    })
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                        temporary = handle.name
                        json.dump(data, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
        except OSError:
            raise OutreachDenied("policy_write_failed") from None

    @contextmanager
    def _member_document(self, create=False):
        """Serialize member grants and identity binding with existing policy writers."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._load() if self.path.exists() or not create else {
                "version": 1, "approvals": {}, "consents": [], "suppressions": []}
            if data is None:
                raise OutreachDenied("policy_unavailable")
            yield data
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                    temporary = handle.name
                    json.dump(data, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    def record_member_job_consents(self, subjects, account_id, operator_id):
        """An authenticated admin explicitly authorizes one new recipient list.

        account_id may be a single account or a list of selected accounts; the
        recorded grant then authorizes any of them to invite the recipients, so
        the worker can rotate across accounts when one hits a Telegram limit.
        """
        if not operator_id:
            raise OutreachDenied("authenticated_operator_required")
        account_ids = [account_id] if isinstance(account_id, str) else list(account_id)
        subject_hashes = list(dict.fromkeys(hash_identifier(value) for value in subjects))
        if not subject_hashes:
            raise OutreachDenied("explicit_recipients_required")
        with self._member_document(create=True) as data:
            if any(r["active"] and r["subject_hash"] in subject_hashes for r in data["suppressions"]):
                raise OutreachDenied("suppressed")
            now = datetime.now(timezone.utc)
            expires = (now + timedelta(days=30)).isoformat()
            account_hashes = sorted({hash_identifier(a) for a in account_ids})
            approval_id = "member_job_" + uuid4().hex
            data["approvals"][approval_id] = {
                "action": "member_add", "account_hashes": account_hashes,
                "subject_hashes": subject_hashes, "expires_at": expires,
                "operator_hash": hash_identifier(operator_id), "recorded_via": "member_add_form"}
            for subject_hash in subject_hashes:
                data["consents"].append({
                    "subject_hash": subject_hash, "account_hashes": account_hashes,
                    "actions": ["member_add"], "source": "explicit",
                    "granted_at": now.isoformat(), "expires_at": expires,
                    "approval_id": approval_id, "recorded_via": "member_add_form",
                    "operator_hash": hash_identifier(operator_id)})
        return approval_id

    def bind_personal_peer(self, subject, peer, account_id, operator_id):
        """Bind a resolved alias without renewing the submitting consent window."""
        if not operator_id:
            raise OutreachDenied("authenticated_operator_required")
        with self._member_document() as data:
            decision = self.authorize(action="personal_send", subject=subject,
                                      account_id=account_id, approval_id=None, _data=data)
            if not decision.allowed:
                raise OutreachDenied(decision.reason)
            peer_hash = hash_identifier(peer)
            if any(r["active"] and r["subject_hash"] == peer_hash for r in data["suppressions"]):
                raise OutreachDenied("suppressed")
            account_hash = hash_identifier(account_id)
            operator_hash = hash_identifier(operator_id)
            source = next((r for r in data["consents"] if
                           r["subject_hash"] == hash_identifier(subject)
                           and "personal_send" in r["actions"]
                           and self._account_matches(r, account_hash)
                           and r.get("recorded_via") == "bulk_send_form"
                           and r.get("operator_hash") == operator_hash
                           and self._record_is_current(r, datetime.now(timezone.utc))), None)
            if source is None:
                raise OutreachDenied("consent_missing_or_invalid")
            if not any(r["subject_hash"] == peer_hash
                       and r.get("recorded_via") == "bulk_send_form"
                       and r.get("operator_hash") == operator_hash
                       and self._account_matches(r, account_hash)
                       and r.get("granted_at") == source.get("granted_at")
                       and r.get("expires_at") == source.get("expires_at")
                       for r in data["consents"]):
                data["consents"].append(dict(source, subject_hash=peer_hash))

    def bind_member_peer(self, subject, peer, account_id, approval_id):
        """Bind the resolved user once without extending consent or authorization."""
        with self._member_document() as data:
            decision = self.authorize(action="member_add", subject=subject, account_id=account_id,
                                      approval_id=approval_id, _data=data)
            if not decision.allowed:
                raise OutreachDenied(decision.reason)
            peer_hash = hash_identifier(peer)
            if any(r["active"] and r["subject_hash"] == peer_hash for r in data["suppressions"]):
                raise OutreachDenied("suppressed")
            approval = data["approvals"][approval_id]
            # Only grants created by the member form can enroll resolved aliases.
            if approval.get("recorded_via") != "member_add_form":
                raise OutreachDenied("member_grant_invalid")
            source = next((r for r in data["consents"] if
                           r["subject_hash"] == hash_identifier(subject)
                           and r.get("approval_id") == approval_id
                           and "member_add" in r["actions"]
                           and self._account_matches(r, hash_identifier(account_id))
                           and self._record_is_current(r, datetime.now(timezone.utc))), None)
            if source is None:
                raise OutreachDenied("consent_missing_or_invalid")
            if peer_hash not in approval["subject_hashes"]:
                approval["subject_hashes"].append(peer_hash)
            if not any(r["subject_hash"] == peer_hash and r.get("approval_id") == approval_id
                       for r in data["consents"]):
                data["consents"].append(dict(source, subject_hash=peer_hash))

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
        if any("subject_hashes" in r and not _hashes(r["subject_hashes"])
               for r in data["approvals"].values()):
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
        _data: Optional[dict] = None,
    ) -> PolicyDecision:
        subject_hash = hash_identifier(subject)
        subject_ref = subject_hash[:12]
        account_ref = hash_identifier(account_id)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        if source != "explicit":
            return PolicyDecision(False, "scraped_source_forbidden", action, subject_ref)
        if action not in ACTIONS:
            return PolicyDecision(False, "action_invalid", "unknown", subject_ref)

        data = self._load() if _data is None else _data
        if data is None:
            return PolicyDecision(False, "policy_unavailable", action, subject_ref)

        for record in data["suppressions"]:
            if (
                isinstance(record, dict)
                and record.get("subject_hash") == subject_hash
                and record.get("active") is True
            ):
                return PolicyDecision(False, "suppressed", action, subject_ref)

        # Personal sends are initiated by an authenticated operator. A separate
        # manual approval reference is optional; recipient policy still applies.
        # Validate references supplied by legacy callers rather than ignoring them.
        if action != "personal_send" or approval_id:
            denied = self._check_approval(data, approval_id, action, account_ref, current_time, subject_ref)
            if denied:
                return denied

        approval = data["approvals"].get(approval_id, {})
        if "subject_hashes" in approval and subject_hash not in approval["subject_hashes"]:
            return PolicyDecision(False, "approval_subject_mismatch", action, subject_ref)
        for consent in data["consents"]:
            if (consent.get("subject_hash") == subject_hash
                    and action in consent.get("actions", [])
                    and consent.get("source") == "explicit"
                    and (consent.get("approval_id") == approval_id
                         or (approval.get("recorded_via") != "member_add_form" and not consent.get("approval_id")))
                    and self._account_matches(consent, account_ref)
                    and self._record_is_current(consent, current_time)):
                return PolicyDecision(True, "consent_current", action, subject_ref)

        return PolicyDecision(False, "consent_missing_or_invalid", action, subject_ref)

    def _check_approval(self, data, approval_id, action, account_ref, current_time, subject_ref):
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

        return None

    def validate_new_member_approval(self, approval_id):
        """Job-specific form grants cannot authorize a second submission."""
        data = self._load()
        if data is None:
            raise OutreachDenied("policy_unavailable")
        if data["approvals"].get(approval_id, {}).get("recorded_via") == "member_add_form":
            raise OutreachDenied("member_authorization_job_mismatch")

    def is_suppressed(self, subject: object) -> bool:
        """Read the opt-out ledger for one subject. An unreadable store blocks the send."""
        data = self._load()
        if data is None:
            raise OutreachDenied("policy_unavailable")
        subject_hash = hash_identifier(subject)
        return any(isinstance(record, dict) and record.get("active") is True
                   and record.get("subject_hash") == subject_hash for record in data["suppressions"])

    def select_member_approval(self, account_id: object) -> str:
        """Select an existing authorization; never create or renew one."""
        data = self._load()
        if data is None:
            raise OutreachDenied("policy_unavailable")
        current_time = datetime.now(timezone.utc)
        account_ref = hash_identifier(account_id)
        for approval_id in sorted(data["approvals"]):
            if data["approvals"][approval_id].get("recorded_via") == "member_add_form":
                continue
            if self._check_approval(data, approval_id, "member_add", account_ref, current_time, "") is None:
                return approval_id
        raise OutreachDenied("member_authorization_unavailable")

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
        # Validate one snapshot for preflight; dispatch still reloads immediately
        # before every actual send so mid-run opt-outs remain effective.
        data = self._load()
        if data is None:
            return [PolicyDecision(False, "policy_unavailable", action, hash_identifier(subject)[:12])
                    for _ in account_ids for subject in subjects]
        return [
            self.authorize(
                action=action,
                subject=subject,
                account_id=account_id,
                approval_id=approval_id,
                source=source,
                now=now,
                _data=data,
            )
            for account_id in account_ids
            for subject in subjects
        ]


outreach_policy = OutreachPolicy()


class OutreachDenied(ValueError):
    """Safe to log: contains a policy reason, never recipient or message data."""


def _rate_path():
    return Path(os.getenv("OUTREACH_RATE_DB", "./accounts/outreach_rate.sqlite3"))


def account_cooldown(account_id, seconds):
    """Persist provider restrictions across batches, processes and restarts."""
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise OutreachDenied("invalid_cooldown")
    path = _rate_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=1) as db:
            db.execute("CREATE TABLE IF NOT EXISTS cooldowns (account TEXT PRIMARY KEY, until REAL NOT NULL)")
            db.execute("INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(account) DO UPDATE SET until=MAX(until, excluded.until)",
                       (hash_identifier(account_id), time.time() + seconds))
    except (OSError, sqlite3.Error):
        raise OutreachDenied("rate_store_unavailable") from None


def _check_cooldown(db, key):
    db.execute("CREATE TABLE IF NOT EXISTS cooldowns (account TEXT PRIMARY KEY, until REAL NOT NULL)")
    row = db.execute("SELECT until FROM cooldowns WHERE account=?", (key,)).fetchone()
    if row and row[0] > time.time():
        raise OutreachDenied("account_cooldown")


def account_cooldown_remaining(account_id):
    """Read persisted provider waits without extending or clearing them."""
    path = _rate_path()
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cooldowns'").fetchone():
                return 0
            row = db.execute("SELECT until FROM cooldowns WHERE account=?", (hash_identifier(account_id),)).fetchone()
        return max(0, row[0] - time.time()) if row else 0
    except (OSError, sqlite3.Error):
        raise OutreachDenied("rate_store_unavailable") from None


def account_pacing_remaining(account_id):
    """Read a reserved dispatch slot so queue workers can yield without sleeping."""
    path = _rate_path()
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone():
                return 0
            row = db.execute("SELECT until FROM attempts WHERE account=?",
                             (hash_identifier(account_id),)).fetchone()
        return max(0, row[0] - time.time()) if row else 0
    except (OSError, sqlite3.Error):
        raise OutreachDenied("rate_store_unavailable") from None


def record_provider_restriction(account_id, exc):
    from telethon import errors
    if isinstance(exc, errors.FloodWaitError):
        account_cooldown(account_id, max(1, exc.seconds))
    elif isinstance(exc, (errors.PeerFloodError, errors.UserDeactivatedBanError,
                          errors.AuthKeyUnregisteredError, errors.FrozenMethodInvalidError)):
        account_cooldown(account_id, 86400)


async def dispatch_outreach(*, action, subject, account_id, approval_id, send, delay=None, defer_attempt=False):
    """Pace every attempt across processes, then revalidate immediately before sending.

    All workers for an account must share the mounted rate database. Failure to
    access it fails closed. The caller supplies a resolved, stable Telegram peer ID.
    """
    minimum = 0 if action == "personal_send" else 35 if action == "member_add" else 3
    delay = (3 if action == "personal_send" else minimum) if delay is None else delay
    valid_delay = (type(delay) in (int, float) and math.isfinite(delay) and delay >= minimum
                   and (action == "personal_send" or delay <= 3600))
    if not valid_delay:
        raise OutreachDenied("invalid_rate_bounds")
    if defer_attempt:
        # The real client gates and paces its native RPC after resolution/uploads.
        # Reserving here too would charge two slots for one delivery.
        from outreach_client import outreach_context
        if dispatch_guard.get():
            await dispatch_guard.get()(False)
        with outreach_context(action=action, subject=subject, account_id=account_id,
                              approval_id=approval_id, delay=delay):
            return await send()
    key = hash_identifier(account_id)
    rate_path = _rate_path()
    while True:
        if dispatch_guard.get():
            await dispatch_guard.get()(False)
        decision = outreach_policy.authorize(action=action, subject=subject,
                                             account_id=account_id, approval_id=approval_id)
        if not decision.allowed:
            raise OutreachDenied(decision.reason)
        try:
            rate_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(rate_path, timeout=1) as db:
                db.execute("CREATE TABLE IF NOT EXISTS attempts (account TEXT PRIMARY KEY, until REAL NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                _check_cooldown(db, key)
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
        with sqlite3.connect(rate_path, timeout=1) as db:
            _check_cooldown(db, key)
    except (OSError, sqlite3.Error):
        raise OutreachDenied("rate_store_unavailable") from None
    if dispatch_guard.get():
        await dispatch_guard.get()(not defer_attempt)
        # The async owner check may have waited; consent must still be current.
        decision = outreach_policy.authorize(action=action, subject=subject,
                                             account_id=account_id, approval_id=approval_id)
        if not decision.allowed:
            raise OutreachDenied(decision.reason)
        try:
            with sqlite3.connect(rate_path, timeout=1) as db:
                _check_cooldown(db, key)
        except (OSError, sqlite3.Error):
            raise OutreachDenied("rate_store_unavailable") from None
    try:
        from outreach_client import outreach_context
        with outreach_context(action=action, subject=subject, account_id=account_id,
                              approval_id=approval_id, delay=delay):
            result = await send()
    except Exception as exc:
        record_provider_restriction(account_id, exc)
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
