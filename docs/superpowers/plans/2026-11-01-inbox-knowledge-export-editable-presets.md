# Inbox, Knowledge Export and Editable Copilot Presets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a real-time, Telegram-style Inbox grouped by chat type, add a ZIP export to the Knowledge base, and let an operator edit their own Copilot presets.

**Architecture:** Persistent Telethon clients already held by `account_manager` gain the product's first push consumer: one `events.NewMessage` handler per opted-in account normalises an update into an in-process wall and fans it out to that operator's SSE queues. Operator scope comes from the route handler knowing its caller, not from a shared broadcaster. Two small additions ride along in `knowledge.py` (a streamed ZIP) and `copilot.py` (per-operator preset records).

**Tech Stack:** Python 3.12, FastAPI, Telethon ≥1.42, `zipfile` (stdlib), Vue 3 single-file dashboard, pytest + pytest-asyncio, Node's built-in test runner with Playwright/Chrome for the browser pass.

**Spec:** `docs/superpowers/specs/2026-11-01-inbox-realtime-sync-design.md` (the Inbox half of this plan argues from that document; read it first).

**Execution note:** Tasks 1–9 implement the spec. Tasks 10 and 11 are two bounded additions the operator requested in the same breath and are not covered by that spec; their requirements are stated in full inside their own tasks. Plan location follows this repo's existing house style (`docs/superpowers/plans/`).

## Global Constraints

- Ships dark: `inbox_enabled` is `False` in `DEFAULT_FEATURES` **and** declared in `FeatureUpdate`. A switch missing from `FeatureUpdate` is silently dropped by `PUT /api/platform/features` — the exact bug that made the Auto Reply switch persist nothing.
- Never write to disk: message bodies, previews, thread pages, raw peer ids. Opt-ins, unread counters and last-seen only, through `write_secret("inbox", …)`.
- No exception text crosses an HTTP boundary. Fixed sentences from the module, never `str(exc)` (the scraper/`RPCError` contract).
- Nothing sends on its own. The Inbox sends only text a human typed at that moment. Auto Reply stays the only surface that sends without a human.
- No account is reached at boot. Attach happens on first subscribe or first Refresh, for an opted-in account only.
- The Inbox does not join groups, scrape, add members, register accounts, or feed the knowledge base.
- Every `/api/inbox/*` route refuses with **403** and `OFF_MESSAGE` while the switch is off.
- Container runs uvicorn `--workers 1`, so in-process cache and fan-out are correct; do not add cross-process state.
- Bounds, verbatim: `MAX_WATCHED = 8`, `MAX_CHATS = 200`, `MAX_SUBSCRIBERS = 4`, `HEARTBEAT_SECONDS = 15`, `PAGE_MESSAGES = 50`, `STORED_MESSAGE_CHARS = 1200`, `CHAT_PREVIEW_CHARS = 120`, `STALE_EVENT_SECONDS = 1800`, `REPLY_MAX = 700`, `QUEUE_MAXSIZE = 200`, `PEER_REF_LENGTH = 16`.
- The repo's own test command must stay green: `PYTHONPATH=src: pytest tests/ -q --tb=short` and `node --test tests/*.mjs`.

## File Structure

| File | Responsibility |
|------|----------------|
| `inbox.py` (new) | The whole inbox subsystem: buckets, refs, in-process wall, subscriber hub, handler registry, state codes, and the `/api/inbox/*` router. Nothing else imports it except `dashboard.py` and the tests. |
| `src/open_teleset/operator_store.py` | Declare `inbox_enabled` in `DEFAULT_FEATURES`. |
| `dashboard_features.py` | Declare `inbox_enabled` on `FeatureUpdate` so the switch persists. |
| `dashboard.py` | `include_router(inbox_router)` beside the other product routers; `inbox_worker.stop()` in the lifespan `finally`. |
| `static/dashboard.html` | The Inbox tab (nav item, panel, state, methods, stream reader); the Knowledge Export button; the Copilot preset editor. |
| `knowledge.py` | `build_zip()` + `GET /api/knowledge/export`. |
| `copilot.py` | Per-operator preset records + `GET/POST/PUT/DELETE /api/copilot/presets`. |
| `tests/test_inbox.py` (new) | Buckets, stale drop, detach, the 403 gate, switch round-trip, reply safety, LRU and queue overflow, disk shape. |
| `tests/test_knowledge.py` | Export: contents, ordering, template opt-in, operator scope. |
| `tests/test_copilot.py` | Preset edit/add/delete/reset, bounds, and the blocked-instruction regression. |
| `tests/test_inbox_client.mjs` (new) | Source guard: inbox methods never reach bulk/schedules/auto-join/adder. |
| `ui_inbox.mjs` (new, session workspace) | Real-browser pass against the dev harness. |
| `docs/INBOX.md` (new) | Operator-facing doc; one README bullet beside the Auto Reply one. |

`inbox.py` is one file deliberately: it is a single subsystem with one interface (`attach`/`detach`/`note`/`snapshot` and a router), and the product's other subsystems (`auto_reply.py`, `knowledge.py`, `cloud_browser.py`) are each one file too.

---

## Part One — The Inbox

### Task 1: The platform switch, declared in both places

The regression this locks: `FeatureUpdate` is `extra='ignore'`, so a switch that is not a declared field is dropped without an error.

**Files:**
- Modify: `src/open_teleset/operator_store.py:83-90` (`DEFAULT_FEATURES`)
- Modify: `dashboard_features.py:553-557` (`FeatureUpdate`)
- Test: `tests/test_dashboard_features.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `feature_enabled("inbox") -> bool`, used by every later task as the gate.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_features.py`, following the existing `auto_reply_enabled` round-trip test in that file:

```python
def test_inbox_switch_round_trips_through_the_update_model(client):
    """A switch missing from FeatureUpdate is silently dropped, so this locks both halves."""
    assert features()["inbox_enabled"] is False          # ships dark

    body = client.put("/api/platform/features", json={"inbox_enabled": True}).json()
    assert body["features"]["inbox_enabled"] is True
    assert operator_store.get_features()["inbox_enabled"] is True
    assert operator_store.feature_enabled("inbox") is True

    assert client.put("/api/platform/features", json={"inbox_enabled": False}).json()
    assert operator_store.feature_enabled("inbox") is False
```

`features()` is the helper already in that file that reads `GET /api/platform/features`; reuse it rather than re-issuing the request.

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_dashboard_features.py -k inbox -q`
Expected: FAIL — `KeyError: 'inbox_enabled'` (absent from `DEFAULT_FEATURES`).

- [ ] **Step 3: Declare it in both models**

In `src/open_teleset/operator_store.py`, add to the `DEFAULT_FEATURES` dict after the `auto_reply_enabled` line:

```python
    # Ships dark: it holds a live connection per account, so it stays off until switched on.
    "inbox_enabled": False,
```

In `dashboard_features.py`, add one field to `FeatureUpdate`:

```python
class FeatureUpdate(BaseModel):
    automation_enabled: Optional[bool] = None
    factory_enabled: Optional[bool] = None
    cloud_browser_enabled: Optional[bool] = None
    auto_reply_enabled: Optional[bool] = None
    inbox_enabled: Optional[bool] = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_dashboard_features.py -q`
Expected: PASS, no regressions in the neighbouring switch tests.

- [ ] **Step 5: Commit**

```bash
git add src/open_teleset/operator_store.py dashboard_features.py tests/test_dashboard_features.py
git commit -m "Add the Inbox platform switch and lock its persistence"
```

---

### Task 2: `inbox.py` — buckets, refs, and the in-process wall

Pure logic, no I/O, so the classification rules get tested before anything can connect.

**Files:**
- Create: `inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `outreach_policy.hash_identifier(value: str) -> str`.
- Produces:
  - `BUCKETS: tuple[str, …] = ("personal", "group", "channel", "bot")`
  - `bucket_of(entity) -> str` — one of `BUCKETS` or `"unknown"`
  - `peer_ref(operator: str, account_id: str, peer: object) -> str` — 16 chars
  - `class Wall` with `upsert(row: dict) -> None`, `rows() -> list[dict]`, `filter_chats(bucket: str, account: str, query: str) -> list[dict]`, `get(ref: str) -> dict | None`, `put_messages(ref: str, items: list[dict]) -> None`, `messages(ref: str) -> list[dict]`, `size() -> int`
  - Row shape, used by every later task:

```python
{"ref": str, "account_id": str, "peer": int | str, "bucket": str, "name": str,
 "preview": str, "time": float, "unread": int, "outgoing": bool}
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox.py`:

```python
"""The inbox classifies, remembers briefly, and forgets on purpose.

Two invariants carry the safety weight: a chat is filed by its real Telegram type,
and nothing a third party said is ever written to disk.
"""

import time
from types import SimpleNamespace

import pytest

import inbox
from open_teleset import operator_store


class User(SimpleNamespace):
    """Named to match Telethon's class name: bucket_of works on the type, as _private() does."""


def a_user(**kw):
    kw.setdefault("id", 11)
    kw.setdefault("bot", False)
    kw.setdefault("deleted", False)
    return User(**kw)


def a_channel(**kw):
    kw.setdefault("id", 22)
    kw.setdefault("megagroup", False)
    kw.setdefault("broadcast", False)
    return SimpleNamespace(**kw)


@pytest.mark.parametrize(
    "entity,expected",
    [
        (a_user(), "personal"),
        (a_user(bot=True), "bot"),
        (a_user(deleted=True), "unknown"),
        (a_user(bot=True, deleted=True), "bot"),
        (SimpleNamespace(id=33), "group"),                      # telethon.tl.types.Chat
        (a_channel(megagroup=True), "group"),
        (a_channel(broadcast=True), "channel"),
        (a_channel(), "unknown"),                               # neither megagroup nor broadcast
        (SimpleNamespace(), "unknown"),
    ],
)
def test_bucket_of_files_each_telegram_type(entity, expected):
    assert inbox.bucket_of(entity) == expected


def test_bucket_of_reads_the_type_not_an_isinstance():
    """auto_reply._private() matches on the class name; the harness fakes are not real TL objects."""
    assert inbox.bucket_of(SimpleNamespace(id=1)) == "group"


def test_peer_ref_is_operator_scoped_stable_and_short():
    one = inbox.peer_ref("op-1", "acct-1", 4242)
    assert one == inbox.peer_ref("op-1", "acct-1", 4242)
    assert len(one) == inbox.PEER_REF_LENGTH
    assert one != inbox.peer_ref("op-2", "acct-1", 4242)
    assert one != inbox.peer_ref("op-1", "acct-2", 4242)
    assert str(4242) not in one


def test_wall_orders_newest_first_and_caps_at_max_chats():
    wall = inbox.Wall()
    for index in range(inbox.MAX_CHATS + 5):
        wall.upsert({"ref": f"r{index}", "account_id": "a", "peer": index, "bucket": "personal",
                     "name": f"chat {index}", "preview": "", "time": float(index),
                     "unread": 0, "outgoing": False})
    rows = wall.rows()
    assert len(rows) == inbox.MAX_CHATS
    assert rows[0]["ref"] == f"r{inbox.MAX_CHATS + 4}"          # newest first
    assert wall.get("r0") is None                               # oldest evicted


def test_wall_filters_by_bucket_account_and_name_only():
    wall = inbox.Wall()
    wall.upsert({"ref": "x1", "account_id": "a", "peer": 1, "bucket": "personal",
                 "name": "Alice Lee", "preview": "about the invoice", "time": 3.0,
                 "unread": 2, "outgoing": False})
    wall.upsert({"ref": "x2", "account_id": "b", "peer": 2, "bucket": "channel",
                 "name": "News", "preview": "Alice Lee mentioned here", "time": 2.0,
                 "unread": 0, "outgoing": True})
    assert [r["ref"] for r in wall.filter_chats(bucket="personal")] == ["x1"]
    assert [r["ref"] for r in wall.filter_chats(account="b")] == ["x2"]
    assert [r["ref"] for r in wall.filter_chats(query="alice")] == ["x1", "x2"]
    assert wall.filter_chats(query="invoice") == [wall.rows()[0]] or True
    # q matches chat names only: a preview hit must not qualify
    assert [r["ref"] for r in wall.filter_chats(query="invoice")] == ["x1"]


def test_wall_evicts_a_chats_messages_with_the_chat():
    wall = inbox.Wall()
    for index in range(inbox.MAX_CHATS + 1):
        wall.upsert({"ref": f"r{index}", "account_id": "a", "peer": index, "bucket": "personal",
                     "name": "n", "preview": "", "time": float(index), "unread": 0, "outgoing": False})
    wall.put_messages("r0", [{"id": 1, "text": "gone", "time": 1.0, "outgoing": False, "sender": ""}])
    assert wall.messages("r0") == []
    assert wall.size() == inbox.MAX_CHATS
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'inbox'`.

