"""Durable, owner-scoped outreach jobs; one dispatcher per mounted state volume.

Never replays uncertain delivery. Provider waits pin the recipient and pause the
whole job; rotation is assignment only, never a restriction fallback.
"""
import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator
from telethon import errors, functions, types, utils

from open_teleset.crypto.cipher import SessionCipher
from open_teleset.security import _load_profile
from outreach_policy import (
    OutreachDenied,
    account_cooldown_remaining,
    account_pacing_remaining,
    dispatch_guard,
    dispatch_outreach,
    hash_identifier,
    record_provider_restriction,
)
from telegram_ops import (
    AddMembersRequest,
    BulkSendRequest,
    _enforce_outreach,
    account_manager,
    matches_activity,
    outreach_policy,
)

router = APIRouter()
ACTIVE = ("queued", "running", "waiting")
# Per-recipient authorization failures for a personal send: skipping the recipient
# means we never message someone who hasn't consented or has opted out, so the job
# continues safely instead of pausing the whole list for one recipient.
RECIPIENT_SKIP_REASONS = frozenset({"consent_missing_or_invalid", "suppressed", "approval_subject_mismatch"})
TELEGRAM_READ_TIMEOUT = 30
TELEGRAM_SEND_TIMEOUT = 60
log = logging.getLogger(__name__)


class Interrupted(Exception):
    pass


class SenderChanged(Exception):
    pass


class RecipientChanged(Exception):
    pass


class BulkJobRequest(BulkSendRequest):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class MemberJobRequest(AddMembersRequest):
    consent_confirmed: bool = Field(default=False, strict=True)
    # Durable jobs process the complete explicit list. Legacy callers can still
    # opt into a run cap; the synchronous endpoint keeps its existing bounds.
    limit: int = Field(default=0, ge=0, strict=True)

    @field_validator("usernames")
    @classmethod
    def bounded_recipient_payload(cls, value):
        if value and sum(len(target) + 1 for target in value) > 2 * 1024 * 1024:
            raise ValueError("Recipient list must be at most 2 MB")
        return value

    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


def owner(request):
    user = getattr(request.state, "user", None)
    if not user or not user.id:
        raise HTTPException(403, "Authenticated operator required")
    return user.id


class JobStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("OUTREACH_JOBS_DB", "./accounts/outreach_jobs.sqlite3"))

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    retry_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
                    UNIQUE(owner, request_id));
                CREATE TABLE IF NOT EXISTS items (
                    job TEXT NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL,
                    payload TEXT NOT NULL, peer TEXT, state TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(job, seq));
                CREATE TABLE IF NOT EXISTS message_events (
                    job TEXT NOT NULL, seq INTEGER NOT NULL, account TEXT NOT NULL,
                    date TEXT NOT NULL, week TEXT NOT NULL, created TEXT NOT NULL,
                    PRIMARY KEY(job, seq));
                CREATE TABLE IF NOT EXISTS message_totals (
                    account_hash TEXT PRIMARY KEY, account TEXT NOT NULL,
                    total INTEGER NOT NULL, first_created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS message_daily (
                    account_hash TEXT NOT NULL, date TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(account_hash, date));
                CREATE TABLE IF NOT EXISTS message_weekly (
                    account_hash TEXT NOT NULL, week TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(account_hash, week));
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            self._backfill_message_aggregates(db)
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def pack(value):
        return SessionCipher().encrypt(json.dumps(value))

    @staticmethod
    def unpack(value):
        return json.loads(SessionCipher().decrypt(value))

    @staticmethod
    def _increment_message_aggregates(db, account, account_cipher, date, week, created):
        account_hash = hash_identifier(account)
        db.execute("INSERT INTO message_totals VALUES(?,?,1,?) "
                   "ON CONFLICT(account_hash) DO UPDATE SET total=total+1, "
                   "first_created=MIN(first_created,excluded.first_created)",
                   (account_hash, account_cipher, created))
        db.execute("INSERT INTO message_daily VALUES(?,?,1) "
                   "ON CONFLICT(account_hash,date) DO UPDATE SET count=count+1",
                   (account_hash, date))
        db.execute("INSERT INTO message_weekly VALUES(?,?,1) "
                   "ON CONFLICT(account_hash,week) DO UPDATE SET count=count+1",
                   (account_hash, week))

    def _backfill_message_aggregates(self, db):
        marker = db.execute("SELECT 1 FROM store_metadata WHERE key='message_aggregates_v1'").fetchone()
        if marker:
            return
        db.execute("BEGIN IMMEDIATE")
        marker = db.execute("SELECT 1 FROM store_metadata WHERE key='message_aggregates_v1'").fetchone()
        if marker:
            db.commit()
            return
        for event in db.execute("SELECT account,date,week,created FROM message_events"):
            self._increment_message_aggregates(
                db, self.unpack(event["account"]), event["account"],
                event["date"], event["week"], event["created"])
        db.execute("INSERT INTO store_metadata VALUES('message_aggregates_v1','complete')")
        db.commit()

    def submit(self, kind, operator, request, targets, accounts):
        payload = request.model_dump(exclude={"request_id"})
        fingerprint = hashlib.sha256(json.dumps([kind, payload], sort_keys=True).encode()).hexdigest()
        owner_hash = hash_identifier(operator)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE owner=? AND request_id=?",
                             (owner_hash, request.request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Submission key already belongs to different input")
                return old["id"]
            identities = {}
            for account in accounts:
                identity = account_manager.accounts.get(account, {}).get("user_id")
                if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
                    raise HTTPException(422, "Selected account has no verified Telegram identity")
                identities[account] = identity
            if kind == "personal_send":
                if request.template_id:
                    from template_manager import template_manager
                    now = datetime.now()
                    payload["message"] = template_manager.render_template(
                        request.template_id, time=now.strftime("%H:%M"), date=now.strftime("%Y-%m-%d")) or request.message
                message = payload["message"]
                if len(message) > 4096 or (not message.strip() and any(t not in request.messages for t in targets)):
                    raise HTTPException(400, "A valid message is required")
            # Resolve only for new jobs and freeze the selected authorization.
            # Idempotent retries above must not reselect or renew authorization.
            policy_targets = [t for t in targets if re.fullmatch(r"(?:[a-z][a-z0-9_]{3,31}|[1-9][0-9]{0,19})", t)] if kind == "member_add" else targets
            if kind == "member_add" and request.approval_id is not None:
                outreach_policy.validate_new_member_approval(request.approval_id)
            if kind == "member_add" and request.approval_id is None:
                if request.consent_confirmed:
                    payload["approval_id"] = outreach_policy.record_member_job_consents(policy_targets, accounts[0], operator)
                    payload["member_form_grant"] = True
                else:
                    payload["approval_id"] = outreach_policy.select_member_approval(accounts[0])
            payload["sender_identities"] = identities
            # Submission records declarations only once. Resume never renews consent.
            if kind == "personal_send" and request.consent_confirmed:
                if len(accounts) > 1 and getattr(request, "rotation_mode", "single") == "round_robin":
                    # The operator confirmed consent for the whole selected set, so record
                    # each recipient's consent for every selected account and let the worker
                    # send through whichever account is available (true rotation).
                    outreach_policy.record_personal_consents(targets, accounts, operator)
                    payload["rotate_any_account"] = True
                else:
                    for i, account in enumerate(accounts):
                        outreach_policy.record_personal_consents(targets[i::len(accounts)], account, operator)
            for account in accounts:
                _enforce_outreach(kind, policy_targets, account, payload["approval_id"])
            job = str(uuid4())
            payload["operator"] = operator
            payload.pop("targets", None)
            payload.pop("usernames", None)
            db.execute("INSERT INTO jobs(id,owner,request_id,fingerprint,kind,payload,state,created) VALUES(?,?,?,?,?,?,?,?)",
                       (job, owner_hash, request.request_id, fingerprint, kind,
                        self.pack(payload), "queued", time.time()))
            seen = set()
            for seq, target in enumerate(targets):
                invalid = kind == "member_add" and not re.fullmatch(r"(?:[a-z][a-z0-9_]{3,31}|[1-9][0-9]{0,19})", target)
                duplicate = target in seen
                seen.add(target)
                db.execute("INSERT INTO items(job,seq,payload,state,reason) VALUES(?,?,?,?,?)",
                           (job, seq, self.pack({"target": target, "account": accounts[seq % len(accounts)]}),
                            "skipped" if invalid or duplicate else "pending",
                            "invalid_username" if invalid else "duplicate_recipient" if duplicate else ""))
            return job

    def snapshot(self, job, operator):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?",
                             (job, hash_identifier(operator))).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            counts = {r["state"]: r["n"] for r in db.execute(
                "SELECT state,COUNT(*) n FROM items WHERE job=? GROUP BY state", (job,))}
            results = []
            next_item = db.execute("SELECT payload FROM items WHERE job=? AND state IN ('pending','sending') "
                                   "ORDER BY seq LIMIT 1", (job,)).fetchone()
            # Bounded response with full counts; all outcomes remain in the database.
            for item in db.execute("SELECT * FROM items WHERE job=? ORDER BY seq DESC LIMIT 100", (job,)):
                p = self.unpack(item["payload"])
                results.append({"target": p["target"], "account_id": p["account"],
                                "state": item["state"], "reason": item["reason"]})
            return {"id": job, "kind": row["kind"], "state": row["state"], "reason": row["reason"],
                    "retry_at": row["retry_at"],
                    "next_account_id": self.unpack(next_item[0])["account"] if next_item else None,
                    "retry_after": max(0, row["retry_at"] - time.time()), "total": sum(counts.values()),
                    "completed": counts.get("completed", 0), "skipped": counts.get("skipped", 0),
                    "failed": counts.get("failed", 0), "uncertain": counts.get("uncertain", 0),
                    "pending": counts.get("pending", 0), "in_flight": counts.get("sending", 0),
                    "results": list(reversed(results)), "created": row["created"]}

    def listing(self, operator):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE owner=? AND (state NOT IN ('completed','stopped','needs_review') "
                                           "OR id IN (SELECT id FROM jobs WHERE owner=? AND state IN ('completed','stopped','needs_review') "
                                           "ORDER BY created DESC LIMIT 30)) ORDER BY created DESC",
                                           (hash_identifier(operator), hash_identifier(operator)))]
        return [self.snapshot(j, operator) for j in ids]

    def control(self, job, operator, action):
        self.snapshot(job, operator)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            reason = "operator_" + action
            if action == "resume":
                if row["state"] != "paused":
                    raise HTTPException(409, "Only paused jobs can resume; uncertain delivery requires review")
                state = "queued"
                item = db.execute("SELECT payload,reason FROM items WHERE job=? AND state='pending' "
                                  "ORDER BY seq LIMIT 1", (job,)).fetchone()
                if item:
                    remaining = account_cooldown_remaining(self.unpack(item["payload"])["account"])
                    if remaining:
                        db.execute("UPDATE jobs SET retry_at=MAX(retry_at,?) WHERE id=?",
                                   (time.time() + remaining, job))
                        state, reason = "waiting", "telegram_cooldown"
                    elif row["retry_at"] > time.time():
                        state = "waiting"
                        reason = item["reason"] if item["reason"] in ("telegram_timeout", "account_unavailable") else "scheduled_wait"
            elif row["state"] in ("completed", "stopped", "needs_review"):
                raise HTTPException(409, "Job is already terminal or requires review")
            else:
                state = "paused" if action == "pause" else "stopped"
            # Keep retry_at: user resume cannot shorten an existing cooldown.
            db.execute("UPDATE jobs SET state=?,reason=? WHERE id=?", (state, reason, job))
        return self.snapshot(job, operator)

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='needs_review',reason='delivery_uncertain_after_restart' "
                       "WHERE id IN (SELECT job FROM items WHERE state='sending')")
            db.execute("UPDATE items SET state='uncertain',reason='delivery_uncertain_after_restart' WHERE state='sending'")

    def guard(self, job, seq, mark=False):
        with self.db() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] not in ACTIVE:
                raise Interrupted()
            if mark:
                db.execute("UPDATE items SET state='sending' WHERE job=? AND seq=?", (job, seq))

    def outcome(self, job, seq, state, reason="", delay=0, job_state=None):
        with self.db() as db:
            db.execute("UPDATE items SET state=?,reason=? WHERE job=? AND seq=?", (state, reason, job, seq))
            if state == "completed":
                kind = db.execute("SELECT kind FROM jobs WHERE id=?", (job,)).fetchone()[0]
                if kind == "personal_send":
                    item = db.execute("SELECT payload FROM items WHERE job=? AND seq=?", (job, seq)).fetchone()
                    now = datetime.now()
                    account = self.unpack(item[0])["account"]
                    account_cipher = self.pack(account)
                    date = now.strftime("%Y-%m-%d")
                    week = (now - timedelta(days=now.weekday())).strftime("%Y-W%W")
                    created = now.isoformat()
                    inserted = db.execute("INSERT OR IGNORE INTO message_events VALUES(?,?,?,?,?,?)",
                                          (job, seq, account_cipher, date, week, created)).rowcount
                    if inserted:
                        self._increment_message_aggregates(
                            db, account, account_cipher, date, week, created)
            db.execute("UPDATE jobs SET retry_at=? WHERE id=?", (time.time() + delay, job))
            if job_state:
                # Never undo a concurrent manual Stop/Pause.
                db.execute("UPDATE jobs SET state=?,reason=? WHERE id=? AND state IN ('queued','running','waiting')",
                           (job_state, reason, job))

    def waiting(self, job, reason, seconds):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='waiting',reason=?,retry_at=? WHERE id=? "
                       "AND state IN ('queued','running','waiting')", (reason, time.time() + seconds, job))


