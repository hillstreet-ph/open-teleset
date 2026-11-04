"""The Inbox: every chat on your own accounts, moving as messages arrive.

Telegram pushes updates to a connected client, and account_manager already keeps those
clients open, so this is the product's first push consumer rather than another poller.
Everything a third party said lives in this process and nowhere else: the encrypted store
sees opt-ins and unread counters, so a restart costs a re-read and no disk corpus of
other people's messages accumulates.

Nothing here sends on its own. The one write path posts text a human typed at that
moment, and it honours the opt-out ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from log_manager import log_manager
from open_teleset.operator_store import feature_enabled, read_secret, secret_key_available, write_secret
from outreach_policy import OutreachDenied, hash_identifier, outreach_policy

router = APIRouter(tags=["inbox"])
log = logging.getLogger(__name__)

STORE = "inbox"
OFF_MESSAGE = "Inbox is switched off. Turn it on under Integrations > Platform switches."
SEND_FAILED = "Telegram did not deliver that reply. Try again or answer in your own client."
SUPPRESSED = "That person asked not to be messaged. The reply is blocked."
UNREADABLE_LEDGER = "The opt-out list could not be read, so nothing was sent. Check the outreach policy file."
NO_REF = "That chat is not on this screen any more. Use Refresh now."
NO_ACCOUNT = "No account here answers to that id."
TOO_MANY_SUBSCRIBERS = "Four tabs already have this inbox open."
EMPTY_REPLY = "Type a reply before sending it."
BUCKETS = ("personal", "group", "channel", "bot")

MAX_WATCHED = 8
MAX_CHATS = 200
MAX_SUBSCRIBERS = 4
HEARTBEAT_SECONDS = 15
PAGE_MESSAGES = 50
STORED_MESSAGE_CHARS = 1200
CHAT_PREVIEW_CHARS = 120
STALE_EVENT_SECONDS = 1800
REPLY_MAX = 700
QUEUE_MAXSIZE = 200
PEER_REF_LENGTH = 16
NAME_MAX = 60

STATE_LABELS = {
    "off": "Inbox is switched off.",
    "live": "Watching this account for new messages.",
    "cooldown": "Telegram is asking this account to wait before it connects again.",
    "no-session": "This account has no saved Telegram session. Sign in again to watch it.",
    "no-account": "This account no longer exists here.",
    "proxy-unavailable": "The saved proxy for this account is unavailable.",
    "connection-unavailable": "Telegram did not hand back a connection. Try Refresh now.",
    "lost": "The connection stopped. Use Refresh now to start it again.",
    "slowed": "This tab stopped keeping up. Reload it to catch up.",
}


def bucket_of(entity: Any) -> str:
    """File a chat by what Telegram says it is.

    Matched on the class name, the way auto_reply._private() does it: the harness fakes are
    SimpleNamespaces, and an isinstance check against telethon.tl.types would silently
    classify every one of them as unknown.
    """
    if entity is None:
        return "unknown"
    kind = type(entity).__name__
    if kind in ("User", "PeerUser"):
        if getattr(entity, "bot", False):
            return "bot"
        return "unknown" if getattr(entity, "deleted", False) else "personal"
    if kind in ("Chat", "PeerChat"):
        return "group"
    if kind in ("Channel", "PeerChannel"):
        if getattr(entity, "megagroup", False):
            return "group"
        return "channel" if getattr(entity, "broadcast", False) else "unknown"
    return "unknown"


def peer_ref(operator: str, account_id: str, peer: Any) -> str:
    """The browser's handle for one chat. Raw peer ids never leave this process."""
    return hash_identifier(f"{operator}|{account_id}|{peer}")[:PEER_REF_LENGTH]


class Wall:
    """This operator's chats, newest first, in memory and capped."""

    def __init__(self):
        self._rows: OrderedDict[str, dict] = OrderedDict()
        self._messages: dict[str, list[dict]] = {}

    def upsert(self, row: dict) -> None:
        self._rows[row["ref"]] = row
        self._rows.move_to_end(row["ref"], last=False)
        while len(self._rows) > MAX_CHATS:
            dropped, _ = self._rows.popitem(last=True)
            self._messages.pop(dropped, None)

    def rows(self) -> list[dict]:
        return list(self._rows.values())

    def filter_chats(self, *, bucket: str = "", account: str = "", query: str = "") -> list[dict]:
        """q matches chat names, never message history - history search is out of scope."""
        needle = (query or "").strip().lower()
        out = []
        for row in self.rows():
            if bucket and row["bucket"] != bucket:
                continue
            if account and row["account_id"] != account:
                continue
            if needle and needle not in (row.get("name") or "").lower():
                continue
            out.append(row)
        return out

    def get(self, ref: str) -> Optional[dict]:
        return self._rows.get(ref)

    def put_messages(self, ref: str, items: list[dict]) -> None:
        if ref not in self._rows:
            return
        self._messages[ref] = items[:PAGE_MESSAGES]

    def messages(self, ref: str) -> list[dict]:
        return self._messages.get(ref, [])

    def size(self) -> int:
        return len(self._rows)