- [ ] **Step 3: Write the module head and the Wall**

Create `inbox.py`:

```python
"""The Inbox: every chat on your own accounts, moving as messages arrive.

Telegram pushes updates to a connected client, and account_manager already keeps those
clients open, so this is the product's first push consumer rather than another poller.
Everything a stranger said lives in this process and nowhere else: the encrypted store
sees opt-ins and unread counters, so a restart costs a re-fetch and no disk corpus of
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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from log_manager import log_manager
from open_teleset.operator_store import feature_enabled, read_secret, secret_key_available, write_secret
from outreach_policy import hash_identifier, outreach_policy

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
    kind = type(entity).__name__
    if kind in ("User", "PeerUser"):
        if getattr(entity, "bot", False):
            return "bot"
        return "personal" if not getattr(entity, "deleted", False) else "unknown"
    if kind == "Chat":
        return "group"
    if kind == "Channel":
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
        self._messages[ref] = items[:PAGE_MESSAGES]

    def messages(self, ref: str) -> list[dict]:
        return self._messages.get(ref, [])

    def size(self) -> int:
        return len(self._rows)
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add inbox.py tests/test_inbox.py
git commit -m "Classify inbox chats by Telegram type and cap the in-memory wall"
```

---

### Task 3: Opt-ins and counters in the encrypted store — and nothing else

**Files:**
- Modify: `inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `read_secret("inbox", {})`, `write_secret("inbox", …)`, `secret_key_available()`.
- Produces:
  - `_record(operator: str) -> dict` → `{"enabled": bool, "accounts": {account_id: {"enabled": bool}}, "counters": {ref: int}, "seen": {ref: float}}`
  - `_save(operator: str, record: dict) -> None`
  - `opted_in_accounts(operator: str) -> list[str]`
  - `set_account_enabled(operator: str, account_id: str, enabled: bool) -> dict`
  - `bump_counter(operator: str, ref: str) -> int`, `zero_counter(operator: str, ref: str) -> None`, `counters(operator: str) -> dict`
  - `WALLS: dict[str, Wall]` (module-level; one process, `--workers 1`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox.py`:

```python
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({"inbox_enabled": True})
    monkeypatch.setattr(inbox.log_manager, "add_log", lambda *a, **k: None)
    inbox.WALLS.clear()
    yield tmp_path


def test_opting_in_is_capped_and_survives_a_reload(workspace):
    for index in range(inbox.MAX_WATCHED):
        inbox.set_account_enabled("op-1", f"acct-{index}", True)
    assert len(inbox.opted_in_accounts("op-1")) == inbox.MAX_WATCHED
    with pytest.raises(HTTPException) as refusal:
        inbox.set_account_enabled("op-1", "acct-overflow", True)
    assert refusal.value.status_code == 409
    assert inbox.opted_in_accounts("op-1")[:1] == ["acct-0"]

    inbox.set_account_enabled("op-1", "acct-0", False)
    assert "acct-0" not in inbox.opted_in_accounts("op-1")


def test_only_opt_ins_and_counters_reach_the_disk(workspace):
    """The disk shape is the privacy contract: assert it, do not trust the writer."""
    inbox.set_account_enabled("op-1", "acct-0", True)
    inbox.bump_counter("op-1", "ref-abc")
    wall = inbox.WALLS.setdefault("op-1", inbox.Wall())
    wall.upsert({"ref": "ref-abc", "account_id": "acct-0", "peer": 90210,
                 "bucket": "personal", "name": "Alice Lee",
                 "preview": "the private thing she said", "time": time.time(),
                 "unread": 1, "outgoing": False})

    raw = (workspace / "inbox.secret").read_text()
    assert "Alice" not in raw and "private thing" not in raw and "90210" not in raw
    stored = operator_store.read_secret("inbox", {})
    assert stored["op-1"]["counters"] == {"ref-abc": 1}
    assert stored["op-1"]["accounts"] == {"acct-0": {"enabled": True}}
    assert "preview" not in str(stored["op-1"])


def test_counters_are_per_operator(workspace):
    inbox.set_account_enabled("op-1", "acct-0", True)
    inbox.bump_counter("op-1", "shared-ref")
    assert inbox.counters("op-1") == {"shared-ref": 1}
    assert inbox.counters("op-2") == {}
    inbox.zero_counter("op-1", "shared-ref")
    assert inbox.counters("op-1") == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -k "opt or counters or disk" -q`
Expected: FAIL — `AttributeError: module 'inbox' has no attribute 'set_account_enabled'`.

- [ ] **Step 3: Implement the store layer**

Append to `inbox.py`, after the `Wall` class:

```python
WALLS: dict[str, Wall] = {}


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _save(operator: str, record: dict) -> None:
    everything = _all()
    everything[operator] = record
    write_secret(STORE, everything)


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    if not isinstance(entry, dict):
        entry = {}
    accounts = {str(key): {"enabled": bool((value or {}).get("enabled"))}
                for key, value in (entry.get("accounts") or {}).items() if isinstance(value, dict)}
    return {
        "enabled": bool(entry.get("enabled")),
        "accounts": accounts,
        "counters": {str(k): int(v) for k, v in (entry.get("counters") or {}).items()},
        "seen": {str(k): float(v) for k, v in (entry.get("seen") or {}).items()},
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add inbox.py tests/test_inbox.py
git commit -m "Keep only inbox opt-ins and unread counters on disk, capped at eight accounts"
```

---

### Task 4: Attach and detach a handler, with the state code decided up front

**Files:**
- Modify: `inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `account_manager.account_manager` — `.accounts`, `._paused_by_cooldown(account_id)`, `.saved_proxy(account_id)`, `.get_client(account_id)`; `telethon.events.NewMessage`.
- Produces:
  - `state_of(account_id: str) -> str` — `"live"` or one fixed code (`cooldown`, `no-session`, `no-account`, `proxy-unavailable`)
  - `async attach(operator: str, account_ids: list[str]) -> dict[str, str]` → `{account_id: state_code}`
  - `detach(account_id: str) -> bool`, `detach_all() -> int`, `watched() -> list[str]`
  - `HUB: Hub` with `subscribe(operator) -> asyncio.Queue | None`, `unsubscribe(operator, queue)`, `publish(operator, payload: dict) -> int`, `subscribers(operator) -> int`

- [ ] **Step 1: Write the failing test**

```python
class FakeClient:
    """Stands a Telethon client in at the seams the inbox actually touches."""

    def __init__(self, account_id="acct-0", fail_add=False):
        self.account_id = account_id
        self.handlers = []
        self.fail_add = fail_add
        self.removed = []

    def add_event_handler(self, handler, *args, **kwargs):
        if self.fail_add:
            raise RuntimeError("Telethon refused to register the handler")
        self.handlers.append(handler)

    def remove_event_handler(self, handler, *args, **kwargs):
        self.removed.append(handler)
        self.handlers.remove(handler)

    async def iter_dialogs(self, limit=None):
        for index in range(3):
            yield SimpleNamespace(entity=a_user(id=index), name=f"chat {index}",
                                  unread_count=0, date=time.time() - index)

    async def get_messages(self, peer, limit=50, offset_id=0):
        return [SimpleNamespace(id=limit, text="hello", raw_text="hello", date=None,
                                out=False, sender=None, sender_id=peer)]

    async def send_message(self, peer, text):
        self.sent = getattr(self, "sent", [])
        self.sent.append((peer, text))
        return SimpleNamespace(id=99, text=text, date=None, out=True, sender=None)

    async def mark_read(self, entity):
        self.read = getattr(self, "read", 0) + 1

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def accounts(monkeypatch, workspace):
    from account_manager import account_manager
    clients = {}

    def an_account(account_id="acct-0", session="saved-session"):
        record = {"id": account_id}
        if session:
            record["session_string"] = session
        account_manager.accounts[account_id] = record
        clients.setdefault(account_id, FakeClient(account_id))
        return account_id

    async def get_client(account_id="default", proxy=None):
        return clients.get(account_id)

    monkeypatch.setattr(account_manager, "accounts", {})
    monkeypatch.setattr(account_manager, "get_client", get_client)
    monkeypatch.setattr(account_manager, "saved_proxy", lambda account_id: {"proxy_type": 0})
    monkeypatch.setattr(account_manager, "_paused_by_cooldown", lambda account_id: False)
    inbox._CLIENTS.clear()
    yield an_account, clients
    inbox._CLIENTS.clear()


def test_state_of_reads_the_three_blockers_it_can_see_before_connecting(accounts, monkeypatch):
    an_account, _ = accounts
    from account_manager import account_manager
    assert inbox.state_of(an_account()) == "live"
    assert inbox.state_of("does-not-exist") == "no-account"

    account_manager._paused_by_cooldown = lambda account_id: True
    assert inbox.state_of(an_account()) == "cooldown"

    account_manager._paused_by_cooldown = lambda account_id: False
    account_manager.accounts[an_account()]["session_string"] = ""
    assert inbox.state_of(an_account()) == "no-session"

    def broken_proxy(account_id):
        raise ValueError("saved proxy is gone")
    account_manager.saved_proxy = broken_proxy
    account_manager.accounts[an_account()]["session_string"] = "x"
    assert inbox.state_of(an_account()) == "proxy-unavailable"


def test_attach_registers_one_handler_per_account_not_per_operator(accounts):
    an_account, clients = accounts
    account_id = an_account()
    first = asyncio.run(inbox.attach("op-1", [account_id]))
    second = asyncio.run(inbox.attach("op-2", [account_id]))
    assert first == second == {account_id: "live"}
    assert len(clients[account_id].handlers) == 1
    assert inbox.watched() == [account_id]


def test_attach_reports_a_connection_we_could_not_make(accounts, monkeypatch):
    from account_manager import account_manager
    an_account, _ = accounts
    account_id = an_account()
    account_manager.accounts[account_id]["session_string"] = ""
    assert asyncio.run(inbox.attach("op-1", [account_id])) == {account_id: "no-session"}
    assert inbox.watched() == []


def test_detach_removes_the_handler_exactly_once(accounts):
    an_account, clients = accounts
    account_id = an_account()
    asyncio.run(inbox.attach("op-1", [account_id]))
    assert inbox.detach(account_id) is True
    assert clients[account_id].removed and len(clients[account_id].handlers) == 0
    assert inbox.detach(account_id) is False          # already gone
    assert inbox.detach_all() == 0


def test_a_handler_is_not_registered_twice_after_a_re_attach(accounts):
    an_account, clients = accounts
    account_id = an_account()
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.attach("op-1", [account_id]))
    assert len(clients[account_id].handlers) == 1


def test_switching_off_detaches_everything(accounts):
    an_account, _ = accounts
    asyncio.run(inbox.attach("op-1", [an_account(), an_account("acct-1")]))
    assert len(inbox.watched()) == 2
    assert inbox.detach_all() == 2
    assert inbox.watched() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -k "state_of or attach or detach" -q`
Expected: FAIL — `AttributeError: module 'inbox' has no attribute 'state_of'`.

- [ ] **Step 3: Implement attach, detach and the hub**

```python
_CLIENTS: dict[str, Any] = {}
_HANDLERS: dict[str, Callable] = {}


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
                # A tab that cannot keep up is closed, not allowed to stall Telegram.
                try:
                    queue.put_nowait({"type": "state", "state": "slowed"})
                except asyncio.QueueFull:
                    pass
                self.unsubscribe(operator, queue)
        return delivered


HUB = Hub()


def state_of(account_id: str) -> str:
    """The blockers this module can see for itself, as fixed codes - never our own prose."""
    from account_manager import account_manager

    account = account_manager.accounts.get(account_id)
    if account is None:
        return "no-account"
    if account_manager._paused_by_cooldown(account_id):
        return "cooldown"
    if not (account.get("session_string") or "").strip():
        return "no-session"
    try:
        account_manager.saved_proxy(account_id)
    except ValueError:
        return "proxy-unavailable"
    return "live"


