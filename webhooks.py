"""Operator-managed outgoing webhooks with signed HTTPS delivery.

Secrets are returned once at creation or rotation, stored encrypted, and never
echoed by list endpoints. Delivery is HTTPS-only, refuses private/loopback
targets at send time (SSRF), follows no redirects, and signs the exact request
body so receivers can verify authenticity.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from open_teleset.crypto.cipher import SessionCipher
from outreach_policy import hash_identifier

router = APIRouter()
DELIVERY_TIMEOUT = 10
SIGNATURE_HEADER = "X-OpenTeleset-Signature"
EVENTS = {
    "outreach.job.completed": "A message or member-add job finished cleanly",
    "outreach.job.failed": "A message or member-add job finished with failures",
    "outreach.job.stopped": "An operator stopped a message or member-add job",
    "join.job.completed": "An Auto Join job finished cleanly",
    "join.job.failed": "An Auto Join job finished with failures",
    "join.job.stopped": "An operator stopped an Auto Join job",
    "webhook.test": "A test delivery requested from the dashboard",
}
_MAX_WEBHOOKS = 20
_tasks = set()


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    events: List[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value):
        return _validate_url(value)

    @field_validator("events")
    @classmethod
    def _valid_events(cls, value):
        return _validate_events(value)


class WebhookUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    url: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    events: Optional[List[str]] = None
    enabled: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value):
        return None if value is None else _validate_url(value)

    @field_validator("events")
    @classmethod
    def _valid_events(cls, value):
        return None if value is None else _validate_events(value)


def _validate_events(events):
    if len(set(events)) != len(events):
        raise ValueError("Duplicate webhook events")
    unknown = [event for event in events if event not in EVENTS]
    if unknown:
        raise ValueError(f"Unknown webhook event: {unknown[0]}")
    return events


def _validate_url(url: str) -> str:
    value = (url or "").strip()
    parts = urlsplit(value)
    if parts.scheme.lower() != "https":
        raise ValueError("Webhook URL must start with https://")
    if not parts.hostname:
        raise ValueError("Webhook URL must include a hostname")
    if parts.username or parts.password:
        raise ValueError("Webhook URL must not embed credentials")
    if not (parts.port is None or 1 <= parts.port <= 65535):
        raise ValueError("Webhook URL port is invalid")
    host = parts.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("Webhook URL must use a public DNS hostname") from None
    else:
        if not address.is_global:
            raise ValueError("Webhook URL must resolve to a public address")
    return value


class _BlockedTarget(Exception):
    pass


def _assert_public_target(url: str):
    host = urlsplit(url).hostname
    if not host:
        raise _BlockedTarget()
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _BlockedTarget() from exc
    for info in addresses:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise _BlockedTarget()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class WebhookStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("WEBHOOKS_DB", "./accounts/webhooks.sqlite3"))

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS webhooks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                    delivery_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_status INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '');
            """)
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def pack(value):
        return SessionCipher().encrypt(value)

    @staticmethod
    def unpack(value):
        return SessionCipher().decrypt(value)

    def create(self, operator, body):
        owner = hash_identifier(operator)
        secret = "owh_" + secrets.token_urlsafe(32)
        hook_id = str(uuid4())
        with self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM webhooks WHERE owner=?", (owner,)).fetchone()[0]
            if count >= _MAX_WEBHOOKS:
                raise HTTPException(409, f"At most {_MAX_WEBHOOKS} webhooks are allowed")
            db.execute(
                "INSERT INTO webhooks(id,owner,name,url,secret,events,enabled,created) "
                "VALUES(?,?,?,?,?,?,1,?)",
                (hook_id, owner, body.name.strip(), body.url,
                 self.pack(secret), json.dumps(body.events), time.time()))
        row = self.get(operator, hook_id)
        return {**self.public(row), "secret": secret}

    def list(self, operator):
        with self.db() as db:
            rows = db.execute("SELECT * FROM webhooks WHERE owner=? ORDER BY created",
                              (hash_identifier(operator),)).fetchall()
        return [self.public(row) for row in rows]

    def get(self, operator, hook_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM webhooks WHERE id=? AND owner=?",
                             (hook_id, hash_identifier(operator))).fetchone()
        if not row:
            raise HTTPException(404, "Webhook not found")
        return row

    def for_delivery(self, operator):
        with self.db() as db:
            return db.execute("SELECT * FROM webhooks WHERE owner=? AND enabled=1 "
                              "ORDER BY created", (hash_identifier(operator),)).fetchall()

    def update(self, operator, hook_id, body):
        row = self.get(operator, hook_id)
        fields, values = [], []
        if body.name is not None:
            fields.append("name=?")
            values.append(body.name.strip())
        if body.url is not None:
            fields.append("url=?")
            values.append(body.url)
        if body.events is not None:
            fields.append("events=?")
            values.append(json.dumps(body.events))
        if body.enabled is not None:
            fields.append("enabled=?")
            values.append(1 if body.enabled else 0)
        if fields:
            values.extend([hook_id, row["owner"]])
            with self.db() as db:
                db.execute(f"UPDATE webhooks SET {', '.join(fields)} WHERE id=? AND owner=?", values)
        return self.public(self.get(operator, hook_id))

    def rotate(self, operator, hook_id):
        self.get(operator, hook_id)
        secret = "owh_" + secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("UPDATE webhooks SET secret=? WHERE id=? AND owner=?",
                       (self.pack(secret), hook_id, hash_identifier(operator)))
        return {**self.public(self.get(operator, hook_id)), "secret": secret}

    def delete(self, operator, hook_id):
        self.get(operator, hook_id)
        with self.db() as db:
            db.execute("DELETE FROM webhooks WHERE id=? AND owner=?",
                       (hook_id, hash_identifier(operator)))

    def record_delivery(self, hook_id, status, error):
        with self.db() as db:
            db.execute(
                "UPDATE webhooks SET delivery_count=delivery_count+1, "
                "failure_count=failure_count+?, last_status=?, last_attempt=?, last_error=? "
                "WHERE id=?",
                (0 if 200 <= status < 300 else 1, status, time.time(), error[:200], hook_id))

    @staticmethod
    def public(row):
        return {"id": row["id"], "name": row["name"], "url": row["url"],
                "events": json.loads(row["events"]), "enabled": bool(row["enabled"]),
                "created": row["created"], "delivery_count": row["delivery_count"],
                "failure_count": row["failure_count"], "last_status": row["last_status"],
                "last_attempt": row["last_attempt"], "last_error": row["last_error"]}