WALLS: dict[str, Wall] = {}
_CLIENTS: dict[str, Any] = {}
_HANDLERS: dict[str, Callable] = {}


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _save(operator: str, record: dict) -> None:
    everything = _all()
    everything[operator] = record
    write_secret(STORE, everything)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _record(operator: str) -> dict:
    entry = _mapping(_all().get(operator))
    accounts = {str(key): {"enabled": bool((value or {}).get("enabled"))}
                for key, value in _mapping(entry.get("accounts")).items() if isinstance(value, dict)}
    return {
        "accounts": accounts,
        "counters": {str(k): int(v) for k, v in _mapping(entry.get("counters")).items()},
        "seen": {str(k): float(v) for k, v in _mapping(entry.get("seen")).items()},
        "skipped_unknown": int(entry.get("skipped_unknown") or 0),
    }


def opted_in_accounts(operator: str) -> list[str]:
    return sorted(key for key, cfg in _record(operator)["accounts"].items() if cfg["enabled"])


def set_account_enabled(operator: str, account_id: str, enabled: bool) -> dict:
    record = _record(operator)
    if enabled and account_id not in record["accounts"] and len(opted_in_accounts(operator)) >= MAX_WATCHED:
        raise HTTPException(409, f"The Inbox watches up to {MAX_WATCHED} accounts at once.")
    if enabled:
        record["accounts"][account_id] = {"enabled": True}
    else:
        record["accounts"].pop(account_id, None)
    _save(operator, record)
    return record


def bump_counter(operator: str, ref: str) -> int:
    record = _record(operator)
    record["counters"][ref] = record["counters"].get(ref, 0) + 1
    record["seen"][ref] = time.time()
    _save(operator, record)
    return record["counters"][ref]


def zero_counter(operator: str, ref: str) -> None:
    record = _record(operator)
    record["counters"].pop(ref, None)
    record["seen"][ref] = time.time()
    _save(operator, record)


def counters(operator: str) -> dict:
    return _record(operator)["counters"]


class Hub:
    """Fan-out to one operator's browser tabs. A handler must never await a browser."""

    def __init__(self):
        self._queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, operator: str) -> Optional[asyncio.Queue]:
        queues = self._queues.setdefault(operator, [])
        if len(queues) >= MAX_SUBSCRIBERS:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        queues.append(queue)
        return queue

    def unsubscribe(self, operator: str, queue: asyncio.Queue) -> None:
        queues = self._queues.get(operator) or []
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._queues.pop(operator, None)

    def subscribers(self, operator: str) -> int:
        return len(self._queues.get(operator) or [])

    def publish(self, operator: str, payload: dict) -> int:
        delivered = 0
        for queue in list(self._queues.get(operator) or []):
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                self._drop_stalled(operator, queue)
        return delivered

    def _drop_stalled(self, operator: str, queue: asyncio.Queue) -> None:
        """A tab that cannot keep up is closed, never allowed to stall Telegram."""
        try:
            queue.get_nowait()               # make room for exactly one last notice
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait({"type": "state", "state": "slowed"})
        except asyncio.QueueFull:
            pass
        self.unsubscribe(operator, queue)


HUB = Hub()


def state_of(account_id: str) -> str:
    """The blockers this module can see for itself, as fixed codes - never our own prose."""
    from account_manager import account_manager

    account = account_manager.accounts.get(account_id)
    if account is None:
        return "no-account"
    if account_manager._paused_by_cooldown(account_id):
        return "cooldown"
    if not str(account.get("session_string") or "").strip():
        return "no-session"
    try:
        account_manager.saved_proxy(account_id)
    except ValueError:
        return "proxy-unavailable"
    return "live"


def _build_handler(account_id: str) -> Callable:
    async def handle(event):
        try:
            await note(account_id, event)
        except Exception:
            # One unreadable chat must not close the wall for anybody watching it.
            log.exception("Inbox could not file an update from %s", account_id)
    return handle


