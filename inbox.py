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
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

from log_manager import log_manager
from open_teleset.operator_store import read_secret, secret_key_available, write_secret
from outreach_policy import hash_identifier

router = APIRouter(tags=["inbox"])
log = logging.getLogger(__name__)

STORE = "inbox"
OFF_MESSAGE = "Inbox is switched off. Turn it on under Integrations > Platform switches."
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
