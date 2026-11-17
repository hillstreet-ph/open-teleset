"""Auto Forwarder: mirror new messages from a source chat into a target chat.

A rule names one account, one source (a private chat, group or channel the
account can read) and one destination. Each pass forwards only messages newer
than the last id the rule recorded, so a restart or a failed pass cannot
duplicate history.

This is copying between the operator's own chats, not outreach: there is no
consent gate, but every Telegram flood wait is honoured per account, and an
exception is reported by its type only — never by its text.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from telethon import errors

from log_manager import log_manager
from open_teleset.operator_store import feature_enabled, read_secret, secret_key_available, write_secret

router = APIRouter(tags=["auto-forward"])
log = logging.getLogger(__name__)

STORE = "auto_forward"
FEATURE = "auto_forward"

MAX_RULES = 10
FORWARD_BATCH = 20
MIN_POLL_SECONDS = 30
DEFAULT_POLL_SECONDS = 60
MAX_POLL_SECONDS = 900
MAX_ACTIVITY = 60
NAME_MAX = 60
SOURCE_MAX = 200

OFF_MESSAGE = "Auto Forwarder is switched off. Turn it on under Integrations > Platform switches."
NO_SWITCH = "Switch Auto Forwarder on first."
TOO_MANY_RULES = f"Auto Forwarder keeps up to {MAX_RULES} rules."


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled(FEATURE):
        raise HTTPException(403, OFF_MESSAGE)


# ============ storage ============


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _blank() -> dict:
    return {
        "enabled": False,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "rules": [],
        "activity": [],
        "waits": {},
        "last_pass": 0,
    }


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _stored_rule(entry: dict) -> dict | None:
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    try:
        source = _reference(entry.get("source"))
        target = _reference(entry.get("target"))
    except HTTPException:
        return None
    if not source or not target or not entry.get("account"):
        return None
    return {
        "id": str(entry["id"]),
        "name": str(entry.get("name") or source)[:NAME_MAX],
        "account": str(entry["account"]),
        "source": source,
        "target": target,
        "enabled": bool(entry.get("enabled", True)),
        "last_id": int(entry.get("last_id") or 0),
        "forwarded": int(entry.get("forwarded") or 0),
        "last_forward_at": _as_float(entry.get("last_forward_at")),
        "error": str(entry.get("error") or "")[:NAME_MAX],
    }


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    record = _blank()
    if not isinstance(entry, dict):
        return record
    record["enabled"] = bool(entry.get("enabled"))
    record["poll_seconds"] = _clamp(entry.get("poll_seconds"), MIN_POLL_SECONDS, MAX_POLL_SECONDS,
                                    DEFAULT_POLL_SECONDS)
    record["rules"] = [rule for rule in (_stored_rule(item) for item in (entry.get("rules") or [])) if rule]
    record["activity"] = [item for item in (entry.get("activity") or []) if isinstance(item, dict)][-MAX_ACTIVITY:]
    waits = entry.get("waits")
    if isinstance(waits, dict):
        record["waits"] = {str(key): _as_float(value) for key, value in waits.items()}
    record["last_pass"] = _as_float(entry.get("last_pass"))
    return record


def _save(operator: str, record: dict) -> None:
    record["activity"] = record["activity"][-MAX_ACTIVITY:]
    all_records = _all()
    all_records[operator] = {key: record[key] for key in _blank()}
    write_secret(STORE, all_records)


# ============ rules ============


def _reference(value) -> str:
    """A chat reference as one comparison token: username without '@', or a numeric id."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "t.me/" in text or "telegram.me/" in text:
        path = text.split("t.me/", 1)[-1].split("telegram.me/", 1)[-1]
        path = path.split("?", 1)[0].split("/", 1)[0].strip()
        if path.startswith("+"):
            raise HTTPException(400, "Invite links cannot be forwarded from. Join the chat and use its @username or id.")
        text = path
    if text.startswith("@"):
        text = text[1:]
    text = text.strip()
    if not text or len(text) > SOURCE_MAX:
        raise HTTPException(400, "Enter a source as @username, t.me/username or a numeric chat id.")
    if text.lstrip("-").isdigit():
        return text
    lowered = text.lower()
    if not _re.fullmatch(r"[a-z0-9_]+", lowered):
        raise HTTPException(400, "Enter a source as @username, t.me/username or a numeric chat id.")
    return lowered


class ConfigBody(BaseModel):
    enabled: bool
    poll_seconds: int | None = None


class RuleBody(BaseModel):
    id: str | None = None
    name: str = Field(default="", max_length=NAME_MAX)
    account: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=2048)
    target: str = Field(default="", max_length=2048)
    enabled: bool = True