store = JobStore()


# errno values that mean the state-volume filesystem cannot provide flock at
# all (seen on some container / network-backed mounts), as opposed to the lock
# simply being held by another live dispatcher (EAGAIN/EWOULDBLOCK, surfaced as
# BlockingIOError). On an "unsupported" error we degrade to running without the
# advisory lock rather than disabling outreach entirely.
LOCK_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "ENOLCK", None), getattr(errno, "EINVAL", None),
        getattr(errno, "EOPNOTSUPP", None), getattr(errno, "ENOTSUP", None),
        getattr(errno, "ENOSYS", None), getattr(errno, "EPERM", None),
    ) if code is not None
)


class JobWorker:
    def __init__(self, storage):
        self.store = storage
        self.task = None
        self.lock = None
        self.error = None

    def start(self):
        if self.task and not self.task.done():
            return
        # A prior run may have finished or crashed while still holding the lock
        # file descriptor. Release it before re-acquiring so a restart in the
        # same process does not deadlock against its own stale flock.
        if self.lock:
            self.lock.close()
            self.lock = None
        try:
            self.store.path.parent.mkdir(parents=True, exist_ok=True)
            self.lock = self.store.path.with_suffix(".worker.lock").open("a")
            self._lock_or_degrade()
            self.store.recover()
            self.task = asyncio.create_task(self.run())
            self.error = None
        except (OSError, sqlite3.Error):
            self.error = "worker_unavailable"
            if self.lock:
                self.lock.close()
                self.lock = None

    def _lock_or_degrade(self):
        """Take the single-dispatcher advisory lock, best effort.

        A held lock (another live dispatcher) must keep us down so sends are
        never duplicated. But some state-volume filesystems cannot provide
        flock at all; refusing to run there would disable outreach entirely, so
        on an "unsupported" error we log and run without the lock. Exactly-once
        delivery still holds on a single replica via the item state machine and
        the message_events unique key.
        """
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise  # Another dispatcher holds it — stay unavailable on purpose.
        except OSError as exc:
            if exc.errno not in LOCK_UNSUPPORTED_ERRNOS:
                raise
            log.warning(
                "Outreach worker advisory lock unavailable on %s (errno %s); "
                "running without it on the assumption of a single replica",
                self.store.path, exc.errno)

    def ensure_running(self):
        """Restart a dispatcher that never started or has since exited.

        Lets a worker that lost its initial flock race (e.g. a prior container
        had not released it yet during a rolling redeploy) or that exited
        recover on the next poll or submission, instead of staying unavailable
        until a full redeploy. Returns True only when it is processing.
        """
        if not self.task or self.task.done():
            self.start()
        return bool(self.task and not self.task.done() and not self.error)

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # Expected after cancelling our dispatcher during graceful shutdown.
                pass
            self.task = None
        if self.lock:
            self.lock.close()
            self.lock = None

    async def run(self):
        while True:
            try:
                await self.tick()
                self.error = None
            except asyncio.CancelledError:
                # Graceful shutdown cancels the dispatcher; let it propagate.
                raise
            except Exception as exc:
                # The dispatcher must never die on a single bad tick. Fail
                # closed, retain the queue, surface the failure to the operator,
                # and keep looping so the next tick can recover on its own.
                self.error = type(exc).__name__
                log.exception("Outreach worker tick failed: %s", self.error)
            await asyncio.sleep(1)

    async def tick(self):
        with self.store.db() as db:
            jobs = db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','waiting') "
                              "AND retry_at<=? ORDER BY created", (time.time(),)).fetchall()
        for job in jobs:
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One job must never stall the dispatcher or the rest of the
                # queue. An unexpected error (outside process()'s own handlers)
                # pauses just this job for operator review; healthy and new
                # jobs keep running and the worker stays available.
                log.exception("Outreach job %s processing failed; pausing for review", job["id"])
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET state='needs_review',reason=? WHERE id=? "
                               "AND state IN ('queued','running','waiting')",
                               (f"job_error:{type(exc).__name__}"[:120], job["id"]))

    async def process(self, job):
        job_id = job["id"]
        config = self.store.unpack(job["payload"])
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM items WHERE job=? AND state IN ('sending','uncertain')", (job_id,)).fetchone():
                db.execute("UPDATE jobs SET state='needs_review',reason='delivery_uncertain' WHERE id=?", (job_id,))
                db.execute("UPDATE items SET state='uncertain' WHERE job=? AND state='sending'", (job_id,))
                return
            pending = db.execute("SELECT * FROM items WHERE job=? AND state='pending' ORDER BY seq", (job_id,)).fetchall()
            if not pending:
                db.execute("UPDATE jobs SET state='completed',reason='' WHERE id=? "
                           "AND state IN ('queued','running','waiting')", (job_id,))
                return
            current = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current[0] not in ACTIVE:
                return
            # Determine which selected accounts are ready to send right now. One
            # account's timed cooldown must not stall the whole job; wait only when
            # every selected account is cooling down.
            senders = list(config.get("sender_identities") or {})
            ready, soonest, soonest_reason = set(), None, "telegram_cooldown"
            try:
                for sender in senders:
                    cooldown = account_cooldown_remaining(sender)
                    pacing = account_pacing_remaining(sender)
                    hold = max(cooldown, pacing)
                    if hold <= 0:
                        ready.add(sender)
                    elif soonest is None or hold < soonest:
                        soonest, soonest_reason = hold, ("telegram_cooldown" if cooldown >= pacing else "account_pacing")
            except OutreachDenied as exc:
                # The shared rate store must be readable to schedule safely; fail
                # closed by pausing for review rather than sending blind.
                db.execute("UPDATE jobs SET state='paused',reason=? WHERE id=? "
                           "AND state IN ('queued','running','waiting')", (str(exc), job_id))
                return
            item, rotated_account = None, None
            if ready:
                if config.get("rotate_any_account"):
                    # True rotation: the operator consented to the whole selected set,
                    # so send the earliest pending recipient through whichever account
                    # is ready, cycling across them to spread load. The recipient is
                    # re-pinned to that account for this attempt.
                    ready_order = [a for a in senders if a in ready]
                    item = pending[0]
                    rotated_account = ready_order[item["seq"] % len(ready_order)]
                else:
                    # Assignment-only: a recipient keeps its consented account; send the
                    # earliest pending one whose assigned account is ready.
                    for row in pending:
                        if self.store.unpack(row["payload"])["account"] in ready:
                            item = row
                            break
            if item is None:
                self.store.waiting(job_id, soonest_reason, max(1, soonest or 30))
                return
            db.execute("UPDATE jobs SET state='running',reason='' WHERE id=?", (job_id,))
            if rotated_account is not None:
                rotated = self.store.unpack(item["payload"])
                if rotated.get("account") != rotated_account:
                    rotated["account"] = rotated_account
                    db.execute("UPDATE items SET payload=? WHERE job=? AND seq=?",
                               (self.store.pack(rotated), job_id, item["seq"]))
                    item = db.execute("SELECT * FROM items WHERE job=? AND seq=?", (job_id, item["seq"])).fetchone()
        recipient = self.store.unpack(item["payload"])
        seq, account, target = item["seq"], recipient["account"], recipient["target"]
        attempted = False
        token = None
        try:
            # The creating operator must still have administrator access after
            # a browser closes, a restart, or a long cooldown.
            await _load_profile(config["operator"])
            remaining = account_cooldown_remaining(account)
            if remaining:
                self.store.waiting(job_id, "telegram_cooldown", remaining)
                return
            remaining = account_pacing_remaining(account)
            if remaining:
                self.store.waiting(job_id, "account_pacing", remaining)
                return
            client = await asyncio.wait_for(account_manager.get_client(account), TELEGRAM_READ_TIMEOUT)
            if not client:
                self.store.waiting(job_id, "account_unavailable", max(30, account_cooldown_remaining(account)))
                return
            async def check_sender():
                expected = config.get("sender_identities", {}).get(account)
                me = await asyncio.wait_for(client.get_me(), TELEGRAM_READ_TIMEOUT)
                if not expected or not me or me.id != expected:
                    raise SenderChanged()

            await check_sender()
            # Telethon raises KeyError from its entity cache for some
            # unresolvable usernames/IDs; treat it as a resolution failure
            # (skip this recipient) instead of letting it crash the dispatcher.
            resolution_errors = (ValueError, KeyError, errors.UsernameInvalidError,
                                 errors.UsernameNotOccupiedError, errors.UserIdInvalidError,
                                 errors.InputUserDeactivatedError)
            candidates = []
            durable = recipient.get("input_peer")
            if isinstance(durable, dict) and durable.get("kind") == "user":
                try:
                    candidates.append(types.InputPeerUser(
                        user_id=int(durable["id"]), access_hash=int(durable["access_hash"])))
                except (KeyError, TypeError, ValueError):
                    # Fall back to the original target when cached peer fields are incomplete.
                    pass
            candidates.append(int(target) if str(target).isdigit() else target)
            entity = None
            for value in candidates:
                try:
                    entity = await asyncio.wait_for(client.get_entity(value), TELEGRAM_READ_TIMEOUT)
                    break
                except resolution_errors:
                    continue
            if entity is None:
                self.store.outcome(job_id, seq, "skipped", "recipient_not_found")
                return
            if not isinstance(entity, types.User) or entity.deleted:
                reason = "recipient_deleted" if isinstance(entity, types.User) else "recipient_not_user"
                self.store.outcome(job_id, seq, "skipped", reason)
                return
            peer = utils.get_peer_id(entity)
            if "peer_id" in recipient and recipient["peer_id"] != peer:
                raise RecipientChanged()
            peer_hash = hash_identifier(peer)
            with self.store.db() as db:
                duplicate = db.execute("SELECT 1 FROM items WHERE job=? AND peer=? AND seq!=? "
                                       "AND state IN ('completed','skipped','uncertain')", (job_id, peer_hash, seq)).fetchone()
            reason = ("duplicate_recipient" if duplicate else
                      "bot_excluded" if config["filter_bots"] and getattr(entity, "bot", False) else
                      "activity_not_matched" if not matches_activity(entity, config["activity_filter"]) else None)
            if reason:
                self.store.outcome(job_id, seq, "skipped", reason)
                return
            if job["kind"] == "personal_send" and config["consent_confirmed"] and "peer_id" not in recipient:
                decision = outreach_policy.authorize(action="personal_send", subject=target, account_id=account,
                                                       approval_id=config["approval_id"])
                if not decision.allowed:
                    raise OutreachDenied(decision.reason)
                outreach_policy.bind_personal_peer(target, peer, account, config["operator"])
            if job["kind"] == "member_add" and config.get("member_form_grant") and "peer_id" not in recipient:
                outreach_policy.bind_member_peer(target, peer, account, config["approval_id"])
            recipient["peer_id"] = peer
            access_hash = getattr(entity, "access_hash", None)
            if isinstance(entity, types.User) and isinstance(access_hash, int) and not isinstance(access_hash, bool):
                recipient["input_peer"] = {"kind": "user", "id": entity.id,
                                           "access_hash": access_hash}
            with self.store.db() as db:
                db.execute("UPDATE items SET payload=?,peer=? WHERE job=? AND seq=?",
                           (self.store.pack(recipient), peer_hash, job_id, seq))
            # Resolve destination and check existing membership before attempting an add.
            destination = None
            if job["kind"] == "member_add":
                destination = await asyncio.wait_for(client.get_entity(config["dest_target"]), TELEGRAM_READ_TIMEOUT)
                try:
                    membership = await asyncio.wait_for(
                        client(functions.channels.GetParticipantRequest(destination, entity)), TELEGRAM_READ_TIMEOUT)
                    if not isinstance(membership.participant, types.ChannelParticipantLeft):
                        reason = "membership_restricted" if isinstance(membership.participant, types.ChannelParticipantBanned) else "already_member"
                        self.store.outcome(job_id, seq, "skipped", reason)
                        return
                except errors.UserNotParticipantError:
                    # Telegram confirms absence; proceed to the guarded invitation.
                    pass

            async def fence(before_send):
                nonlocal attempted
                if before_send:
                    await _load_profile(config["operator"])
                    await check_sender()
                    if job["kind"] == "member_add" and config.get("member_form_grant"):
                        decision = outreach_policy.authorize(action="member_add", subject=target,
                            account_id=account, approval_id=config["approval_id"])
                        if not decision.allowed:
                            raise OutreachDenied(decision.reason)
                self.store.guard(job_id, seq, mark=before_send)
                attempted = attempted or before_send

            token = dispatch_guard.set(fence)
            async def deliver():
                if job["kind"] == "member_add":
                    result = await asyncio.wait_for(
                        client(functions.channels.InviteToChannelRequest(destination, [entity])), TELEGRAM_SEND_TIMEOUT)
                    if getattr(result, "missing_invitees", None):
                        raise OutreachDenied("invitation_not_completed")
                    return result
                return await asyncio.wait_for(
                    client.send_message(entity, config["messages"].get(target, config["message"])), TELEGRAM_SEND_TIMEOUT)

            from outreach_client import ConsentTelegramClient
            await dispatch_outreach(action=job["kind"], subject=peer, account_id=account,
                                    approval_id=config["approval_id"], delay=config["delay"], send=deliver,
                                    defer_attempt=isinstance(client, ConsentTelegramClient))
            self.store.outcome(job_id, seq, "completed", delay=config["delay"])
        except SenderChanged:
            self.store.outcome(job_id, seq, "pending", "sender_identity_changed", job_state="paused")
        except RecipientChanged:
            self.store.outcome(job_id, seq, "pending", "recipient_identity_changed", job_state="paused")
        except Interrupted:
            self.store.outcome(job_id, seq, "pending", "operator_interrupted")
        except errors.UserAlreadyParticipantError:
            self.store.outcome(job_id, seq, "skipped", "already_member")
        except errors.FloodWaitError as exc:
            # Record the account-level cooldown and return the recipient to the queue
            # pinned to its consented account. The job keeps running so recipients on
            # other available accounts continue sending; process() waits the whole job
            # only once every pending recipient's account is cooling down.
            record_provider_restriction(account, exc)
            self.store.outcome(job_id, seq, "pending", "telegram_cooldown")
        except errors.PeerFloodError as exc:
            # Telegram spam-limited this account (not this recipient). Cool the
            # account and requeue so other available accounts keep sending and the
            # job auto-resumes when the limit clears, instead of pausing the list.
            record_provider_restriction(account, exc)
            self.store.outcome(job_id, seq, "pending", "telegram_cooldown")
        except (errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                errors.FrozenMethodInvalidError) as exc:
            # The account itself is banned/frozen — retrying cannot help and a human
            # must swap it, so pause for review rather than loop.
            record_provider_restriction(account, exc)
            self.store.outcome(job_id, seq, "pending", type(exc).__name__, 86400, "paused")
        except OutreachDenied as exc:
            reason = str(exc)
            if job["kind"] == "member_add" and reason == "invitation_not_completed":
                self.store.outcome(job_id, seq, "skipped", "invitation_not_completed", delay=config["delay"])
            elif reason == "account_cooldown":
                # A concurrent worker cooled this account between selection and send;
                # requeue so process() reschedules across the other available accounts.
                self.store.outcome(job_id, seq, "pending", "telegram_cooldown")
            elif reason in RECIPIENT_SKIP_REASONS and job["kind"] == "personal_send":
                # This recipient hasn't consented / has opted out; skip just this
                # recipient and keep the queue moving — we never message them.
                self.store.outcome(job_id, seq, "skipped", reason, delay=config["delay"])
            elif reason == "rate_store_unavailable":
                self.store.outcome(job_id, seq, "pending", "rate_store_unavailable", job_state="paused")
            else:
                self.store.outcome(job_id, seq, "failed", reason, job_state="paused")
        except (errors.UserPrivacyRestrictedError, errors.UserNotMutualContactError,
                errors.UserChannelsTooMuchError, errors.InputUserDeactivatedError,
                errors.UserIsBlockedError, errors.YouBlockedUserError) as exc:
            self.store.outcome(job_id, seq, "skipped",
                               type(exc).__name__, delay=config["delay"])
        except (errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                errors.ChannelInvalidError, errors.ChatWriteForbiddenError) as exc:
            # For member jobs these are destination-wide permission failures, so
            # pause for review. Personal jobs have resolved a specific User, so the
            # rejection is this recipient's alone — skip them and keep the queue
            # moving instead of stranding the rest of the list.
            if job["kind"] == "personal_send":
                self.store.outcome(job_id, seq, "skipped", "recipient_unreachable", delay=config["delay"])
            else:
                self.store.outcome(job_id, seq, "pending", type(exc).__name__, job_state="paused")
        except errors.RPCError as exc:
            # A server error may have happened after accepting a write.
            uncertain = attempted and getattr(exc, "code", 500) >= 500
            self.store.outcome(job_id, seq, "uncertain" if uncertain else "failed", type(exc).__name__,
                               delay=config["delay"], job_state="needs_review" if uncertain else None)
        except asyncio.CancelledError:
            self.store.outcome(job_id, seq, "uncertain" if attempted else "pending", "worker_interrupted",
                               job_state="needs_review" if attempted else None)
            raise
        except TimeoutError:
            # Read-only timeouts can retry. A write may already have reached
            # Telegram, so uncertain delivery must never be sent twice.
            self.store.outcome(job_id, seq, "uncertain" if attempted else "pending",
                               "delivery_timeout" if attempted else "telegram_timeout",
                               delay=0 if attempted else 30,
                               job_state="needs_review" if attempted else "waiting")
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError, httpx.HTTPError) as exc:
            self.store.outcome(job_id, seq, "uncertain" if attempted else "pending", type(exc).__name__,
                               job_state="needs_review" if attempted else "paused")
        finally:
            if token is not None:
                dispatch_guard.reset(token)