store = WebhookStore()


async def _post(url: str, **kwargs):
    async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT, follow_redirects=False) as client:
        return await client.post(url, **kwargs)


async def deliver(webhook, event, data):
    delivery_id = str(uuid4())
    created = time.time()
    payload = {"id": delivery_id, "event": event, "created": created, "data": data}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(created))
    secret = store.unpack(webhook["secret"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Open-Teleset-Webhooks/1",
        "X-OpenTeleset-Event": event,
        "X-OpenTeleset-Delivery": delivery_id,
        "X-OpenTeleset-Timestamp": timestamp,
        SIGNATURE_HEADER: "sha256=" + sign(secret, timestamp, body),
    }
    status, error = 0, ""
    try:
        _assert_public_target(webhook["url"])
        response = await _post(webhook["url"], content=body, headers=headers)
        status = response.status_code
        if not 200 <= status < 300:
            error = f"HTTP {status}"
    except _BlockedTarget:
        error = "blocked_target"
    except httpx.HTTPError as exc:
        error = type(exc).__name__
    except Exception as exc:  # never let one receiver break the worker
        error = type(exc).__name__
    store.record_delivery(webhook["id"], status, error)
    return {"id": delivery_id, "status": status, "error": error}


def emit(operator, event, data):
    """Queue signed deliveries to the operator's matching webhooks."""
    if not operator:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for webhook in store.for_delivery(operator):
        if event not in json.loads(webhook["events"]):
            continue
        task = loop.create_task(deliver(webhook, event, data))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


def emit_job(operator, kind, snapshot):
    """Emit a terminal job event derived from a job snapshot."""
    state = snapshot.get("state")
    prefix = "join" if kind == "auto_join" else "outreach"
    if state == "completed":
        event = (f"{prefix}.job.failed" if snapshot.get("failed") or snapshot.get("uncertain")
                 else f"{prefix}.job.completed")
    elif state == "stopped":
        event = f"{prefix}.job.stopped"
    else:
        return
    emit(operator, event, {"job": snapshot})
    from open_teleset.telegram_notify import queue_event
    labels = {
        "join.job.completed": "Auto Join finished",
        "join.job.failed": "Auto Join finished with failures",
        "join.job.stopped": "Auto Join stopped",
        "outreach.job.completed": "Outreach job finished",
        "outreach.job.failed": "Outreach job finished with failures",
        "outreach.job.stopped": "Outreach job stopped",
    }
    details = (f"{snapshot.get('completed', 0)} done · {snapshot.get('skipped', 0)} skipped · "
               f"{snapshot.get('failed', 0)} failed")
    queue_event(operator, event, f"{labels[event]}: {details}")


def owner(request):
    user = getattr(request.state, "user", None)
    if not user or not user.id:
        raise HTTPException(403, "Authenticated operator required")
    return user.id


@router.get("/api/webhooks")
async def list_webhooks(request: Request):
    return {"webhooks": store.list(owner(request)),
            "events": [{"name": name, "description": description}
                       for name, description in EVENTS.items()]}


@router.post("/api/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate, request: Request):
    return store.create(owner(request), body)


@router.patch("/api/webhooks/{hook_id}")
async def update_webhook(hook_id: str, body: WebhookUpdate, request: Request):
    return store.update(owner(request), hook_id, body)


@router.post("/api/webhooks/{hook_id}/rotate")
async def rotate_webhook(hook_id: str, request: Request):
    return store.rotate(owner(request), hook_id)


@router.post("/api/webhooks/{hook_id}/test")
async def test_webhook(hook_id: str, request: Request):
    webhook = store.get(owner(request), hook_id)
    result = await deliver(webhook, "webhook.test", {"message": "Test delivery from Open Teleset"})
    return {**result, "ok": 200 <= result["status"] < 300}


@router.delete("/api/webhooks/{hook_id}", status_code=204)
async def delete_webhook(hook_id: str, request: Request):
    store.delete(owner(request), hook_id)
    return None