def _clean_rule(body: RuleBody, existing: dict | None = None) -> dict:
    account = str(body.account or "").strip()
    if not account:
        raise HTTPException(400, "Choose the account that reads the source chat.")
    source = _reference(body.source)
    target = _reference(body.target)
    if not source:
        raise HTTPException(400, "Enter the chat to forward from.")
    if not target:
        raise HTTPException(400, "Enter the chat to forward to.")
    if source == target:
        raise HTTPException(400, "The source and the destination must be different chats.")
    previous = existing or {}
    same_route = previous.get("source") == source and previous.get("target") == target
    return {
        "id": str(previous.get("id") or body.id or uuid4().hex[:12]),
        "name": (str(body.name or "").strip() or source)[:NAME_MAX],
        "account": account,
        "source": source,
        "target": target,
        "enabled": bool(body.enabled),
        # Editing the route starts a fresh cursor; the old id is meaningless there.
        "last_id": int(previous.get("last_id") or 0) if same_route else 0,
        "forwarded": int(previous.get("forwarded") or 0) if same_route else 0,
        "last_forward_at": _as_float(previous.get("last_forward_at")) if same_route else 0.0,
        "error": "",
    }


# ============ the listener ============

# Operators whose pass is running right now; a second caller is told to wait
# rather than repeating the same forwards.
_IN_FLIGHT: set = set()


def _client_getter():
    from account_manager import account_manager

    async def get_client(account_id: str):
        return await account_manager.get_client(account_id)

    return get_client


async def _entity(client, reference: str):
    try:
        if reference.lstrip("-").isdigit():
            return await client.get_entity(int(reference))
        return await client.get_entity("@" + reference)
    except (ValueError, KeyError, TypeError):
        return None


async def _forward_rule(client, rule: dict, now: float) -> dict:
    source = await _entity(client, rule["source"])
    target = await _entity(client, rule["target"])
    if source is None or target is None:
        return {"forwarded": 0, "error": "chat_not_found"}
    last = int(rule.get("last_id") or 0)
    ids = []
    async for message in client.iter_messages(source, min_id=last, limit=FORWARD_BATCH, reverse=True):
        ids.append(int(message.id))
    if not ids:
        return {"forwarded": 0, "error": ""}
    await client.forward_messages(target, ids, from_peer=source)
    return {"forwarded": len(ids), "error": "", "last_id": max(ids)}


async def poll_operator(operator: str, *, get_client=None, now: float = None) -> dict:
    """One pass over every enabled rule. Returns what it did; never raises.

    A manual run and a scheduled pass must not overlap: both would read the
    same cursor and forward the same messages twice, so the second caller
    reports ``already_running`` instead of starting a pass.
    """
    if operator in _IN_FLIGHT:
        return {"rules": 0, "forwarded": 0, "skipped": 0, "errors": 0, "waiting": 0,
                "reason": "already_running"}
    _IN_FLIGHT.add(operator)
    try:
        return await _pass(operator, get_client=get_client, now=now)
    finally:
        _IN_FLIGHT.discard(operator)


async def _pass(operator: str, *, get_client=None, now: float = None) -> dict:
    record = _record(operator)
    clock = time.time() if now is None else float(now)
    counts = {"rules": 0, "forwarded": 0, "skipped": 0, "errors": 0, "waiting": 0, "reason": ""}
    record["last_pass"] = clock
    if not record["enabled"]:
        counts["reason"] = "off"
        _save(operator, record)
        return counts
    rules = [rule for rule in record["rules"] if rule.get("enabled")][:MAX_RULES]
    if not rules:
        counts["reason"] = "no_rules"
        _save(operator, record)
        return counts

    getter = get_client or _client_getter()
    clients: dict = {}
    waits = record["waits"]
    # A wait set by this very pass takes effect on the next one, so every rule
    # present here reports against the same Telegram answer.
    blocked = {account for account, until in waits.items() if until > clock}
    for rule in rules:
        account = rule["account"]
        if account in blocked:
            counts["waiting"] += 1
            counts["skipped"] += 1
            continue
        counts["rules"] += 1
        if account not in clients:
            try:
                clients[account] = await getter(account)
            except Exception:
                clients[account] = None
        client = clients[account]
        if client is None:
            rule["error"] = "account_unavailable"
            counts["errors"] += 1
            _activity(record, clock, rule, 0, rule["error"])
            continue
        moved = 0
        try:
            outcome = await _forward_rule(client, rule, clock)
            moved = outcome["forwarded"]
            if moved:
                rule["last_id"] = outcome["last_id"]
                rule["forwarded"] = int(rule.get("forwarded") or 0) + moved
                rule["last_forward_at"] = clock
                counts["forwarded"] += moved
            rule["error"] = outcome["error"]
            if outcome["error"]:
                counts["errors"] += 1
        except errors.FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            waits[account] = clock + max(seconds, 1)
            rule["error"] = "flood_wait"
            counts["errors"] += 1
            counts["waiting"] += 1
        except errors.RPCError as exc:
            rule["error"] = type(exc).__name__
            counts["errors"] += 1
        except (ValueError, KeyError, TypeError):
            rule["error"] = "chat_not_found"
            counts["errors"] += 1
        except Exception:
            # Exception text can carry request details; only the class is recorded.
            rule["error"] = "forward_failed"
            counts["errors"] += 1
        _activity(record, clock, rule, moved, rule["error"])
    if counts["waiting"] and not counts["forwarded"] and not counts["errors"]:
        counts["reason"] = "waiting"
    _save(operator, record)
    return counts