async def attach(operator: str, account_ids: list[str]) -> dict:
    """Connect and register one handler per account, process-wide. Returns {id: state code}."""
    from account_manager import account_manager

    outcome: dict[str, str] = {}
    for account_id in account_ids[:MAX_WATCHED]:
        blocked = state_of(account_id)
        if blocked != "live":
            outcome[account_id] = blocked
            continue
        if account_id in _HANDLERS:
            outcome[account_id] = "live"
            continue
        client = _CLIENTS.get(account_id) or await account_manager.get_client(account_id)
        if client is None:
            outcome[account_id] = "connection-unavailable"
            continue
        _CLIENTS[account_id] = client
        handler = _build_handler(account_id)
        try:
            from telethon import events

            client.add_event_handler(handler, events.NewMessage(incoming=True, outgoing=True))
        except Exception:
            log.exception("Inbox could not register a handler for %s", account_id)
            _CLIENTS.pop(account_id, None)
            outcome[account_id] = "connection-unavailable"
            continue
        _HANDLERS[account_id] = handler
        outcome[account_id] = "live"
    return outcome


def detach(account_id: str) -> bool:
    """Drop one account's handler. Called on opt-out, on the switch going off, and on shutdown."""
    handler = _HANDLERS.pop(account_id, None)
    client = _CLIENTS.pop(account_id, None)
    if handler is None:
        return False
    try:
        client.remove_event_handler(handler)
    except Exception:
        log.exception("Inbox could not remove a handler for %s", account_id)
    return True


def detach_all() -> int:
    return sum(1 for account_id in list(_HANDLERS) if detach(account_id))


def watched() -> list:
    return sorted(_HANDLERS)


def _seconds(value: Any) -> float:
    """Telegram hands back a datetime; the harness hands back a float."""
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (OSError, ValueError, OverflowError):
            return time.time()
    return time.time()


def message_shape(event: Any) -> dict:
    sender = getattr(event, "sender", None)
    return {
        "id": getattr(event, "id", None),
        "text": str(getattr(event, "raw_text", None) or "")[:STORED_MESSAGE_CHARS],
        "time": _seconds(getattr(event, "date", None)),
        "outgoing": bool(getattr(event, "out", False)),
        "sender": str(getattr(sender, "username", "") or getattr(sender, "first_name", "") or ""),
    }


def _display_name(entity: Any, dialog_name: str = "") -> str:
    name = str(dialog_name or "").strip()
    if not name:
        name = " ".join(part for part in (getattr(entity, "first_name", None),
                                          getattr(entity, "last_name", None)) if part).strip()
    if not name:
        name = str(getattr(entity, "title", "") or "").strip()
    if not name:
        username = str(getattr(entity, "username", "") or "").strip()
        name = f"@{username}" if username else ""
    return name[:NAME_MAX] or "a chat"


def _public(row: dict) -> dict:
    """The browser sees a ref and a display name, never the identifiers we hold."""
    return {key: value for key, value in row.items() if key not in ("peer", "username")}


def _record_owners(account_id: str) -> list:
    """Operators who opted this account in. One handler per account, not per operator."""
    owners = []
    for operator, entry in _all().items():
        if not isinstance(entry, dict):
            continue
        cfg = _mapping(entry.get("accounts")).get(account_id)
        if isinstance(cfg, dict) and cfg.get("enabled"):
            owners.append(str(operator))
    return owners


async def note(account_id: str, event: Any) -> dict:
    """One Telegram update in, one wall row out, fanned to whoever watches this account."""
    entity = getattr(event, "chat", None)
    kind = bucket_of(entity)
    if kind == "unknown":
        return {"stored": False, "published": 0, "reason": "unknown_bucket"}

    at = _seconds(getattr(event, "date", None))
    if time.time() - at > STALE_EVENT_SECONDS:
        return {"stored": False, "published": 0, "reason": "stale"}

    peer = getattr(event, "chat_id", None) or getattr(entity, "id", None)
    if peer is None:
        return {"stored": False, "published": 0, "reason": "no-peer"}

    owners = _record_owners(account_id)
    if not owners:
        return {"stored": False, "published": 0, "reason": "not_watched"}

    message = message_shape(event)
    published = 0
    for operator in owners:
        wall = WALLS.setdefault(operator, Wall())
        ref = peer_ref(operator, account_id, peer)
        if message["outgoing"]:
            unread = _record(operator)["counters"].get(ref, 0)
        else:
            unread = bump_counter(operator, ref)
        row = {
            "ref": ref,
            "account_id": account_id,
            "peer": peer,
            "username": str(getattr(entity, "username", "") or ""),
            "bucket": kind,
            "name": _display_name(entity),
            "preview": message["text"][:CHAT_PREVIEW_CHARS],
            "time": at,
            "unread": unread,
            "outgoing": message["outgoing"],
        }
        wall.upsert(row)
        wall.put_messages(ref, [message] + wall.messages(ref))
        published += HUB.publish(operator, {"type": "chat", "chat": _public(row)})
        published += HUB.publish(operator, {"type": "message", "ref": ref, "message": message})
    return {"stored": True, "published": published, "reason": ""}