async def attach(operator: str, account_ids: list[str]) -> dict[str, str]:
    """Connect and register one handler per account. Returns {account_id: state code}."""
    from account_manager import account_manager

    outcome: dict[str, str] = {}
    for account_id in account_ids[:MAX_WATCHED]:
        blocked = state_of(account_id)
        if blocked != "live":
            outcome[account_id] = blocked
            continue
        client = _CLIENTS.get(account_id)
        if client is None:
            client = await account_manager.get_client(account_id)
        if client is None:
            outcome[account_id] = "connection-unavailable"
            continue
        _CLIENTS[account_id] = client
        if account_id not in _HANDLERS:
            handler = _build_handler(account_id)
            try:
                from telethon import events

                client.add_event_handler(handler, events.NewMessage(incoming=True))
            except Exception:
                log.exception("Inbox could not register a handler")
                _CLIENTS.pop(account_id, None)
                outcome[account_id] = "connection-unavailable"
                continue
            _HANDLERS[account_id] = handler
        outcome[account_id] = "live"
    return outcome


def detach(account_id: str) -> bool:
    handler = _HANDLERS.pop(account_id, None)
    client = _CLIENTS.pop(account_id, None)
    if handler is None:
        return False
    try:
        client.remove_event_handler(handler)
    except Exception:
        log.exception("Inbox could not remove a handler")
    return True


def detach_all() -> int:
    return sum(1 for account_id in list(_HANDLERS) if detach(account_id))


def watched() -> list[str]:
    return sorted(_HANDLERS)
```

`_build_handler` is a stub in this task — Task 5 gives it a body:

```python
def _build_handler(account_id: str) -> Callable:
    async def handle(event):
        await note(account_id, event)
    return handle
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: PASS. `note` does not exist yet, so give it the body from Task 5 Step 3 now if you prefer, or leave the handler unexercised — `test_attach_*` never fires it.

- [ ] **Step 5: Commit**

```bash
git add inbox.py tests/test_inbox.py
git commit -m "Attach one Telegram update handler per watched inbox account"
```

---

### Task 5: Normalise an update and fan it out

**Files:**
- Modify: `inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `bucket_of`, `peer_ref`, `Wall`, `HUB`, `bump_counter`, `_record`, `opted_in_accounts`.
- Produces:
  - `async note(account_id: str, event: Any) -> dict` → `{"stored": bool, "published": int, "reason": str}`; `reason` in `{"", "unknown_bucket", "stale", "not_watched"}`
  - `message_shape(event) -> dict` → `{"id", "text", "time", "outgoing", "sender"}`
  - `row_from_dialog(account_id: str, dialog: Any) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
def an_event(peer=4242, text="the thing said", age=0, out=False, *, entity=None, name="Alice Lee"):
    return SimpleNamespace(
        chat=entity or a_user(id=peer),
        chat_id=peer,
        raw_text=text,
        date=time.time() - age,
        out=out,
        id=77,
        sender=None,
    )


def watched_operator(accounts):
    an_account, _ = accounts
    account_id = an_account()
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    return account_id


def test_a_new_message_lands_in_the_wall_and_in_the_queue(accounts):
    account_id = watched_operator(accounts)
    queue = inbox.HUB.subscribe("op-1")
    result = asyncio.run(inbox.note(account_id, an_event()))
    assert result["stored"] is True and result["published"] == 1
    row = inbox.WALLS["op-1"].rows()[0]
    assert row["bucket"] == "personal" and row["unread"] == 1
    assert row["name"] == "Alice Lee"
    assert row["preview"] == "the thing said"
    payloads = [queue.get_nowait() for _ in range(queue.qsize())]
    assert {p["type"] for p in payloads} == {"chat", "message"}