def _activity(record: dict, clock: float, rule: dict, forwarded: int, error: str) -> None:
    if not forwarded and not error:
        return
    record["activity"].append({"at": clock, "rule": rule["name"], "account": rule["account"],
                               "forwarded": int(forwarded or 0), "error": error or ""})


def public_config(operator: str, *, worker_state: dict = None) -> dict:
    record = _record(operator)
    return {
        "feature": feature_enabled(FEATURE),
        "enabled": record["enabled"],
        "poll_seconds": record["poll_seconds"],
        "rules": record["rules"],
        "activity": list(reversed(record["activity"]))[:20],
        "worker": worker_state or {},
    }


# ============ routes ============


@router.get("/api/auto-forward/config")
async def get_config(request: Request):
    _enabled()
    return public_config(_operator(request), worker_state=_worker_state())


@router.put("/api/auto-forward/config")
async def put_config(body: ConfigBody, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    record["enabled"] = bool(body.enabled)
    if body.poll_seconds is not None:
        record["poll_seconds"] = _clamp(body.poll_seconds, MIN_POLL_SECONDS,
                                        MAX_POLL_SECONDS, DEFAULT_POLL_SECONDS)
    _save(operator, record)
    return {"success": True, "config": public_config(operator, worker_state=_worker_state())}


@router.post("/api/auto-forward/rules")
async def create_rule(body: RuleBody, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    existing = next((rule for rule in record["rules"] if rule["id"] == body.id), None)
    if existing is None and len(record["rules"]) >= MAX_RULES:
        raise HTTPException(409, TOO_MANY_RULES)
    rule = _clean_rule(body, existing)
    record["rules"] = [item for item in record["rules"] if item["id"] != rule["id"]] + [rule]
    _save(operator, record)
    log_manager.add_log("Auto Forwarder", rule["account"], f"Saved rule {rule['name']}")
    return {"success": True, "rule": rule, "config": public_config(operator, worker_state=_worker_state())}


@router.delete("/api/auto-forward/rules/{rule_id}")
async def delete_rule(rule_id: str, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    kept = [rule for rule in record["rules"] if rule["id"] != rule_id]
    if len(kept) == len(record["rules"]):
        raise HTTPException(404, "That rule is gone.")
    record["rules"] = kept
    _save(operator, record)
    return {"success": True, "config": public_config(operator, worker_state=_worker_state())}


@router.post("/api/auto-forward/run-now")
async def run_now(request: Request):
    """One pass, on the operator's own press. The same code the listener runs."""
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    if not record["enabled"]:
        raise HTTPException(409, NO_SWITCH)
    if not any(rule.get("enabled") for rule in record["rules"]):
        raise HTTPException(409, "Add at least one enabled rule first.")
    result = await poll_operator(operator)
    log_manager.add_log("Auto Forwarder", "-",
                        f"Forwarded {result['forwarded']} message(s) across {result['rules']} rule(s)")
    return {"success": True, "result": result,
            "config": public_config(operator, worker_state=_worker_state())}


# ============ background listener ============


class AutoForwardWorker:
    """One loop for every operator; a bad pass never stops the next one."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.last_tick: float | None = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = type(exc).__name__
                log.exception("Auto Forwarder pass failed")
            await asyncio.sleep(MIN_POLL_SECONDS)

    async def tick(self):
        if not feature_enabled(FEATURE):
            return
        for operator in _due_operators():
            await poll_operator(operator)
        self.last_tick = time.time()


def _due_operators() -> list:
    try:
        data = read_secret(STORE, {})
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    now = time.time()
    due = []
    for operator, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        if not any(isinstance(rule, dict) and rule.get("enabled") for rule in (entry.get("rules") or [])):
            continue
        interval = _clamp(entry.get("poll_seconds"), MIN_POLL_SECONDS, MAX_POLL_SECONDS, DEFAULT_POLL_SECONDS)
        if now - _as_float(entry.get("last_pass")) >= interval:
            due.append(operator)
    return due[:MAX_RULES]


def _worker_state() -> dict:
    return {"running": worker.task is not None and not worker.task.done(),
            "error": worker.error, "last_tick": worker.last_tick}


worker = AutoForwardWorker()