async def snapshot(operator: str) -> dict:
    """Read every dialog on the opted-in accounts. The non-realtime path, always available."""
    accounts = opted_in_accounts(operator)
    states = await attach(operator, accounts) if accounts else {}
    wall = WALLS.setdefault(operator, Wall())
    record = _record(operator)
    chats, skipped = 0, 0
    for account_id in accounts:
        client = _CLIENTS.get(account_id)
        if client is None:
            continue
        try:
            async for dialog in client.iter_dialogs(limit=MAX_CHATS):
                entity = getattr(dialog, "entity", None)
                kind = bucket_of(entity)
                peer = getattr(entity, "id", None)
                if kind == "unknown" or peer is None:
                    skipped += 1
                    continue
                ref = peer_ref(operator, account_id, peer)
                last = getattr(dialog, "message", None)
                row = {
                    "ref": ref,
                    "account_id": account_id,
                    "peer": peer,
                    "username": str(getattr(entity, "username", "") or ""),
                    "bucket": kind,
                    "name": _display_name(entity, getattr(dialog, "name", "") or ""),
                    "preview": str(getattr(last, "raw_text", "") or "")[:CHAT_PREVIEW_CHARS],
                    "time": _seconds(getattr(dialog, "date", None)),
                    "unread": record["counters"].get(ref, 0),
                    "outgoing": bool(getattr(last, "out", False)),
                }
                wall.upsert(row)
                chats += 1
                HUB.publish(operator, {"type": "chat", "chat": _public(row)})
        except Exception:
            log.exception("Inbox snapshot failed")
    set_skipped(operator, skipped)
    return {"chats": chats, "skipped_unknown": skipped, "accounts": accounts, "state": states}


def set_skipped(operator: str, count: int) -> None:
    record = _record(operator)
    record["skipped_unknown"] = int(count)
    _save(operator, record)


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled("inbox"):
        raise HTTPException(403, OFF_MESSAGE)


def _label(operator: str) -> str:
    return str(operator)[:8] if operator else "operator"


def _suppress_key(row: dict) -> str:
    return str(row.get("username") or row.get("peer") or "")


def resolve(operator: str, ref: str) -> dict:
    row = WALLS.get(operator, Wall()).get(ref)
    if not row:
        raise HTTPException(404, NO_REF)
    return row


def public_config(operator: str) -> dict:
    accounts = opted_in_accounts(operator)
    state = {}
    for account_id in accounts:
        if account_id in _HANDLERS:
            state[account_id] = "live"
        else:
            blocked = state_of(account_id)
            state[account_id] = blocked if blocked != "live" else "off"
    return {
        "success": True,
        "enabled": feature_enabled("inbox"),
        "accounts": accounts,
        "limit": MAX_WATCHED,
        "state": state,
        "watched": watched(),
        "chats": WALLS.get(operator, Wall()).size(),
        "subscribers": HUB.subscribers(operator),
        "skipped_unknown": _record(operator)["skipped_unknown"],
        "labels": STATE_LABELS,
        "buckets": BUCKETS,
    }


class OptIn(BaseModel):
    enabled: bool


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=REPLY_MAX)


@router.get("/api/inbox/config")
async def config(request: Request):
    _enabled()
    return public_config(_operator(request))


@router.put("/api/inbox/accounts/{account_id}")
async def opt_account(account_id: str, body: OptIn, request: Request):
    _enabled()
    from account_manager import account_manager

    if account_id not in account_manager.accounts:
        raise HTTPException(404, NO_ACCOUNT)
    operator = _operator(request)
    set_account_enabled(operator, account_id, body.enabled)
    if body.enabled:
        await attach(operator, [account_id])
    else:
        detach(account_id)
    log_manager.add_log("Inbox", _label(operator),
                        f"Watching {account_id}" if body.enabled else f"Stopped watching {account_id}",
                        "info")
    return {"success": True, **public_config(operator)}


