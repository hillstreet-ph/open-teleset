"""Durable, owner-scoped outreach jobs; one dispatcher per mounted state volume.

Never replays uncertain delivery. Provider waits pin the recipient and pause the
whole job; rotation is assignment only, never a restriction fallback.
"""
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from telethon import errors, functions, types, utils

from open_teleset.crypto.cipher import SessionCipher
from open_teleset.security import _load_profile
from outreach_policy import (
    OutreachDenied,
    account_cooldown_remaining,
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
log = logging.getLogger(__name__)


class Interrupted(Exception):
    pass


class BulkJobRequest(BulkSendRequest):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class MemberJobRequest(AddMembersRequest):
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
            """)
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
            # Submission records declarations only once. Resume never renews consent.
            if kind == "personal_send" and request.consent_confirmed:
                for i, account in enumerate(accounts):
                    outreach_policy.record_personal_consents(targets[i::len(accounts)], account, operator)
            for account in accounts:
                _enforce_outreach(kind, targets, account, request.approval_id)
            job = str(uuid4())
            payload["operator"] = operator
            payload.pop("targets", None)
            payload.pop("usernames", None)
            db.execute("INSERT INTO jobs(id,owner,request_id,fingerprint,kind,payload,state,created) VALUES(?,?,?,?,?,?,?,?)",
                       (job, owner_hash, request.request_id, fingerprint, kind,
                        self.pack(payload), "queued", time.time()))
            seen = set()
            for seq, target in enumerate(targets):
                duplicate = target in seen
                seen.add(target)
                db.execute("INSERT INTO items(job,seq,payload,state,reason) VALUES(?,?,?,?,?)",
                           (job, seq, self.pack({"target": target, "account": accounts[seq % len(accounts)]}),
                            "skipped" if duplicate else "pending", "duplicate_recipient" if duplicate else ""))
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
            # Bounded response with full counts; all outcomes remain in the database.
            for item in db.execute("SELECT * FROM items WHERE job=? ORDER BY seq DESC LIMIT 100", (job,)):
                p = self.unpack(item["payload"])
                results.append({"target": p["target"], "account_id": p["account"],
                                "state": item["state"], "reason": item["reason"]})
            return {"id": job, "kind": row["kind"], "state": row["state"], "reason": row["reason"],
                    "retry_after": max(0, row["retry_at"] - time.time()), "total": sum(counts.values()),
                    "completed": counts.get("completed", 0), "skipped": counts.get("skipped", 0),
                    "failed": counts.get("failed", 0), "uncertain": counts.get("uncertain", 0),
                    "pending": counts.get("pending", 0), "in_flight": counts.get("sending", 0),
                    "results": list(reversed(results)), "created": row["created"]}

    def listing(self, operator):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE owner=? AND (state NOT IN ('completed','stopped') "
                                           "OR id IN (SELECT id FROM jobs WHERE owner=? AND state IN ('completed','stopped') "
                                           "ORDER BY created DESC LIMIT 30)) ORDER BY created DESC",
                                           (hash_identifier(operator), hash_identifier(operator)))]
        return [self.snapshot(j, operator) for j in ids]

    def control(self, job, operator, action):
        self.snapshot(job, operator)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if action == "resume":
                if row["state"] != "paused":
                    raise HTTPException(409, "Only paused jobs can resume; uncertain delivery requires review")
                state = "queued"
            elif row["state"] in ("completed", "stopped", "needs_review"):
                raise HTTPException(409, "Job is already terminal or requires review")
            else:
                state = "paused" if action == "pause" else "stopped"
            # Keep retry_at: user resume cannot shorten an existing cooldown.
            db.execute("UPDATE jobs SET state=?,reason=? WHERE id=?", (state, "operator_" + action, job))
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


class JobWorker:
    def __init__(self, storage):
        self.store = storage
        self.task = None
        self.lock = None
        self.error = None

    def start(self):
        if self.task and not self.task.done():
            return
        try:
            self.store.path.parent.mkdir(parents=True, exist_ok=True)
            self.lock = self.store.path.with_suffix(".worker.lock").open("a")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store.recover()
            self.task = asyncio.create_task(self.run())
        except (OSError, sqlite3.Error):
            self.error = "worker_unavailable"
            if self.lock:
                self.lock.close()
                self.lock = None

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
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
            except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError) as exc:
                # Fail closed, retain queue, and show failure to the operator.
                self.error = type(exc).__name__
                log.error("Outreach worker unavailable: %s", self.error)
            await asyncio.sleep(1)

    async def tick(self):
        with self.store.db() as db:
            jobs = db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','waiting') "
                              "AND retry_at<=? ORDER BY created", (time.time(),)).fetchall()
        for job in jobs:
            await self.process(job)

    async def process(self, job):
        job_id = job["id"]
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM items WHERE job=? AND state IN ('sending','uncertain')", (job_id,)).fetchone():
                db.execute("UPDATE jobs SET state='needs_review',reason='delivery_uncertain' WHERE id=?", (job_id,))
                db.execute("UPDATE items SET state='uncertain' WHERE job=? AND state='sending'", (job_id,))
                return
            item = db.execute("SELECT * FROM items WHERE job=? AND state='pending' ORDER BY seq LIMIT 1", (job_id,)).fetchone()
            if not item:
                db.execute("UPDATE jobs SET state='completed',reason='' WHERE id=? "
                           "AND state IN ('queued','running','waiting')", (job_id,))
                return
            current = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current[0] not in ACTIVE:
                return
            db.execute("UPDATE jobs SET state='running',reason='' WHERE id=?", (job_id,))
        config = self.store.unpack(job["payload"])
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
            client = await account_manager.get_client(account)
            if not client:
                self.store.waiting(job_id, "account_unavailable", max(30, account_cooldown_remaining(account)))
                return
            value = recipient.get("peer_id", target)
            entity = await client.get_entity(int(value) if str(value).isdigit() else value)
            peer = utils.get_peer_id(entity)
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
                outreach_policy.record_personal_consents([peer], account, config["operator"])
            recipient["peer_id"] = peer
            with self.store.db() as db:
                db.execute("UPDATE items SET payload=?,peer=? WHERE job=? AND seq=?",
                           (self.store.pack(recipient), peer_hash, job_id, seq))
            # Resolve destination and check existing membership before attempting an add.
            destination = None
            if job["kind"] == "member_add":
                destination = await client.get_entity(config["dest_target"])
                try:
                    membership = await client(functions.channels.GetParticipantRequest(destination, entity))
                    if not isinstance(membership.participant, types.ChannelParticipantLeft):
                        reason = "membership_restricted" if isinstance(membership.participant, types.ChannelParticipantBanned) else "already_member"
                        self.store.outcome(job_id, seq, "skipped", reason)
                        return
                except errors.UserNotParticipantError:
                    pass

            async def fence(before_send):
                nonlocal attempted
                if before_send:
                    await _load_profile(config["operator"])
                self.store.guard(job_id, seq, mark=before_send)
                attempted = attempted or before_send

            token = dispatch_guard.set(fence)
            async def deliver():
                if job["kind"] == "member_add":
                    result = await client(functions.channels.InviteToChannelRequest(destination, [entity]))
                    if getattr(result, "missing_invitees", None):
                        raise OutreachDenied("invitation_not_completed")
                    return result
                return await client.send_message(entity, config["messages"].get(target, config["message"]))

            from outreach_client import ConsentTelegramClient
            await dispatch_outreach(action=job["kind"], subject=peer, account_id=account,
                                    approval_id=config["approval_id"], delay=config["delay"], send=deliver,
                                    defer_attempt=isinstance(client, ConsentTelegramClient))
            self.store.outcome(job_id, seq, "completed", delay=config["delay"])
        except Interrupted:
            self.store.outcome(job_id, seq, "pending", "operator_interrupted")
        except errors.UserAlreadyParticipantError:
            self.store.outcome(job_id, seq, "skipped", "already_member")
        except errors.FloodWaitError as exc:
            record_provider_restriction(account, exc)
            self.store.outcome(job_id, seq, "pending", "telegram_cooldown", max(1, exc.seconds), "waiting")
        except (errors.PeerFloodError, errors.UserDeactivatedBanError,
                errors.AuthKeyUnregisteredError, errors.FrozenMethodInvalidError) as exc:
            record_provider_restriction(account, exc)
            self.store.outcome(job_id, seq, "pending", type(exc).__name__, 86400, "paused")
        except OutreachDenied as exc:
            if str(exc) == "account_cooldown":
                self.store.outcome(job_id, seq, "pending", "telegram_cooldown",
                                   max(1, account_cooldown_remaining(account)), "waiting")
            else:
                self.store.outcome(job_id, seq, "failed", str(exc), job_state="paused")
        except errors.RPCError as exc:
            # A server error may have happened after accepting a write.
            uncertain = attempted and getattr(exc, "code", 500) >= 500
            self.store.outcome(job_id, seq, "uncertain" if uncertain else "failed", type(exc).__name__,
                               delay=config["delay"], job_state="needs_review" if uncertain else None)
        except asyncio.CancelledError:
            self.store.outcome(job_id, seq, "uncertain" if attempted else "pending", "worker_interrupted",
                               job_state="needs_review" if attempted else None)
            raise
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError, httpx.HTTPError) as exc:
            self.store.outcome(job_id, seq, "uncertain" if attempted else "pending", type(exc).__name__,
                               job_state="needs_review" if attempted else "paused")
        finally:
            if token is not None:
                dispatch_guard.reset(token)


worker = JobWorker(store)


def require_worker():
    if not worker.task or worker.task.done() or worker.error:
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
    if body.template_id:
        # Freeze template content at submission, never silently send empty text.
        from datetime import datetime

        from template_manager import template_manager
        now = datetime.now()
        body.message = template_manager.render_template(body.template_id, time=now.strftime("%H:%M"), date=now.strftime("%Y-%m-%d")) or body.message
        body.template_id = None
    if len(body.message) > 4096 or (not body.message.strip() and any(t not in body.messages for t in targets)):
        raise HTTPException(400, "A valid message is required")
    return await submit("personal_send", operator, body, targets, accounts)


@router.post("/api/outreach/jobs/members", status_code=202)
async def submit_members(body: MemberJobRequest, request: Request):
    require_worker()
    if body.source_target:
        raise HTTPException(403, "Outreach denied: scraped_source_forbidden")
    targets = [t.strip().lower().lstrip("@") for t in body.usernames or []]
    if not targets or not all(targets) or len(targets) > body.limit:
        raise HTTPException(400, "Provide explicit recipients within the member-add limit")
    return await submit("member_add", owner(request), body, targets, [body.account_id])


async def submit(kind, operator, body, targets, accounts):
    try:
        job = store.submit(kind, operator, body, targets, accounts)
        return store.snapshot(job, operator)
    except OutreachDenied as exc:
        raise HTTPException(403, str(exc)) from None
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        raise HTTPException(503, "Encrypted job storage unavailable") from None


@router.get("/api/outreach/jobs")
async def list_jobs(request: Request):
    return {"jobs": store.listing(owner(request)), "worker": "ready" if worker.task and not worker.task.done() and not worker.error else "unavailable"}


@router.get("/api/outreach/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    return store.snapshot(job_id, owner(request))


@router.post("/api/outreach/jobs/{job_id}/{action}")
async def control_job(job_id: str, action: Literal["pause", "resume", "stop"], request: Request):
    return store.control(job_id, owner(request), action)