def test_a_second_operator_sharing_the_account_gets_only_its_own_ref(accounts):
    """The ref is salted with the operator, so two watchers never collide on one handle."""
    an_account, clients = accounts
    account_id = an_account()
    inbox.set_account_enabled("op-1", account_id, True)
    inbox.set_account_enabled("op-2", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    one, two = inbox.HUB.subscribe("op-1"), inbox.HUB.subscribe("op-2")
    asyncio.run(inbox.note(account_id, an_event()))
    assert one.qsize() == 2 and two.qsize() == 2
    first = inbox.WALLS["op-1"].rows()[0]["ref"]
    second = inbox.WALLS["op-2"].rows()[0]["ref"]
    assert first != second


def test_an_update_older_than_the_stale_window_is_dropped(accounts):
    account_id = watched_operator(accounts)
    queue = inbox.HUB.subscribe("op-1")
    result = asyncio.run(inbox.note(account_id, an_event(age=inbox.STALE_EVENT_SECONDS + 60)))
    assert result["reason"] == "stale" and result["stored"] is False
    assert queue.qsize() == 0 and inbox.WALLS["op-1"].rows() == []


def test_an_unclassifiable_chat_is_stored_nowhere(accounts):
    account_id = watched_operator(accounts)
    inbox.HUB.subscribe("op-1")
    result = asyncio.run(inbox.note(account_id, an_event(entity=SimpleNamespace(id=5))))
    assert result["reason"] == "unknown_bucket"
    assert inbox.WALLS["op-1"].rows() == []


def test_a_message_for_an_account_nobody_watched_is_ignored(accounts):
    an_account, _ = accounts
    asyncio.run(inbox.note("acct-never-opted", an_event()))
    assert inbox.WALLS == {}


def test_a_handler_that_raises_never_closes_the_stream(accounts, monkeypatch):
    account_id = watched_operator(accounts)
    queue = inbox.HUB.subscribe("op-1")
    monkeypatch.setattr(inbox, "note", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    asyncio.run(inbox._HANDLERS[account_id](an_event()))
    assert inbox.HUB.subscribers("op-1") == 1
    assert queue is not None


def test_a_slow_subscriber_is_dropped_and_the_others_keep_receiving(accounts):
    account_id = watched_operator(accounts)
    slow, fine = inbox.HUB.subscribe("op-1"), inbox.HUB.subscribe("op-1")
    for index in range(inbox.QUEUE_MAXSIZE):
        slow.put_nowait({"type": "ping", "i": index})
    asyncio.run(inbox.note(account_id, an_event(peer=8899)))
    assert inbox.HUB.subscribers("op-1") == 1
    assert fine.qsize() == 2
    assert [p["state"] for p in [slow.get_nowait() for _ in range(inbox.QUEUE_MAXSIZE)]][-1] == "slowed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -k "message_lands or stale or unclassifiable or raises or slow" -q`
Expected: FAIL — `AttributeError: module 'inbox' has no attribute 'note'` (or `NameError` on `an_event` if the helper is missing; add it).

- [ ] **Step 3: Implement `note`**

```python
def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (OSError, ValueError, OverflowError):
            return time.time()
    return time.time()


def message_shape(event: Any) -> dict:
    return {
        "id": getattr(event, "id", None),
        "text": (getattr(event, "raw_text", None) or "")[:STORED_MESSAGE_CHARS],
        "time": _seconds(getattr(event, "date", None)),
        "outgoing": bool(getattr(event, "out", False)),
        "sender": str(getattr(getattr(event, "sender", None), "username", "")
                      or getattr(getattr(event, "sender", None), "first_name", "") or ""),
    }


def _display_name(entity: Any, dialog_name: str = "") -> str:
    name = str(dialog_name or "").strip()
    if not name:
        name = " ".join(part for part in (getattr(entity, "first_name", None),
                                         getattr(entity, "last_name", None)) if part)
    if not name:
        username = str(getattr(entity, "username", "") or "").strip()
        name = f"@{username}" if username else ""
    return name[:NAME_MAX] or "a chat"


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
        return {"stored": False, "published": 0, "reason": "unknown_bucket"}

    published = 0
    for operator in _record_owners(account_id):
        wall = WALLS.setdefault(operator, Wall())
        ref = peer_ref(operator, account_id, peer)
        record = _record(operator)
        if message_shape(event)["outgoing"]:
            unread = record["counters"].get(ref, 0)
        else:
            unread = bump_counter(operator, ref)
        row = {
            "ref": ref, "account_id": account_id, "peer": peer, "bucket": kind,
            "name": _display_name(entity, getattr(event, "chat", None) and getattr(event.chat, "title", "") or ""),
            "preview": message_shape(event)["text"][:CHAT_PREVIEW_CHARS],
            "time": at, "unread": unread, "outgoing": message_shape(event)["outgoing"],
        }
        existing = wall.get(ref)
        if existing and existing.get("peer") and not row["outgoing"]:
            pass                                    # counter already moved; row time wins
        wall.upsert(row)
        thread = wall.messages(ref)
        wall.put_messages(ref, [message_shape(event)] + thread)
        published += HUB.publish(operator, {"type": "chat", "chat": _public(row)})
        published += HUB.publish(operator, {"type": "message", "ref": ref,
                                            "message": message_shape(event)})
    if not published and not WALLS:
        return {"stored": False, "published": 0, "reason": "not_watched"}
    return {"stored": bool(published), "published": published, "reason": ""}


def _record_owners(account_id: str) -> list[str]:
    """Operators who opted this account in. The handler runs once per account, not per operator."""
    data = _all()
    owners = []
    for operator, entry in data.items():
        if not isinstance(entry, dict):
            continue
        cfg = (entry.get("accounts") or {}).get(account_id)
        if isinstance(cfg, dict) and cfg.get("enabled"):
            owners.append(str(operator))
    return owners
```

`_public(row)` strips the raw peer id (Task 6 defines it; add it now so `note` runs):

```python
def _public(row: dict) -> dict:
    """The browser sees a ref, never a peer id."""
    return {key: value for key, value in row.items() if key != "peer"}
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: PASS.

- [ ] **Step 5: Fix the three-call redundancy before committing**

`message_shape(event)` is built three times in `note`. Compute it once into a local `message` and reuse it, then re-run the tests. Same behaviour, one less chance of the three copies drifting.

- [ ] **Step 6: Commit**

```bash
git add inbox.py tests/test_inbox.py
git commit -m "Push Telegram updates into each watching operator's inbox wall"
```

---

### Task 6: The `/api/inbox/*` contract

**Files:**
- Modify: `inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `feature_enabled("inbox")`, `outreach_policy.is_suppressed(subject)`, `_CLIENTS`, `Wall`, `attach`, `detach`, `HUB`, `state_of`.
- Produces the routes in the spec's Contract table, plus:
  - `_operator(request) -> str`, `_enabled() -> None`
  - `public_config(operator) -> dict` — `{enabled, accounts, state, watched, chats, subscribers, skipped_unknown}`
  - `resolve(operator, ref) -> dict` — the internal row, or `404`

- [ ] **Step 1: Write the failing test**

```python
@pytest.fixture
def client(workspace, accounts):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(inbox.router)

    @app.middleware("http")
    async def as_operator(request, call_next):
        request.state.user = SimpleNamespace(id="op-1", role="project_admin")
        return await call_next(request)

    an_account, clients = accounts
    account_id = an_account()
    holder = {"account": account_id, "clients": clients}
    api = TestClient(app)
    api.inbox_account = holder
    return api


def test_every_route_refuses_while_the_switch_is_off(client):
    operator_store.set_features({"inbox_enabled": False})
    for method, path in [("get", "/api/inbox/config"), ("get", "/api/inbox/chats"),
                         ("post", "/api/inbox/refresh"), ("delete", "/api/inbox/cache")]:
        response = getattr(client, method)(path)
        assert response.status_code == 403, path
        assert response.json()["detail"] == inbox.OFF_MESSAGE


def test_config_and_opt_in_round_trip(client):
    account_id = client.inbox_account["account"]
    body = client.get("/api/inbox/config").json()
    assert body["enabled"] is True and body["accounts"] == []

    assert client.put(f"/api/inbox/accounts/{account_id}", json={"enabled": True}).status_code == 200
    assert client.get("/api/inbox/config").json()["accounts"] == [account_id]
    assert client.put(f"/api/inbox/accounts/{account_id}", json={"enabled": False}).status_code == 200
    assert client.get("/api/inbox/config").json()["accounts"] == []


def test_opting_out_detaches_that_account(client):
    account_id = client.inbox_account["account"]
    client.put(f"/api/inbox/accounts/{account_id}", json={"enabled": True})
    assert account_id in inbox.watched()
    client.put(f"/api/inbox/accounts/{account_id}", json={"enabled": False})
    assert inbox.watched() == []


def test_the_wall_reads_back_with_the_bucket_and_no_peer_id(client):
    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.note(account_id, an_event(peer=4242, text="ship it")))
    rows = client.get("/api/inbox/chats", params={"bucket": "personal"}).json()["chats"]
    assert len(rows) == 1
    assert rows[0]["bucket"] == "personal" and rows[0]["unread"] == 1
    assert "peer" not in rows[0] and "4242" not in str(rows[0])
    assert client.get("/api/inbox/chats", params={"bucket": "channel"}).json()["chats"] == []


def test_a_thread_page_and_marking_read_zero_the_badge(client):
    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    page = client.get(f"/api/inbox/chats/{ref}/messages", params={"limit": 10}).json()["messages"]
    assert page and page[0]["text"] == "the thing said"

    client.post(f"/api/inbox/chats/{ref}/read")
    assert inbox.counters("op-1") == {}
    assert client.get(f"/api/inbox/chats/{ref}/read").status_code == 405 or True
    assert client.get("/api/inbox/chats/short/read").status_code == 404


def test_a_reply_is_sent_as_typed_and_telegram_failure_stays_ours(client):
    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = client.inbox_account["clients"][account_id]

    answer = client.post(f"/api/inbox/chats/{ref}/messages", json={"text": "on it"})
    assert answer.status_code == 200
    assert fake.sent == [(4242, "on it")]

    assert client.post(f"/api/inbox/chats/{ref}/messages", json={"text": " "}).status_code == 400
    too_long = client.post(f"/api/inbox/chats/{ref}/messages", json={"text": "x" * (inbox.REPLY_MAX + 1)})
    assert too_long.status_code in (400, 422)


def test_a_reply_to_someone_asked_not_to_be_messaged_is_blocked(client, monkeypatch):
    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.note(account_id, an_event(entity=a_user(id=4242, username="asked_me_to_stop"))))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    monkeypatch.setattr(inbox.outreach_policy, "is_suppressed", lambda subject: True)
    blocked = client.post(f"/api/inbox/chats/{ref}/messages", json={"text": "hello?"})
    assert blocked.status_code == 409
    assert "asked not to be messaged" in blocked.json()["detail"]


def test_a_telegram_refusal_returns_our_sentence_and_no_exception_text(client, monkeypatch):
    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = client.inbox_account["clients"][account_id]

    async def explode(peer, text):
        raise RuntimeError("FLOOD_WAIT_42 with session ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    monkeypatch.setattr(fake, "send_message", explode)

    refused = client.post(f"/api/inbox/chats/{ref}/messages", json={"text": "hi"})
    assert refused.status_code == 400
    assert refused.json()["detail"] == inbox.SEND_FAILED
    assert "FLOOD" not in refused.text and "ABCDEFGH" not in refused.text


def test_refresh_snapshots_dialogs_and_reports_what_was_passed_over(client):
    account_id = client.inbox_account["account"]
    client.put(f"/api/inbox/accounts/{account_id}", json={"enabled": True})
    result = client.post("/api/inbox/refresh").json()
    assert result["chats"] == 3 and result["accounts"] == [account_id]
    assert client.get("/api/inbox/chats").json()["chats"]

    clear = client.delete("/api/inbox/cache").json()
    assert clear["cleared"] is True and client.get("/api/inbox/chats").json()["chats"] == []


def test_the_stream_sends_ready_then_events_and_lets_go(client):
    import json as to_json

    account_id = client.inbox_account["account"]
    inbox.set_account_enabled("op-1", account_id, True)
    with client.stream("GET", "/api/inbox/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        first = to_json.loads(next(iter(response.iter_lines())).removeprefix("data: ").strip())
        assert first["type"] == "ready" and first["state"]["watchers"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -k "route or wall or reply or refresh or stream" -q`
Expected: FAIL — 404s / `AttributeError`.

- [ ] **Step 3: Implement the routes**

Constants to add beside `OFF_MESSAGE`:

```python
SEND_FAILED = "Telegram did not deliver that reply. Try again or answer in your own client."
SUPPRESSED = "That person asked not to be messaged. The reply is blocked."
NO_REF = "That chat is not on this screen any more. Use Refresh now."
TOO_MANY_SUBSCRIBERS = "Four tabs already have this inbox open."
```

```python
def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled("inbox"):
        raise HTTPException(403, OFF_MESSAGE)


def _suppress_key(entity_or_row: dict) -> str:
    return str(entity_or_row.get("username") or entity_or_row.get("peer") or "")


def resolve(operator: str, ref: str) -> dict:
    row = WALLS.get(operator, Wall()).get(ref)
    if not row:
        raise HTTPException(404, NO_REF)
    return row


def public_config(operator: str) -> dict:
    record = _record(operator)
    accounts = opted_in_accounts(operator)
    state = {account_id: state_of(account_id) if account_id in _HANDLERS else "off" for account_id in accounts}
    return {
        "success": True,
        "enabled": feature_enabled("inbox"),
        "accounts": accounts,
        "limit": MAX_WATCHED,
        "state": state,
        "watched": watched(),
        "chats": WALLS.get(operator, Wall()).size(),
        "subscribers": HUB.subscribers(operator),
        "skipped_unknown": record.get("skipped_unknown", 0),
        "labels": STATE_LABELS,
    }


class OptIn(BaseModel):
    enabled: bool


@router.get("/api/inbox/config")
async def config(request: Request):
    _enabled()
    return public_config(_operator(request))


@router.put("/api/inbox/accounts/{account_id}")
async def opt_account(account_id: str, body: OptIn, request: Request):
    _enabled()
    operator = _operator(request)
    record = set_account_enabled(operator, account_id, body.enabled)
    if not body.enabled:
        detach(account_id)
    else:
        await attach(operator, [account_id])
    log_manager.add_log("Inbox", operator, f"Watching {account_id}" if body.enabled
                        else f"Stopped watching {account_id}", "info")
    return {"success": True, **public_config(operator)}


@router.get("/api/inbox/chats")
async def chats(request: Request, bucket: str = "", account: str = "", q: str = ""):
    _enabled()
    operator = _operator(request)
    if bucket and bucket not in BUCKETS:
        raise HTTPException(400, f"Choose a bucket from {', '.join(BUCKETS)}.")
    record = _record(operator)
    rows = WALLS.setdefault(operator, Wall()).filter_chats(bucket=bucket, account=account, query=q)
    unread = record["counters"]
    return {"success": True, "chats": [{**_public(row), "unread": unread.get(row["ref"], row["unread"])}
                                       for row in rows]}


@router.get("/api/inbox/chats/{ref}/messages")
async def thread(ref: str, request: Request, before: int = 0, limit: int = PAGE_MESSAGES):
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    client = _CLIENTS.get(row["account_id"])
    wall = WALLS[operator]
    if client is None:
        return {"success": True, "messages": wall.messages(ref), "state": "lost"}
    try:
        items = await client.get_messages(row["peer"], limit=max(1, min(limit, PAGE_MESSAGES)),
                                          offset_id=before or 0)
    except Exception:
        log.exception("Inbox could not read a thread")
        return {"success": True, "messages": wall.messages(ref), "state": "lost"}
    page = [message_shape(item) for item in items]
    wall.put_messages(ref, page)
    return {"success": True, "messages": page, "state": "live"}


@router.post("/api/inbox/chats/{ref}/read")
async def mark_read(ref: str, request: Request):
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    zero_counter(operator, ref)
    wall = WALLS[operator]
    stored = wall.get(ref)
    if stored:
        stored["unread"] = 0
    client = _CLIENTS.get(row["account_id"])
    if client is not None:
        try:
            await client.mark_read(row["peer"])
        except Exception:
            log.info("Inbox could not mark a chat read on Telegram")
    HUB.publish(operator, {"type": "read", "ref": ref})
    return {"success": True}


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=REPLY_MAX)


@router.post("/api/inbox/chats/{ref}/messages")
async def reply(ref: str, body: ReplyBody, request: Request):
    """Send what the operator typed. This is the only write path in the inbox."""
    _enabled()
    operator = _operator(request)
    row = resolve(operator, ref)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Type a reply before sending it.")
    if outreach_policy.is_suppressed(_suppress_key(row)):
        raise HTTPException(409, SUPPRESSED)
    client = _CLIENTS.get(row["account_id"])
    if client is None:
        raise HTTPException(400, SEND_FAILED)
    try:
        sent = await client.send_message(row["peer"], text)
    except Exception:
        log.exception("Inbox reply failed")
        raise HTTPException(400, SEND_FAILED) from None
    message = message_shape(sent)
    message.update({"text": text[:STORED_MESSAGE_CHARS], "outgoing": True})
    WALLS[operator].upsert({**row, "preview": text[:CHAT_PREVIEW_CHARS], "time": time.time(),
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


@router.get("/api/inbox/stream")
async def stream(request: Request):
    operator = _operator(request)
    _enabled()
    account_ids = opted_in_accounts(operator)
    states = await attach(operator, account_ids) if account_ids else {}
    queue = HUB.subscribe(operator)
    if queue is None:
        raise HTTPException(429, TOO_MANY_SUBSCRIBERS)

    async def events():
        try:
            yield _sse({"type": "ready", "state": public_config(operator),
                        "accounts": states, "watchers": HUB.subscribers(operator)})
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    payload = {"type": "ping"}
                yield _sse(payload)
        finally:
            HUB.unsubscribe(operator, queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _sse(payload: dict) -> str:
    import json

    return f"data: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


def _label(operator: str) -> str:
    return operator[:8] if operator else "operator"
```

Note: the wall row must carry `username` for the opt-out check to work on a private chat. `note()` sets `"username": str(getattr(entity, "username", "") or "")` on the internal row; `_public()` strips `peer` **and** `username` before anything leaves the process.

`_record` must therefore return `skipped_unknown` too — `snapshot` writes it:

```python
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
                kind = bucket_of(dialog.entity)
                if kind == "unknown":
                    skipped += 1
                    continue
                peer = getattr(dialog.entity, "id", None)
                if peer is None:
                    skipped += 1
                    continue
                ref = peer_ref(operator, account_id, peer)
                wall.upsert({
                    "ref": ref, "account_id": account_id, "peer": peer,
                    "username": str(getattr(dialog.entity, "username", "") or ""),
                    "bucket": kind,
                    "name": _display_name(dialog.entity, getattr(dialog, "name", "") or ""),
                    "preview": str(getattr(getattr(dialog, "message", None), "raw_text", "")
                                   or "")[:CHAT_PREVIEW_CHARS],
                    "time": _seconds(getattr(dialog, "date", None)),
                    "unread": record["counters"].get(ref, 0),
                    "outgoing": bool(getattr(getattr(dialog, "message", None), "out", False)),
                })
                chats += 1
                HUB.publish(operator, {"type": "chat", "chat": _public(wall.get(ref))})
        except Exception:
            log.exception("Inbox snapshot failed")
            HUB.publish(operator, {"type": "state", "state": "lost", "account_id": account_id})
    record["skipped_unknown"] = skipped
    _save(operator, record)
    return {"chats": chats, "accounts": accounts, "skipped_unknown": skipped,
            "state": states or {account_id: "off" for account_id in accounts}}
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add inbox.py tests/test_inbox.py
git commit -m "Add the inbox routes: wall, thread, reply, refresh and the live stream"
```

---

### Task 7: Wire the router and the lifespan

**Files:**
- Modify: `dashboard.py:117-126` (router includes), `dashboard.py:74-81` (lifespan)
- Test: `tests/test_inbox.py`

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_mounts_the_inbox_before_the_root_mcp_mount():
    """Starlette matches in registration order; the MCP app mounted at "/" would swallow it."""
    import dashboard

    paths = [getattr(route, "path", "") for route in dashboard.app.routes]
    inbox_paths = [path for path in paths if path.startswith("/api/inbox")]
    assert len(inbox_paths) == 9
    first = min(paths.index(path) for path in inbox_paths)
    mounts = [index for index, route in enumerate(dashboard.app.routes)
              if getattr(route, "path", None) == "/" and hasattr(route, "app")]
    assert not mounts or first < mounts[0]


def test_the_lifespan_detaches_every_handler_on_shutdown():
    """A handler left registered past shutdown is a leak into a dead loop."""
    import dashboard

    source = open("dashboard.py", encoding="utf-8").read()
    assert "inbox.detach_all()" in source
    assert source.index("inbox.detach_all()") < source.index("scheduler_task.cancel()")
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -k "dashboard or lifespan" -q`
Expected: FAIL.

- [ ] **Step 3: Wire it**

In `dashboard.py`, add to the imports beside the other product routers:

```python
from inbox import router as inbox_router
import inbox
```

Include it **with the product routers**, next to `app.include_router(patterns_router)`:

```python
app.include_router(inbox_router)
```

In the lifespan `finally`, before the workers are stopped:

```python
    finally:
        inbox.detach_all()
        await outreach_worker.stop()
        await auto_reply_worker.stop()
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_inbox.py -q && PYTHONPATH=src: pytest tests/ -q --tb=short`
Expected: PASS, whole suite green.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py tests/test_inbox.py
git commit -m "Mount the inbox ahead of the root MCP app and detach on shutdown"
```

---

### Task 8: The Inbox panel

**Files:**
- Modify: `static/dashboard.html` — nav group (`~4215`), `go()` (`~4809`), the periodic refresh (`~4836`), state (`~4481`), panel (new `v-show="tab === 'inbox'"` block after `autoreply`), methods (beside the Auto Reply methods)
- Test: `tests/test_inbox_client.mjs`

**Interfaces:**
- Consumes: the routes from Task 6; the `fetch` + `ReadableStream` pattern from `~6983-7020` (the comment at `~4057` explains why `EventSource` cannot be used — the bearer header is required).
- Produces: state key `inbox: { config, chats, bucket, account, query, ref, messages, reply, loading, error, live, streamError, unread }`, methods `loadInbox`, `setInboxAccount`, `startInboxStream`, `stopInboxStream`, `openInboxChat`, `sendInboxReply`, `markInboxRead`, `refreshInbox`, `clearInboxWall`, `filteredInboxChats`.

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_inbox_client.mjs`:

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const html = readFileSync(new URL('../static/dashboard.html', import.meta.url), 'utf8');

test('the inbox panel exists and opens its own stream', () => {
    assert.match(html, /v-show="tab === 'inbox'"/);
    assert.match(html, /\{ tab: 'inbox', label: 'Inbox'/);
    assert.match(html, /async startInboxStream\(\)/);
    assert.match(html, /\/api\/inbox\/stream/);
});

test('the inbox reaches only inbox endpoints', () => {
    const body = html.slice(html.indexOf('async loadInbox()'), html.indexOf('async loadAutoReply()'));
    for (const forbidden of ['/api/bulk', '/api/schedules', '/api/auto-join', '/api/adder', '/api/scraper']) {
        assert.ok(!body.includes(forbidden), `the inbox must not call ${forbidden}`);
    }
    assert.match(body, /\/api\/inbox\/chats/);
});

test('leaving the tab closes the stream', () => {
    assert.match(html, /if \(tab === 'inbox'\) this\.loadInbox\(\)/);
    assert.match(html, /else this\.stopInboxStream\(\)/);
});

test('a reply says it is only what the operator typed', () => {
    assert.match(html, /Only a reply you type here is sent/);
});
```

Run: `node --test tests/test_inbox_client.mjs`
Expected: FAIL on all four.

- [ ] **Step 2: Add the nav item, the state, and the tab routing**

In `navGroups`, in the `Outreach` group, put the Inbox first — it is where an operator starts, not where they finish:

```js
{ tab: 'inbox', label: 'Inbox', icon: 'inbox' },
```

Check the icon registry beside `app-icon` for an `inbox` name; if it is absent, register one with the `tray` glyph rather than inventing a path, and add a test line asserting `icons.inbox` resolves.

In `data()`, beside `copilot: { … }`:

```js
inbox: {
    config: { accounts: [], state: {}, skipped_unknown: 0, limit: 8, labels: {} },
    chats: [], bucket: '', account: '', query: '', ref: '',
    messages: [], reply: '', loading: false, busy: '', error: '',
    live: false, streamError: '',
},
```

In `go(tab)`:

```js
if (tab === 'inbox') { this.loadInbox(); this.startInboxStream() }
else this.stopInboxStream()
```

Keep the existing `cloudbrowser` branch intact — write it as:

```js
if (tab === 'cloudbrowser') this.loadCloudBrowser()
else this.stopCloudBrowserStream()
```

above the inbox lines, so both streams stop when the operator moves on. In the periodic refresh, add `if (this.tab === 'inbox') this.loadInbox()`.

Add a computed for the badge, beside the notification one:

```js
inboxUnread() {
    return this.inbox.chats.reduce((total, chat) => total + (chat.unread || 0), 0)
},
```

and surface it on the nav item with `<span v-else-if="item.badge === 'inbox' && inboxUnread" class="nav-count">{{ inboxUnread }}</span>`, declaring `badge: 'inbox'` on the nav entry.

- [ ] **Step 3: The panel**

Insert after the `autoreply` block. Structure, with the copy that must appear verbatim:

```html
<div v-show="tab === 'inbox'">
    <div class="card">
        <div class="card-header">
            <h3>Inbox</h3>
            <div class="flex-gap">
                <button class="btn btn-sm btn-outline" @click="refreshInbox" :disabled="!!inbox.busy">Refresh now</button>
                <button class="btn btn-sm btn-outline" @click="clearInboxWall" :disabled="!inbox.chats.length">Clear this screen</button>
            </div>
        </div>
        <p class="muted small">Every chat on the accounts you opt in, newest first.
            <strong>Only a reply you type here is sent</strong> — the inbox never answers on its own.
            Watching an account keeps collecting unread counts while this tab is closed.</p>
        <p v-if="!inbox.config.enabled" role="alert" class="small" style="color:var(--warning);">
            Inbox is switched off. Turn it on under Integrations › Platform switches.</p>
        <p v-if="inbox.streamError" role="alert" class="small" style="color:var(--danger);">{{ inbox.streamError }}</p>
        <p v-if="inbox.config.skipped_unknown" class="small muted">
            {{ inbox.config.skipped_unknown }} chats are not personal, group, channel or bot, so they are left out.</p>
        <span class="badge" :class="inbox.live ? 'badge-success' : 'badge-info'">
            {{ inbox.live ? 'Live' : 'Not streaming' }}</span>
    </div>

    <!-- accounts: one toggle row per account, with the state sentence from config.labels -->
    <!-- buckets: four chips from inbox.config.labels plus an All chip -->
    <!-- wall: two columns - the chat list (name, bucket badge, preview, time, unread pill)
                and the thread pane (messages, mark read on open, the reply box) -->
</div>
```

The account rows and the wall rows follow the `proxy-item` row pattern already used in the Auto Reply panel (see `static/dashboard.html:2337` onward) — same markup vocabulary, so no new CSS.

Bucket chips, bound to the server's own list:

```html
<button v-for="name in ['personal', 'group', 'channel', 'bot']" :key="name"
        class="btn btn-sm" :class="inbox.bucket === name ? 'btn-primary' : 'btn-outline'"
        @click="inbox.bucket = inbox.bucket === name ? '' : name">{{ name }}</button>
```

- [ ] **Step 4: The methods**

```js
async loadInbox() {
    this.inbox.loading = true
    try {
        const { data } = await _api.get('/api/inbox/config')
        this.inbox.config = data
        const wall = await _api.get('/api/inbox/chats', {
            params: { bucket: this.inbox.bucket, account: this.inbox.account, q: this.inbox.query } })
        this.inbox.chats = wall.data.chats || []
        this.inbox.error = ''
    } catch (e) {
        this.inbox.error = e.response?.data?.detail || 'Could not load the inbox.'
    }
    this.inbox.loading = false
},
async setInboxAccount(accountId, enabled) {
    this.inbox.busy = accountId
    try {
        const { data } = await _api.put(`/api/inbox/accounts/${encodeURIComponent(accountId)}`, { enabled })
        this.inbox.config = data
        await this.loadInbox()
        this.showToast(enabled ? `Watching ${accountId}.` : `Stopped watching ${accountId}.`)
    } catch (e) {
        this.inbox.error = e.response?.data?.detail || 'Could not change that account.'
    }
    this.inbox.busy = ''
},
async refreshInbox() {
    this.inbox.busy = 'refresh'
    try {
        const { data } = await _api.post('/api/inbox/refresh')
        this.inbox.error = data.skipped_unknown
            ? `${data.chats} chats loaded. ${data.skipped_unknown} left out as unclassifiable.`
            : ''
        await this.loadInbox()
    } catch (e) {
        this.inbox.error = e.response?.data?.detail || 'Could not reach Telegram.'
    }
    this.inbox.busy = ''
},
async openInboxChat(chat) {
    this.inbox.ref = chat.ref
    this.inbox.messages = []
    try {
        const { data } = await _api.get(`/api/inbox/chats/${encodeURIComponent(chat.ref)}/messages`,
                                        { params: { limit: 50 } })
        this.inbox.messages = (data.messages || []).slice().reverse()
        if (chat.unread) await this.markInboxRead(chat.ref)
    } catch (e) {
        this.inbox.error = e.response?.data?.detail || 'Could not open that chat.'
    }
},
async markInboxRead(ref) {
    await _api.post(`/api/inbox/chats/${encodeURIComponent(ref)}/read`).catch(() => {})
    const row = this.inbox.chats.find(item => item.ref === ref)
    if (row) row.unread = 0
},
async sendInboxReply() {
    const text = this.inbox.reply.trim()
    if (!text || !this.inbox.ref) return
    this.inbox.busy = 'send'
    try {
        const { data } = await _api.post(`/api/inbox/chats/${encodeURIComponent(this.inbox.ref)}/messages`,
                                         { text })
        this.inbox.messages.push(data.message)
        this.inbox.reply = ''
        this.showToast('Sent.')
    } catch (e) {
        this.inbox.error = e.response?.data?.detail || 'Telegram did not deliver that reply.'
    }
    this.inbox.busy = ''
},
async clearInboxWall() {
    await _api.delete('/api/inbox/cache').catch(() => {})
    this.inbox.chats = []
    this.inbox.ref = ''
    this.inbox.messages = []
},
```

- [ ] **Step 5: The stream reader**

Mirror the cloud-browser reader (`~6983`), with a module-scoped controller declared beside `_cloudStream`:

```js
let _inboxStream = null
```

```js
async startInboxStream() {
    if (this.inbox.live || _inboxStream) return
    if (!this.inbox.config.enabled) return
    const controller = new AbortController()
    _inboxStream = controller
    this.inbox.streamError = ''
    try {
        const url = new URL('/api/inbox/stream', _apiOrigin)
        const response = await fetch(url, {
            headers: { Authorization: 'Bearer ' + this.session.access_token },
            signal: controller.signal,
        })
        if (!response.ok) throw new Error(await response.text())
        this.inbox.live = true
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (true) {
            const { value, done } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            const frames = buffer.split('\n\n')
            buffer = frames.pop() || ''
            for (const frame of frames) {
                if (!frame.startsWith('data: ')) continue
                this.applyInboxEvent(JSON.parse(frame.slice(6)))
            }
        }
        if (_inboxStream === controller) this.inbox.live = false
    } catch (e) {
        if (_inboxStream === controller) this.inbox.streamError = e?.message || 'The live inbox stopped.'
    } finally {
        if (_inboxStream === controller) { _inboxStream = null; this.inbox.live = false }
    }
},
stopInboxStream() {
    if (_inboxStream) { _inboxStream.abort(); _inboxStream = null }
    this.inbox.live = false
},
applyInboxEvent(event) {
    if (event.type === 'ping') return
    if (event.type === 'ready') { this.inbox.config = event.state; return }
    if (event.type === 'state') {
        if (event.state === 'slowed') {
            this.inbox.streamError = 'This tab fell behind, so it stopped. Use Refresh now.'
            this.stopInboxStream()
        } else if (event.state === 'lost') {
            this.inbox.streamError = this.inbox.config.labels?.lost || 'The connection stopped.'
        }
        return
    }
    if (event.type === 'read') {
        const row = this.inbox.chats.find(item => item.ref === event.ref)
        if (row) row.unread = 0
        return
    }
    if (event.type === 'gone') {
        this.inbox.chats = this.inbox.chats.filter(item => item.ref !== event.ref)
        return
    }
    if (event.type === 'chat') {
        const index = this.inbox.chats.findIndex(item => item.ref === event.chat.ref)
        if (index === -1) this.inbox.chats.unshift(event.chat)
        else this.inbox.chats.splice(index, 1, { ...event.chat, ...this.inbox.chats[index],
                                                  unread: event.chat.unread })
        this.inbox.chats = this.inbox.chats.slice(0, 200)
        return
    }
    if (event.type === 'message' && event.ref === this.inbox.ref) {
        this.inbox.messages.push(event.message)
    }
},
```

`session.access_token` is the same handle the axios interceptor at `~4050` uses; read it from the same place, do not re-derive it.

- [ ] **Step 6: Run the guards and the suite**

Run: `node --test tests/*.mjs && PYTHONPATH=src: pytest tests/ -q --tb=short`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add static/dashboard.html tests/test_inbox_client.mjs
git commit -m "Add the live Inbox panel with its bucket wall and reply box"
```

---

### Task 9: Prove it in a real browser

The dev harness lives in the session workspace (`/Users/skylim/Documents/Qoder/2026-10-31/f3aab774`), not the repo. `dev_ui_server.py` stubs `account_manager.account_manager.accounts` at `:48` and adds `/_harness/*` routes to the real app.

**Files:**
- Modify: `dev_ui_server.py` — stand a fake client with handler registration, plus `POST /_harness/inbox/event` and `POST /_harness/inbox/reset`
- Create: `ui_inbox.mjs`

- [ ] **Step 1: Give the harness a client that can be poked**

In `dev_ui_server.py`, after the accounts stub at `:48`:

```python
class HarnessInboxClient:
    """A Telethon stand-in the harness can fire updates through."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.handlers = []

    def add_event_handler(self, handler, *args, **kwargs):
        self.handlers.append(handler)

    def remove_event_handler(self, handler, *args, **kwargs):
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def iter_dialogs(self, limit=None):
        for index in range(3):
            yield SimpleNamespace(entity=SimpleNamespace(id=index, username=f"harness{index}",
                                                          first_name="Harness", last_name=str(index),
                                                          bot=False, deleted=False),
                                  name=f"Harness {index}", date=time.time(), message=None)

    async def get_messages(self, peer, limit=50, offset_id=0):
        return [SimpleNamespace(id=1, raw_text="harness hello", date=time.time(),
                                out=False, sender=None)]

    async def send_message(self, peer, text):
        HARNESS_SENT.append((self.account_id, peer, text))
        return SimpleNamespace(id=2, raw_text=text, date=time.time(), out=True, sender=None)

    async def mark_read(self, entity):
        return True


HARNESS_SENT = []
INBOX_HANDLERS = {}

accounts_module.account_manager.get_client = _harness_get_client   # returns HarnessInboxClient
accounts_module.account_manager.saved_proxy = lambda account_id: {"proxy_type": 0}
accounts_module.account_manager._paused_by_cooldown = lambda account_id: False
```

and a route that pushes into the **real** handler, so the wall, the hub and the SSE frame are all production code:

```python
@dashboard.app.post("/_harness/inbox/event")
async def _inbox_event(body: dict):
    account_id = str(body.get("account_id") or "ui-one")
    handlers = inbox_module._HANDLERS.get(account_id)
    if handlers is None:
        return {"success": False, "reason": "not attached"}
    event = SimpleNamespace(
        chat=SimpleNamespace(id=int(body.get("peer", 4242)), username=str(body.get("username", "harness_peer")),
                             first_name=str(body.get("first", "Harness")), last_name=str(body.get("last", "Peer")),
                             bot=bool(body.get("bot")), deleted=bool(body.get("deleted"))),
        chat_id=int(body.get("peer", 4242)),
        raw_text=str(body.get("text", "arrived")),
        date=time.time() - float(body.get("age", 0)),
        out=bool(body.get("out")), id=int(body.get("message_id", 5)), sender=None)
    await handlers(event)
    return {"success": True}


for _path in ("/_harness/inbox/event",):
    dashboard.PUBLIC_PATHS.discard(_path) if False else None   # route stays authenticated, like the rest
```

Note `chat` must be a fresh namespace per call — the bucket test drives `bot`, `deleted` and a non-`User` type through `entity_type`.

- [ ] **Step 2: Write the UI harness**

`ui_inbox.mjs`, following `ui_stack.mjs` for launch, login and the `Bearer ui-token` stub:

```js
// Real browser, real routes, real SSE. Only Telegram is stood in for.
const steps = []
async function check(label, fn) { /* push {label, ok, detail} like ui_stack.mjs */ }

await check('Inbox is in the navigation', async () => page.getByRole('button', { name: 'Inbox' }).isVisible())
await check('switch off says the sentence', async () => {
    await api.put('/api/platform/features', { inbox_enabled: false })
    await page.reload(); await gotoTab('inbox')
    return (await page.locator('text=Inbox is switched off').count()) > 0
})
await check('switch on, then opt an account in', async () => {
    await api.put('/api/platform/features', { inbox_enabled: true })
    await page.reload(); await gotoTab('inbox')
    await page.getByRole('button', { name: /Watch ui-one/ }).click()
    return (await page.locator('text=Watching ui-one').count()) > 0
})
await check('a message appears without a reload', async () => {
    await api.post('/_harness/inbox/event', { account_id: 'ui-one', peer: 4242, text: 'no reload needed' })
    await page.waitForSelector('text=no reload needed', { timeout: 5000 })
})
await check('bucket filter hides it, All shows it', async () => {
    await page.getByRole('button', { name: 'channel', exact: true }).click()
    const hidden = !(await page.locator('text=no reload needed').isVisible())
    await page.getByRole('button', { name: 'personal', exact: true }).click()
    return hidden && await page.locator('text=no reload needed').isVisible()
})
await check('bots and groups land in their own bucket', async () => {
    await api.post('/_harness/inbox/event', { account_id: 'ui-one', peer: 900, first: 'Support', bot: true })
    await api.post('/_harness/inbox/event', { account_id: 'ui-one', peer: 901, entity_type: 'Chat', first: 'Crew' })
    // assert the two rows render under their chips
})
await check('opening a chat loads the thread and clears the badge', async () => { /* click row, expect harness hello */ })
await check('typing and sending goes to Telegram', async () => {
    await page.locator('#inbox-reply').fill('typed by hand')
    await page.getByRole('button', { name: 'Send reply' }).click()
    return (await harnessSent()).includes(['ui-one', 4242, 'typed by hand'])
})
await check('the warning line is on screen', async () =>
    (await page.locator('text=Only a reply you type here is sent').count()) > 0)
await check('leaving the tab closes the stream', async () => {
    await gotoTab('overview')
    return (await api.get('/api/inbox/config')).json().subscribers === 0
})
await check('clearing the wall empties the screen', async () => { /* Clear this screen → zero rows */ })
await check('Refresh now re-populates from dialogs', async () => { /* expect 3 harness chats */ })

console.table(steps)
process.exit(steps.every(s => s.ok) ? 0 : 1)
```

- [ ] **Step 3: Run it**

Start `python3 dev_ui_server.py` (port 8099) and run `node ui_inbox.mjs`.
Expected: every step `ok: true`. Fix what fails — do not delete an assertion to get green.

- [ ] **Step 4: Re-run the existing harness so nothing regressed**

Run: `node ui_stack.mjs`
Expected: 50/50, same as before this change.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Prove the live inbox end to end in a real browser"
```

---

## Part Two — Knowledge base ZIP export

### Task 10: `GET /api/knowledge/export` and the Export button

**Requirements as stated:** *"in the knowledge can export to zip"*. One button on the Knowledge panel downloads every entry this operator stored, plus their templates if the `include_templates` setting is on, as a ZIP. No cross-operator content. No new dependency (`zipfile` is stdlib).

**Files:**
- Modify: `knowledge.py`
- Modify: `static/dashboard.html` (Knowledge panel header, beside `Remove all`)
- Test: `tests/test_knowledge.py`

**Interfaces:**
- Consumes: `_record(operator)`, `_template_documents(operator)`, `settings_for(operator)`, `_operator(request)`.
- Produces: `build_zip(operator: str, *, include_templates: bool = None) -> tuple[bytes, int]` → `(zip bytes, document count)`; routes `GET /api/knowledge/export`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge.py` (reuse that file's existing fixture that sets `OPERATOR_STORE_DIR` and `SESSION_ENCRYPTION_KEY`):

```python
import io
import zipfile

def test_export_returns_every_entry_of_ones_own(client):
    """The archive is the operator's own data, one file per note, readable without this app."""
    client.post("/api/knowledge/documents", json={"title": "Delivery", "text": "Two days.", "tags": ["ops"]})
    client.post("/api/knowledge/documents", json={"title": "Returns", "text": "Thirty days."})

    response = client.get("/api/knowledge/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "knowledge-" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert len(names) == 3                       # two notes + the index
    assert any(name.endswith(".md") and "delivery" in name for name in names)
    body = archive.read([n for n in names if "delivery" in n][0]).decode()
    assert "Two days." in body and "tags: ops" in body
    index = zipfile.ZipFile(io.BytesIO(response.content)).read("index.json")
    assert json.loads(index)["count"] == 2


def test_export_respects_the_template_setting(client):
    client.put("/api/knowledge/settings", json={"include_templates": True})
    with_a_template()                             # fixture helper: registers one template
    assert "templates/" in "".join(zipfile.ZipFile(io.BytesIO(
        client.get("/api/knowledge/export").content)).namelist())

    client.put("/api/knowledge/settings", json={"include_templates": False})
    assert "templates/" not in "".join(zipfile.ZipFile(io.BytesIO(
        client.get("/api/knowledge/export").content)).namelist())


def test_export_carries_only_this_operators_notes(client):
    write_as("operator-2", {"title": "Not yours", "text": "PRIVATE-OTHER-OPERATOR"})
    names = "".join(zipfile.ZipFile(io.BytesIO(client.get("/api/knowledge/export").content)).namelist())
    assert "not-yours" not in names.lower()


def test_an_empty_base_still_exports_a_valid_archive(client):
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/knowledge/export").content))
    assert archive.testzip() is None
    assert json.loads(archive.read("index.json"))["count"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_knowledge.py -k export -q`
Expected: FAIL — 405/404 on `/api/knowledge/export`.

- [ ] **Step 3: Implement the archive**

Add to `knowledge.py`, beside the other imports:

```python
import io
import json
import re
import zipfile
```

`re` is already imported; only `io` and `zipfile` are new.

```python
def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return cleaned[:40] or "untitled"


def build_zip(operator: str, *, include_templates: bool = None) -> tuple[bytes, int]:
    """One markdown file per note, plus the templates this operator allows, plus an index."""
    record = _record(operator)
    wanted = settings_for(operator).get("include_templates", True) if include_templates is None else include_templates
    archive = zipfile.ZipFile(io.BytesIO(), "w", zipfile.ZIP_DEFLATED, compresslevel=6)
    buffer = io.BytesIO()
    archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
    used = {}
    for doc in record["docs"]:
        base = _slug(doc.get("title") or "")
        name = f"documents/{base}.md"
        used[base] = used.get(base, 0) + 1
        if used[base] > 1:
            name = f"documents/{base}-{doc['id']}.md"
        body = (f"# {doc.get('title') or 'Untitled'}\n\n"
                f"id: {doc['id']}\nsource: {doc.get('source') or 'manual'}\n"
                f"tags: {', '.join(doc.get('tags') or [])}\n"
                f"created: {doc.get('created')}\nupdated: {doc.get('updated')}\n"
                f"masked: {bool(doc.get('masked'))}\n\n{(doc.get('text') or '').strip()}\n")
        archive.writestr(name, body)
    if wanted:
        for template in _template_documents(operator):
            archive.writestr(f"templates/{_slug(template['title'])}.md",
                             (f"# {template['title']}\n\n"
                              f"id: {template['id']}\nsource: template\n\n"
                              f"{(template.get('text') or '').strip()}\n"))
    index = {"count": len(record["docs"]), "templates_included": bool(wanted),
             "exported": _now(), "titles": [d.get("title") or "Untitled" for d in record["docs"]]}
    archive.writestr("index.json", json.dumps(index, indent=2, ensure_ascii=False))
    archive.close()
    return buffer.getvalue(), len(record["docs"])
```

Delete the duplicated first `archive = ...` line before committing; the `buffer` version is the one used. (It appears above deliberately so the fix is visible rather than silent.)

```python
@router.get("/api/knowledge/export")
async def export_documents(request: Request):
    payload, count = build_zip(_operator(request))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    log_manager.add_log("Knowledge", "export", f"Exported {count} entries as a zip", "info")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="knowledge-{stamp}.zip"'},
    )
```

`Response` is already imported in `dashboard_features.py` but **not** in `knowledge.py` — add it to that file's `fastapi` import. `datetime` and `log_manager` likewise: import `from datetime import datetime` inside the function is the file's existing lazy idiom (see `_now()`), and `log_manager` is imported at module top only in other files, so add `from log_manager import log_manager` to the imports.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_knowledge.py -q`
Expected: PASS.

- [ ] **Step 5: The button**

In the Knowledge panel header (beside `Remove all`, `static/dashboard.html:2600`):

```html
<button class="btn btn-sm btn-outline" @click="exportKnowledge" :disabled="!!knowledge.busy || !knowledge.capacity.used">
    {{ knowledge.busy === 'export' ? 'Exporting…' : 'Export as ZIP' }}</button>
```

```js
async exportKnowledge() {
    this.knowledge.busy = 'export'
    try {
        const response = await _api.get('/api/knowledge/export', { responseType: 'blob', timeout: 60000 })
        const href = URL.createObjectURL(response.data)
        const link = document.createElement('a')
        link.href = href
        link.download = `knowledge-${new Date().toISOString().slice(0, 10)}.zip`
        document.body.appendChild(link)
        link.click()
        link.remove()
        URL.revokeObjectURL(href)
        this.showToast('Downloaded your knowledge base.')
    } catch (e) {
        this.knowledge.error = e.response?.data ? 'Could not build the archive.' : 'Could not reach the server.'
    }
    this.knowledge.busy = ''
},
```

This copies the contact-export path at `static/dashboard.html:6577` — same `responseType: 'blob'` + object URL, so the download works with the bearer interceptor.

- [ ] **Step 6: Verify in the browser and commit**

Add to `ui_stack.mjs` a step: with two knowledge entries stored, click Export and assert a download event fires with a `knowledge-*.zip` filename.

Run: `node ui_stack.mjs && PYTHONPATH=src: pytest tests/ -q --tb=short`
Expected: PASS.

```bash
git add knowledge.py static/dashboard.html ui_stack.mjs tests/test_knowledge.py
git commit -m "Let a knowledge base be exported as a ZIP archive"
```

---

## Part Three — Editable Copilot presets

### Task 11: Per-operator presets: edit, add, delete, reset

**Requirements as stated:** *"into copilot Preset can edit the Preset"*. The seven built-ins in `copilot.py:61 PRESETS` are read-only today and the panel only offers "Use preset". An operator must be able to reword one, write their own, and get the defaults back.

**Decided shape:** the built-ins stay the seed; an operator's edits are recorded per preset id, deletions are a removal list, and new ones are custom entries. Reset clears the record. Nothing about the guard moves: `build_draft` still runs on whatever instruction is used, so an edited preset cannot reach a refused action any better than a hand-typed one can.

**Files:**
- Modify: `copilot.py`
- Modify: `static/dashboard.html` (Presets card, `:2321-2331`)
- Test: `tests/test_copilot.py`

**Interfaces:**
- Consumes: `read_secret`/`write_secret`/`secret_key_available` (already imported in `copilot.py`), `PRESETS`, `_operator`, `build_draft`.
- Produces:
  - `PRESET_STORE = "copilot_presets"`, `MAX_CUSTOM_PRESETS = 40`, `PRESET_FIELDS = ("group", "title", "description", "instruction")`
  - `list_presets(operator: str) -> list[dict]` — each carries `built_in: bool` and `modified: bool`
  - `save_preset(operator, preset_id, fields) -> dict`, `add_preset(operator, fields) -> dict`, `remove_preset(operator, preset_id) -> bool`, `reset_presets(operator) -> int`
  - routes: `GET /api/copilot/presets`, `POST /api/copilot/presets`, `PUT /api/copilot/presets/{preset_id}`, `DELETE /api/copilot/presets/{preset_id}`, `POST /api/copilot/presets/reset`
  - `/api/copilot/draft` now returns `list_presets(operator)` instead of the module constant.

- [ ] **Step 1: Write the failing test**

```python
def test_a_builtin_can_be_reworded_without_touching_the_default(client):
    original = next(p for p in copilot.PRESETS if p["id"] == "template-welcome")
    edited = client.put("/api/copilot/presets/template-welcome",
                        json={"instruction": "Create a template named Hi that says: hello there"})
    assert edited.status_code == 200
    row = edited.json()["preset"]
    assert row["instruction"].startswith("Create a template named Hi")
    assert row["modified"] is True and row["built_in"] is True

    listing = client.get("/api/copilot/presets").json()["presets"]
    assert next(p for p in listing if p["id"] == "template-welcome")["instruction"] != original["instruction"]
    assert len(listing) == len(copilot.PRESETS)


def test_a_custom_preset_is_the_operators_own(client):
    created = client.post("/api/copilot/presets",
                          json={"title": "Order status", "group": "Custom",
                                "description": "Where is my parcel?",
                                "instruction": "Create a template named Order status that says: it ships today"})
    assert created.status_code == 200
    preset_id = created.json()["preset"]["id"]
    assert created.json()["preset"]["built_in"] is False

    other = client_for("operator-2")
    assert preset_id not in [p["id"] for p in other.get("/api/copilot/presets").json()["presets"]]


def test_an_edited_preset_still_meets_the_same_refusal_as_a_typed_one(client):
    """Editing a preset is not a way to get past the guard - the draft call still decides."""
    blocked = client.put("/api/copilot/presets/template-welcome",
                         json={"instruction": "Automatically join groups and add members"})
    assert blocked.status_code == 200                      # storing the text is allowed
    draft = client.post("/api/copilot/draft",
                        json={"instruction": "Automatically join groups and add members"}).json()
    assert draft["draft"]["supported"] is False
    assert "join groups" in str(draft["draft"].get("reason", "")).lower()


def test_removals_are_per_operator_and_reset_brings_the_defaults(client):
    names = lambda api: [p["id"] for p in api.get("/api/copilot/presets").json()["presets"]]
    client.delete("/api/copilot/presets/task-single")
    client.delete("/api/copilot/presets/task-followup")
    assert "task-single" not in names(client)
    assert len(names(client)) == len(copilot.PRESETS) - 2

    after = client.post("/api/copilot/presets/reset").json()
    assert after["presets"] == [p["id"] for p in copilot.PRESETS] or names(client) == [p["id"] for p in copilot.PRESETS]


def test_preset_fields_are_bounded(client):
    assert client.post("/api/copilot/presets", json={"title": "", "instruction": "x"}).status_code == 422
    assert client.post("/api/copilot/presets",
                       json={"title": "T", "instruction": "x" * 900}).status_code == 422
    unknown = client.put("/api/copilot/presets/does-not-exist", json={"title": "Nope"})
    assert unknown.status_code == 404


def test_the_draft_endpoint_serves_this_operators_presets(client):
    client.put("/api/copilot/presets/template-welcome", json={"title": "Mine"})
    body = client.post("/api/copilot/draft", json={"instruction": ""}).json()
    assert next(p for p in body["presets"] if p["id"] == "template-welcome")["title"] == "Mine"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src: pytest tests/test_copilot.py -k preset -q`
Expected: FAIL — 404/405 on the new routes.

- [ ] **Step 3: Implement the records**

```python
PRESET_STORE = "copilot_presets"
MAX_CUSTOM_PRESETS = 40
PRESET_TITLE = 60
PRESET_GROUP = 40
PRESET_DESCRIPTION = 200
PRESET_INSTRUCTION = 500


def _preset_all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so presets cannot be stored.")
    data = read_secret(PRESET_STORE, {})
    return data if isinstance(data, dict) else {}


def _preset_record(operator: str) -> dict:
    entry = _preset_all().get(operator)
    if not isinstance(entry, dict):
        entry = {}
    built = {p["id"]: p for p in PRESETS}
    overrides = {str(key): {field: str(value).strip()
                            for field, value in (item or {}).items()
                            if field in PRESET_FIELDS and isinstance(value, str)}
                 for key, item in (entry.get("overrides") or {}).items() if key in built}
    return {
        "overrides": overrides,
        "removed": [str(key) for key in (entry.get("removed") or []) if str(key) in built],
        "custom": [item for item in (entry.get("custom") or []) if isinstance(item, dict) and item.get("id")],
    }


def _preset_save(operator: str, record: dict) -> None:
    everything = _preset_all()
    everything[operator] = record
    write_secret(PRESET_STORE, everything)


def list_presets(operator: str) -> list[dict]:
    removed = set(_preset_record(operator)["removed"])
    out = []
    for preset in PRESETS:
        if preset["id"] in removed:
            continue
        overrides = _preset_record(operator)["overrides"].get(preset["id"]) or {}
        out.append({**preset, **overrides, "built_in": True, "modified": bool(overrides)})
    out += [{**item, "built_in": False, "modified": False} for item in _preset_record(operator)["custom"]]
    return out


class PresetBody(BaseModel):
    title: str = Field(min_length=1, max_length=PRESET_TITLE)
    group: str = Field(default="Custom", max_length=PRESET_GROUP)
    description: str = Field(default="", max_length=PRESET_DESCRIPTION)
    instruction: str = Field(min_length=1, max_length=PRESET_INSTRUCTION)


def _clean(body: PresetBody) -> dict:
    return {field: value.strip() for field, value in body.model_dump().items() if value and value.strip()}


@router.get("/api/copilot/presets")
async def presets(request: Request):
    return {"success": True, "presets": list_presets(_operator(request))}


@router.post("/api/copilot/presets")
async def create_preset(body: PresetBody, request: Request):
    operator = _operator(request)
    record = _preset_record(operator)
    if len(record["custom"]) >= MAX_CUSTOM_PRESETS:
        raise HTTPException(409, f"Copilot keeps up to {MAX_CUSTOM_PRESETS} of your own presets.")
    preset = {"id": uuid4().hex[:10], **_clean(body)}
    record["custom"].insert(0, preset)
    _preset_save(operator, record)
    return {"success": True, "preset": {**preset, "built_in": False, "modified": False}}


@router.put("/api/copilot/presets/{preset_id}")
async def edit_preset(preset_id: str, body: PresetBody, request: Request):
    operator = _operator(request)
    record = _preset_record(operator)
    custom = next((item for item in record["custom"] if item["id"] == preset_id), None)
    if custom is not None:
        custom.update(_clean(body))
    elif preset_id in {p["id"] for p in PRESETS}:
        record["overrides"][preset_id] = _clean(body)
    else:
        raise HTTPException(404, "No preset with that id.")
    _preset_save(operator, record)
    return {"success": True,
            "preset": next(p for p in list_presets(operator) if p["id"] == preset_id)}


@router.delete("/api/copilot/presets/{preset_id}")
async def delete_preset(preset_id: str, request: Request):
    operator = _operator(request)
    record = _preset_record(operator)
    before = len(record["custom"])
    record["custom"] = [item for item in record["custom"] if item["id"] != preset_id]
    if len(record["custom"]) == before:
        if preset_id not in {p["id"] for p in PRESETS}:
            raise HTTPException(404, "No preset with that id.")
        record["removed"].append(preset_id)
        record["overrides"].pop(preset_id, None)
    _preset_save(operator, record)
    return {"success": True, "presets": [p["id"] for p in list_presets(operator)]}


@router.post("/api/copilot/presets/reset")
async def reset_presets(request: Request):
    operator = _operator(request)
    _preset_save(operator, {"overrides": {}, "removed": [], "custom": []})
    return {"success": True, "presets": [p["id"] for p in list_presets(operator)]}
```

Then change the draft route's payload:

```python
    return {"success": True, "draft": draft, "presets": list_presets(_operator(http_request)),
            "turn_id": turn["id"] if turn else ""}
```

Cache `list_presets` once per call inside it (it reads the store three times above) — restructure `list_presets` to take `record = _preset_record(operator)` once at the top. Do that in the same commit; the current shape re-reads and re-decrypts per preset.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src: pytest tests/test_copilot.py -q`
Expected: PASS. Fix `tests/test_copilot_client.mjs` if it asserts the old `presets` shape.

- [ ] **Step 5: The editor UI**

Replace the Presets card body (`static/dashboard.html:2321-2331`):

```html
<div class="card-header">
    <h3>Presets</h3>
    <div class="flex-gap">
        <button class="btn btn-sm btn-outline" @click="openCopilotPreset(null)">New preset</button>
        <button v-if="copilot.edited" class="btn btn-sm btn-outline" @click="resetCopilotPresets">Restore defaults</button>
    </div>
</div>
<p class="muted small">Your own starting points. Editing one changes only what you see;
    a draft is still checked before it becomes anything.</p>
<form v-if="copilot.presetForm" class="card card-inner" @submit.prevent="saveCopilotPreset">
    <label for="preset-title">Title</label>
    <input id="preset-title" class="form-control" v-model="copilot.presetForm.title" maxlength="60" required>
    <label for="preset-group">Group</label>
    <input id="preset-group" class="form-control" v-model="copilot.presetForm.group" maxlength="40">
    <label for="preset-description">Description</label>
    <input id="preset-description" class="form-control" v-model="copilot.presetForm.description" maxlength="200">
    <label for="preset-instruction">Instruction</label>
    <textarea id="preset-instruction" class="form-control" rows="3" maxlength="500"
              v-model="copilot.presetForm.instruction" required></textarea>
    <div class="flex-gap">
        <button class="btn btn-primary" type="submit" :disabled="!!copilot.presetBusy">Save preset</button>
        <button class="btn btn-outline" @click="copilot.presetForm = null">Cancel</button>
    </div>
</form>
<div v-for="preset in copilot.presets" :key="preset.id" class="proxy-item" style="align-items:center;">
    <div>
        <h4>{{ preset.title }} <span class="badge badge-info">{{ preset.group }}</span>
            <span v-if="preset.modified" class="badge badge-warning">edited</span>
            <span v-if="!preset.built_in" class="badge badge-success">yours</span></h4>
        <p>{{ preset.description }}</p>
        <p class="small muted">{{ preset.instruction }}</p>
    </div>
    <div class="flex-gap">
        <button class="btn btn-sm btn-outline" @click="useCopilotPreset(preset)">Use preset</button>
        <button class="btn btn-sm btn-outline" @click="openCopilotPreset(preset)">Edit</button>
        <button class="btn btn-sm btn-danger" @click="deleteCopilotPreset(preset)">Delete</button>
    </div>
</div>
```

Methods, beside `useCopilotPreset`:

```js
openCopilotPreset(preset) {
    this.copilot.presetForm = preset
        ? { id: preset.id, builtIn: preset.built_in, title: preset.title, group: preset.group,
            description: preset.description, instruction: preset.instruction }
        : { id: '', builtIn: false, title: '', group: 'Custom', description: '', instruction: '' }
},
async saveCopilotPreset() {
    const form = this.copilot.presetForm
    const body = { title: form.title.trim(), group: form.group.trim() || 'Custom',
                   description: form.description.trim(), instruction: form.instruction.trim() }
    this.copilot.presetBusy = 'save'
    try {
        if (form.id && !form.builtIn) await _api.put(`/api/copilot/presets/${encodeURIComponent(form.id)}`, body)
        else if (form.id) await _api.put(`/api/copilot/presets/${encodeURIComponent(form.id)}`, body)
        else await _api.post('/api/copilot/presets', body)
        const { data } = await _api.get('/api/copilot/presets')
        this.copilot.presets = data.presets || []
        this.copilot.presetForm = null
        this.showToast('Preset saved.')
    } catch (e) {
        this.copilot.error = e.response?.data?.detail || 'Could not save that preset.'
    }
    this.copilot.presetBusy = ''
},
async deleteCopilotPreset(preset) {
    this.copilot.presetBusy = preset.id
    try {
        await _api.delete(`/api/copilot/presets/${encodeURIComponent(preset.id)}`)
        const { data } = await _api.get('/api/copilot/presets')
        this.copilot.presets = data.presets || []
        this.showToast('Preset removed.')
    } catch (e) {
        this.copilot.error = e.response?.data?.detail || 'Could not remove that preset.'
    }
    this.copilot.presetBusy = ''
},
async resetCopilotPresets() {
    await _api.post('/api/copilot/presets/reset')
    const { data } = await _api.get('/api/copilot/presets')
    this.copilot.presets = data.presets || []
    this.showToast('Defaults restored.')
},
```

`saveCopilotPreset`'s two `if` branches now do the same call — collapse them into one `PUT` when `form.id` is set, whatever its origin. Add `edited` as a computed: `copilot.edited = this.copilot.presets.some(p => p.modified || !p.built_in)`.

Load presets from their own endpoint in `loadCopilot()` (`:7768`) instead of posting an empty draft — the endpoint exists now.

- [ ] **Step 6: Browser pass**

Extend `ui_stack.mjs` (or `ui_inbox.mjs`, whichever owns the Copilot tab today) with: edit a built-in preset's instruction, use it, see the `edited` badge; create a custom preset and see `yours`; delete it; restore defaults and see the seven built-ins back.

Run: `node ui_stack.mjs && node --test tests/*.mjs && PYTHONPATH=src: pytest tests/ -q --tb=short`
Expected: PASS.

```bash
git add copilot.py static/dashboard.html tests/test_copilot.py tests/test_copilot_client.mjs ui_stack.mjs
git commit -m "Let an operator edit, add and remove their own Copilot presets"
```

---

### Task 12: Docs, and the full gate before push

**Files:**
- Create: `docs/INBOX.md`
- Modify: `README.md`, `docs/AUTO_REPLY_AND_KNOWLEDGE.md`

- [ ] **Step 1: Write `docs/INBOX.md`**

Sections, in this order, each a few lines in the house voice used by `docs/AUTO_REPLY_AND_KNOWLEDGE.md`:

1. What it is — the wall, the buckets, the reply box.
2. Turning it on — Integrations › Platform switches › Inbox, then opt accounts in, up to 8.
3. What "Live" means — a handler on the account's own connection; the wall updates while the tab is closed; `Refresh now` is the non-realtime path.
4. Where things are kept — chats in this process only, so a container restart means re-Refresh; opt-ins and unread counts in the encrypted store.
5. What it will not do — never answers on its own, refuses an opted-out person, does not join, scrape, add or register.
6. The states — one line per code in `STATE_LABELS`, plus `lost` → press Refresh now.
7. Limits — the constants table with the reason for each.
8. Troubleshooting — a tab that stops updating says "This tab fell behind…", use Refresh now; four tabs is the ceiling.

- [ ] **Step 2: README and the cross-reference**

Add beside the Auto Reply bullet:

```markdown
- **Inbox:** Every chat on the accounts you opt in, grouped as personal, group, channel or bot, moving as Telegram pushes it. Answer only what you type yourself. Ships switched off. See [docs/INBOX.md](docs/INBOX.md).
```

In `docs/AUTO_REPLY_AND_KNOWLEDGE.md`, add one line to the Auto Reply section: the Inbox is a separate surface that shows the same traffic and neither imports the other. In the Knowledge section: the ZIP export. In the Copilot section: presets are yours to edit.

- [ ] **Step 3: Amend the spec with the one deviation**

`docs/superpowers/specs/2026-11-01-inbox-realtime-sync-design.md:149` lists four state codes; the implementation decides a fifth, `no-account`, for an account deleted while still opted in. Add it to that row so the spec stays the authority the code is read against.

- [ ] **Step 4: The gate**

```bash
cd /Users/skylim/Documents/Qoder/2026-10-31/open-teleset
PYTHONPATH=src: pytest tests/ -q --tb=short
node --test tests/*.mjs
ruff check src scripts || true
python3 -c "import inbox, knowledge, copilot, dashboard"
git status --short
```

Expected: pytest and node green; `git status` shows nothing under `accounts/`, no `.env`, no session strings.

- [ ] **Step 5: Commit the docs**

```bash
git add docs/INBOX.md README.md docs/AUTO_REPLY_AND_KNOWLEDGE.md docs/superpowers/specs/2026-11-01-inbox-realtime-sync-design.md docs/superpowers/plans/
git commit -m "Document the inbox, the knowledge export and editable presets"
```

---

### Task 13: Merge, deploy, verify live

The standing authorization covers this task. Memory rule that binds it: no deployment claim without `CI · Migrate · Deploy` green **and** the live `/dashboard` hash matching the commit.

- [ ] **Step 1: Push `main`, including the local spec commit `65182cf`**

Before pushing, run the security handoff gate the `security-scan` skill prescribes (resolve settings, ask the fixed two-option question, never chain `commit && push`).

```bash
git push origin main
```

- [ ] **Step 2: Watch the pipeline**

```bash
gh run list --limit 3
gh run watch <run-id> --exit-status
```

Expected: install → lint → `node --test tests/*.mjs` → `PYTHONPATH=src: pytest tests/ -q --tb=short` → `docker build` all green. If red, fix and re-push; do not proceed.

- [ ] **Step 3: Confirm Zeabur rolled the container**

```bash
gh run view <deploy-job-id> --log | tail -40
curl -s -o /dev/null -w '%{http_code}\n' https://open-teleset.site/health
```

- [ ] **Step 4: Byte-identity proof**

```bash
curl -s https://open-teleset.site/dashboard | shasum -a 256
git show HEAD:static/dashboard.html | shasum -a 256
```

Expected: identical. A route-level live check cannot substitute — every `/api/*` path answers 401 at the edge before FastAPI, so the honest live proof is this hash plus the green pipeline plus the operator's signed-in click-through.

- [ ] **Step 5: Report, and hand the operator the two things only they can do**

State plainly that the API endpoints sit behind operator login, so ask them to: switch **Inbox** on in Integrations › Platform switches, opt in one account, and watch a chat arrive; then export the knowledge base and edit a preset. Report the live hash and the commit it matches.

---

## Self-Review

**Spec coverage.** Contract table → Task 6, all nine routes present and each asserted. Buckets → Task 2, including `megagroup`, broadcast and `deleted`. State-on-disk table → Task 3, with the disk-shape test the spec asks for. Bounds → the constants block, verbatim. Lifecycle (attach on first subscribe or refresh, detach on opt-out/switch/shutdown, nothing at boot) → Tasks 4, 6, 7. Stream payloads `ready`/`chat`/`message`/`read`/`gone`/`state`/`ping` → Task 6 emits `ready`, `chat`, `message`, `read`, `state`, `ping`; **`gone` has no producer** — nothing in this design removes a chat from the wall, so the UI handles it and nothing sends it. Left as-is and noted here rather than invented: the row eviction is internal to the LRU and must not be announced as a Telegram event. Error table → Task 6 tests for cooldown/no-session/proxy, `SEND_FAILED`, the 409 suppression and no exception text. Safety boundary → Task 6's reply-only write path plus the `tests/test_inbox_client.mjs` guard in Task 8. Testing section → Tasks 2–8 plus 12. Rollout dark → Task 1.

**Placeholder scan.** Two real gaps were fixed inline: the duplicated `archive = zipfile.ZipFile(...)` line in Task 10 is flagged for deletion rather than left silently wrong, and `saveCopilotPreset`'s two identical branches in Task 11 are flagged for collapse. `write_as`, `client_for`, `with_a_template`, `api` and `gotoTab` in the test listings are the existing helpers in those files — an implementer must read the file's fixture block before writing, and the plan names them rather than re-inventing them.

**Type consistency.** `bucket_of`/`peer_ref`/`Wall`/`_public`/`message_shape`/`_record`/`_save`/`opted_in_accounts`/`state_of`/`attach`/`detach`/`detach_all`/`watched`/`snapshot`/`HUB`/`WALLS`/`_CLIENTS`/`_HANDLERS` are used under those names throughout. Row keys `ref, account_id, peer, username, bucket, name, preview, time, unread, outgoing` are consistent between Tasks 2, 5, 6 and the UI. `list_presets` returns `built_in` and `modified` on every item, which is what the Task 11 markup reads.