@router.get("/api/inbox/chats")
async def chats(request: Request, bucket: str = "", account: str = "", q: str = ""):
    _enabled()
    operator = _operator(request)
    if bucket and bucket not in BUCKETS:
        raise HTTPException(400, f"Choose a bucket from {', '.join(BUCKETS)}.")
    wall = WALLS.setdefault(operator, Wall())
    unread = _record(operator)["counters"]
    rows = wall.filter_chats(bucket=bucket, account=account, query=q)
    return {"success": True,
            "chats": [{**_public(row), "unread": unread.get(row["ref"], row["unread"])} for row in rows]}


@router.get("/api/inbox/chats/{ref}/messages")
async def thread(ref: str, request: Request, before: int = 0, limit: int = PAGE_MESSAGES):
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    wall = WALLS[operator]
    client = _CLIENTS.get(row["account_id"])
    if client is None:
        return {"success": True, "messages": wall.messages(ref), "state": "lost"}
    try:
        items = await client.get_messages(row["peer"], limit=max(1, min(limit, PAGE_MESSAGES)),
                                          offset_id=max(0, before))
    except Exception:
        log.exception("Inbox could not read a thread")
        return {"success": True, "messages": wall.messages(ref), "state": "lost"}
    page = [message_shape(item) for item in items or []]
    wall.put_messages(ref, page)
    return {"success": True, "messages": page, "state": "live"}


@router.post("/api/inbox/chats/{ref}/read")
async def mark_read(ref: str, request: Request):
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    zero_counter(operator, ref)
    stored = WALLS[operator].get(ref)
    if stored:
        stored["unread"] = 0
    client = _CLIENTS.get(row["account_id"])
    if client is not None:
        try:
            await client.mark_read(row["peer"])
        except Exception:
            log.info("Inbox could not mark a chat read on Telegram")
    HUB.publish(operator, {"type": "read", "ref": ref, "unread": 0})
    return {"success": True}


@router.post("/api/inbox/chats/{ref}/messages")
async def reply(ref: str, body: ReplyBody, request: Request):
    """Send what the operator typed. This is the only write path in the inbox."""
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, EMPTY_REPLY)
    try:
        suppressed = outreach_policy.is_suppressed(_suppress_key(row))
    except OutreachDenied:
        raise HTTPException(409, UNREADABLE_LEDGER) from None
    if suppressed:
        raise HTTPException(409, SUPPRESSED)
    client = _CLIENTS.get(row["account_id"])
    if client is None:
        raise HTTPException(400, SEND_FAILED)
    try:
        await client.send_message(row["peer"], text)
    except Exception:
        log.exception("Inbox reply failed")
        raise HTTPException(400, SEND_FAILED) from None
    message = {"id": None, "text": text[:STORED_MESSAGE_CHARS], "time": time.time(),
               "outgoing": True, "sender": ""}
    WALLS[operator].upsert({**row, "preview": text[:CHAT_PREVIEW_CHARS], "time": message["time"],
                            "outgoing": True})
    HUB.publish(operator, {"type": "message", "ref": ref, "message": message})
    log_manager.add_log("Inbox", _label(operator), "Sent a reply you typed", "info")
    return {"success": True, "message": message}


@router.post("/api/inbox/refresh")
async def refresh(request: Request):
    _enabled()
    operator = _operator(request)
    result = await snapshot(operator)
    log_manager.add_log("Inbox", _label(operator), f"Refreshed {result['chats']} chats", "info")
    return {"success": True, **result}


@router.delete("/api/inbox/cache")
async def clear_cache(request: Request):
    """Forget this screen. Telegram is not contacted and the opt-ins stay."""
    _enabled()
    operator = _operator(request)
    WALLS.pop(operator, None)
    return {"success": True, "cleared": True}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


async def event_source(operator: str, queue: asyncio.Queue):
    """ready, then the wall it starts from, then whatever Telegram pushes. Heartbeat keeps it warm."""
    try:
        yield _sse({"type": "ready", "state": public_config(operator)})
        for row in WALLS.get(operator, Wall()).rows():
            yield _sse({"type": "chat", "chat": _public(row)})
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                payload = {"type": "ping"}
            yield _sse(payload)
    finally:
        HUB.unsubscribe(operator, queue)


@router.get("/api/inbox/stream")
async def stream(request: Request):
    operator = _operator(request)
    _enabled()
    account_ids = opted_in_accounts(operator)
    if account_ids:
        await attach(operator, account_ids)
    queue = HUB.subscribe(operator)
    if queue is None:
        raise HTTPException(429, TOO_MANY_SUBSCRIBERS)
    return StreamingResponse(event_source(operator, queue), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