worker = JobWorker(store)


def require_worker():
    if not worker.ensure_running():
        raise HTTPException(503, "Outreach worker unavailable; no new job accepted")


@router.post("/api/outreach/jobs/personal", status_code=202)
async def submit_personal(body: BulkJobRequest, request: Request):
    require_worker()
    operator = owner(request)
    targets = [t.strip().lower().lstrip("@") for t in body.targets]
    if not all(targets):
        raise HTTPException(400, "Recipient cannot be empty")
    accounts = body.selected_accounts()
    if body.per_account_limit and len(targets) > len(accounts) * body.per_account_limit:
        raise HTTPException(400, "Per-account run limit exceeded")
    return await submit("personal_send", operator, body, targets, accounts)


@router.post("/api/outreach/jobs/members", status_code=202)
async def submit_members(body: MemberJobRequest, request: Request):
    require_worker()
    if body.source_target:
        raise HTTPException(403, "Outreach denied: scraped_source_forbidden")
    targets = [t.strip().lower().removeprefix("@") for t in body.usernames or []]
    if not targets or (body.limit and len(set(targets)) > body.limit):
        raise HTTPException(400, "Provide explicit recipients within the member-add limit")
    valid = [t for t in targets if re.fullmatch(r"(?:[a-z][a-z0-9_]{3,31}|[1-9][0-9]{0,19})", t)]
    if not valid:
        raise HTTPException(400, "No valid recipient remains after skipping invalid entries")
    return await submit("member_add", owner(request), body, targets, [body.account_id])


async def submit(kind, operator, body, targets, accounts):
    try:
        job = store.submit(kind, operator, body, targets, accounts)
        return store.snapshot(job, operator)
    except OutreachDenied as exc:
        detail = ("No active member-add authorization is configured for this account. "
                  "Confirm recipient consent in Member Adder to authorize this new job as an administrator."
                  if str(exc) == "member_authorization_unavailable" else str(exc))
        raise HTTPException(403, detail) from None
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        raise HTTPException(503, "Encrypted job storage unavailable") from None


@router.get("/api/outreach/jobs")
async def list_jobs(request: Request):
    return {"jobs": store.listing(owner(request)), "worker": "ready" if worker.ensure_running() else "unavailable"}


@router.get("/api/outreach/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    return store.snapshot(job_id, owner(request))


@router.post("/api/outreach/jobs/{job_id}/{action}")
async def control_job(job_id: str, action: Literal["pause", "resume", "stop"], request: Request):
    return store.control(job_id, owner(request), action)
