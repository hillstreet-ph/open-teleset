"""The inbox classifies, remembers briefly, and forgets on purpose.

Two invariants carry the safety weight: a chat is filed by the type Telegram actually
gave it, and nothing a third party said is ever written to disk. The wall lives in this
process, so a container restart costs a re-read rather than leaving a corpus behind.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import inbox
from open_teleset import operator_store
from outreach_policy import OutreachPolicy


class User(SimpleNamespace):
    """Named to match Telethon's class name: bucket_of reads the type, as _private() does."""


class Chat(SimpleNamespace):
    """A basic group."""


class Channel(SimpleNamespace):
    """A broadcast channel or a megagroup, told apart by flags."""


def a_user(pid=11, username="alice_x", first="Alice", last="Lee", bot=False, deleted=False):
    return User(id=pid, username=username, first_name=first, last_name=last, bot=bot, deleted=deleted)


def a_channel(pid=22, megagroup=False, broadcast=False):
    return Channel(id=pid, megagroup=megagroup, broadcast=broadcast, title="Announcements")


@pytest.mark.parametrize(
    "entity,expected",
    [
        (a_user(), "personal"),
        (a_user(bot=True), "bot"),
        (a_user(deleted=True), "unknown"),
        (a_user(bot=True, deleted=True), "bot"),
        (Chat(id=33), "group"),
        (a_channel(megagroup=True), "group"),
        (a_channel(broadcast=True), "channel"),
        (a_channel(), "unknown"),
        (User(id=5, bot=False, deleted=False), "personal"),
        (SimpleNamespace(id=1), "unknown"),
        (None, "unknown"),
    ],
)
def test_bucket_of_files_each_telegram_type(entity, expected):
    assert inbox.bucket_of(entity) == expected


def test_bucket_of_reads_the_class_name_not_an_isinstance():
    """The harness fakes are SimpleNamespaces; isinstance against TL types would bury them."""
    assert inbox.bucket_of(User(id=1, bot=False, deleted=False)) == "personal"
    assert inbox.bucket_of(Chat(id=1)) == "group"


def test_peer_ref_is_operator_scoped_stable_and_short():
    one = inbox.peer_ref("op-1", "acct-1", 4242)
    assert one == inbox.peer_ref("op-1", "acct-1", 4242)
    assert len(one) == inbox.PEER_REF_LENGTH
    assert one != inbox.peer_ref("op-2", "acct-1", 4242)
    assert one != inbox.peer_ref("op-1", "acct-2", 4242)
    assert "4242" not in one


def a_row(ref="r1", *, account="a", bucket="personal", name="Alice Lee", preview="", when=1.0,
          unread=0, outgoing=False, peer=1):
    return {"ref": ref, "account_id": account, "peer": peer, "username": "", "bucket": bucket,
            "name": name, "preview": preview, "time": when, "unread": unread, "outgoing": outgoing}


def test_wall_orders_newest_first_and_caps_at_max_chats():
    wall = inbox.Wall()
    for index in range(inbox.MAX_CHATS + 5):
        wall.upsert(a_row(f"r{index}", name=f"chat {index}", when=float(index), peer=index))
    rows = wall.rows()
    assert len(rows) == inbox.MAX_CHATS
    assert rows[0]["ref"] == f"r{inbox.MAX_CHATS + 4}"
    assert wall.get("r0") is None


def test_wall_evicts_a_chats_messages_with_the_chat():
    wall = inbox.Wall()
    for index in range(inbox.MAX_CHATS):
        wall.upsert(a_row(f"r{index}", when=float(index), peer=index))
    # r0 was inserted first and the wall prepends, so it sits at the end and is the oldest row
    wall.put_messages("r0", [{"id": 1, "text": "cached", "time": 1.0, "outgoing": False, "sender": ""}])
    assert len(wall.messages("r0")) == 1
    wall.upsert(a_row("newcomer", when=float(inbox.MAX_CHATS), peer=inbox.MAX_CHATS))
    assert wall.size() == inbox.MAX_CHATS
    assert wall.get("r0") is None
    assert wall.messages("r0") == []


def test_wall_replaces_a_row_without_reordering_the_rest():
    wall = inbox.Wall()
    wall.upsert(a_row("x1", name="Old", when=1.0))
    wall.upsert(a_row("x2", name="New", when=2.0))
    wall.upsert(a_row("x1", name="Old", preview="now with a preview", when=3.0))
    assert [row["ref"] for row in wall.rows()] == ["x1", "x2"]
    assert wall.get("x1")["preview"] == "now with a preview"


def test_wall_filters_by_bucket_account_and_chat_name_only():
    wall = inbox.Wall()
    wall.upsert(a_row("x1", name="Alice Lee", preview="about the invoice", when=3.0))
    wall.upsert(a_row("x2", account="b", bucket="channel", name="News", preview="Alice Lee said it",
                      when=2.0, peer=2))
    assert [row["ref"] for row in wall.filter_chats(bucket="personal")] == ["x1"]
    assert [row["ref"] for row in wall.filter_chats(account="b")] == ["x2"]
    assert [row["ref"] for row in wall.filter_chats(query="alice")] == ["x1"]
    assert [row["ref"] for row in wall.filter_chats(query="news")] == ["x2"]
    # a preview hit must not qualify: message-history search is out of scope
    assert wall.filter_chats(query="invoice") == []
    assert wall.filter_chats(bucket="channel", account="b") == [wall.get("x2")]


def test_a_thread_page_for_a_chat_that_fell_off_the_wall_is_dropped():
    """Otherwise _messages grows past MAX_CHATS while the rows do not."""
    wall = inbox.Wall()
    wall.put_messages("never-seen", [{"id": 1, "text": "m", "time": 1.0, "outgoing": False, "sender": ""}])
    assert wall.messages("never-seen") == []


def test_a_thread_page_is_capped_at_one_telegram_page():
    wall = inbox.Wall()
    wall.upsert(a_row("x1"))
    items = [{"id": index, "text": "m", "time": float(index), "outgoing": False, "sender": ""}
             for index in range(inbox.PAGE_MESSAGES + 20)]
    wall.put_messages("x1", items)
    assert len(wall.messages("x1")) == inbox.PAGE_MESSAGES


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # A reply reads the opt-out ledger before sending. Point it at an empty ledger in
    # this test's own directory, or the read lands on the developer's real file and the
    # suite passes locally while failing on a clean checkout where that file is absent.
    ledger = tmp_path / "outreach_policy.json"
    ledger.write_text(json.dumps({"version": 1, "approvals": {}, "consents": [], "suppressions": []}))
    monkeypatch.setattr(inbox, "outreach_policy", OutreachPolicy(ledger))
    operator_store.set_features({"inbox_enabled": True})
    monkeypatch.setattr(inbox.log_manager, "add_log", lambda *a, **k: None)
    inbox.WALLS.clear()
    inbox.HUB._queues.clear()
    yield tmp_path
    inbox.HUB._queues.clear()


def test_opting_in_is_capped_and_survives_a_reload(workspace):
    for index in range(inbox.MAX_WATCHED):
        inbox.set_account_enabled("op-1", f"acct-{index}", True)
    assert len(inbox.opted_in_accounts("op-1")) == inbox.MAX_WATCHED

    with pytest.raises(HTTPException) as refusal:
        inbox.set_account_enabled("op-1", "acct-overflow", True)
    assert refusal.value.status_code == 409

    inbox.WALLS.clear()
    assert inbox.opted_in_accounts("op-1") == [f"acct-{index}" for index in range(inbox.MAX_WATCHED)]

    inbox.set_account_enabled("op-1", "acct-0", False)
    assert "acct-0" not in inbox.opted_in_accounts("op-1")
    assert len(inbox.opted_in_accounts("op-1")) == inbox.MAX_WATCHED - 1


def test_only_opt_ins_and_counters_reach_the_disk(workspace):
    """The disk shape is the privacy contract: assert it, do not trust the writer."""
    inbox.set_account_enabled("op-1", "acct-0", True)
    inbox.bump_counter("op-1", "ref-abc")
    wall = inbox.WALLS.setdefault("op-1", inbox.Wall())
    wall.upsert(a_row("ref-abc", account="acct-0", peer=90210, name="Alice Lee",
                      preview="the private thing she said", unread=1, when=time.time()))

    raw = (workspace / "inbox.secret.json").read_text()
    assert "Alice" not in raw and "private thing" not in raw and "90210" not in raw
    stored = operator_store.read_secret("inbox", {})
    assert stored["op-1"]["counters"] == {"ref-abc": 1}
    assert stored["op-1"]["accounts"] == {"acct-0": {"enabled": True}}
    assert "preview" not in str(stored["op-1"]) and "name" not in str(stored["op-1"])


def test_counters_are_per_operator(workspace):
    inbox.set_account_enabled("op-1", "acct-0", True)
    inbox.bump_counter("op-1", "shared-ref")
    assert inbox.counters("op-1") == {"shared-ref": 1}
    assert inbox.counters("op-2") == {}
    inbox.zero_counter("op-1", "shared-ref")
    assert inbox.counters("op-1") == {}


def test_a_corrupt_record_falls_back_to_nothing_watched(workspace):
    operator_store.write_secret("inbox", {"op-1": {"accounts": "not-a-dict", "counters": [1]}})
    assert inbox.opted_in_accounts("op-1") == []
    assert inbox.counters("op-1") == {}


class FakePhotoSize:
    def __init__(self, payload):
        self.size = len(payload)
        self.payload = payload


class FakePhoto:
    """A real photo carries one entry per thumbnail and picks the biggest itself."""

    def __init__(self, payload, thumb=b"t"):
        self.sizes = [FakePhotoSize(thumb), FakePhotoSize(payload)]

    def get_biggest(self):
        return self.sizes[-1]


class DocumentAttributeFilename(SimpleNamespace):
    """Named to match Telethon: _file_name reads the attribute by type, as the real client sets it."""


class FakeDocument:
    def __init__(self, payload, mime="application/pdf", name=""):
        self.size = len(payload)
        self.mime_type = mime
        self.attributes = [DocumentAttributeFilename(file_name=name)] if name else []
        # Only the fake connection knows the bytes; the product never reads this back.
        self.payload = payload
        # Handles a fake would happily leak, to prove the descriptor never carries one.
        self.file_id = "AgADbTestFileId"
        self.access_hash = 1234567890123


FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"a tiny picture"
FAKE_PDF = b"%PDF-1.4 fake body"


def an_attachment(kind, payload=FAKE_PNG, *, name="", mime="", mid=701, text="", peer=4242):
    """One message carrying one file, shaped the way Telethon hands it to get_messages.

    Telegram also sets `document` for every non-photo file, so the fake does the same - that is
    precisely why the descriptor has to check `document` last.
    """
    event = SimpleNamespace(id=mid, raw_text=text, date=time.time(), out=False, sender=None,
                            chat=a_user(pid=peer), chat_id=peer,
                            file_name=name, mime_type=mime, photo=None, sticker=None,
                            voice=None, video_note=None, gif=None, video=None, audio=None,
                            document=None)
    if kind == "photo":
        event.photo = FakePhoto(payload)
    else:
        found = FakeDocument(payload, mime or "application/pdf", name)
        event.document = found
        if kind != "document":
            setattr(event, kind, found)
    return event


class FakeClient:
    """Stands a Telethon client in at the seams the inbox actually touches."""

    def __init__(self, account_id="acct-0", fail_add=False):
        self.account_id = account_id
        self.handlers = []
        self.fail_add = fail_add
        self.removed = []
        self.sent = []
        self.read = 0
        # Telegram resolves an id inside the chat it was read from, so the fake keys the same way.
        self.attachments = {}
        self.threads = {}
        self.downloads = []

    def add_event_handler(self, handler, *args, **kwargs):
        if self.fail_add:
            raise RuntimeError("Telethon refused to register the handler")
        self.handlers.append((handler, args, kwargs))

    def remove_event_handler(self, handler, *args, **kwargs):
        self.removed.append(handler)
        self.handlers = [entry for entry in self.handlers if entry[0] is not handler]

    async def iter_dialogs(self, limit=None):
        for index in range(3):
            yield SimpleNamespace(entity=a_user(pid=100 + index, first=f"Peer{index}", last=""),
                                  name="", message=None, date=time.time() - index,
                                  unread_count=0)
        yield SimpleNamespace(entity=SimpleNamespace(id=999), name="a chat we cannot file",
                              message=None, date=time.time(), unread_count=0)

    async def get_messages(self, peer, limit=50, offset_id=0, ids=None):
        if ids is not None:
            wanted = ids[0] if isinstance(ids, (list, tuple)) else ids
            return self.attachments.get((peer, wanted))
        if peer in self.threads:
            return self.threads[peer]
        return [SimpleNamespace(id=index, raw_text=f"line {index}", date=time.time(),
                                out=False, sender=None) for index in range(min(limit, 5))]

    async def iter_download(self, target, file_size=None, chunk_size=None):
        self.downloads.append({"file_size": file_size, "chunk_size": chunk_size})
        payload = target.payload
        step = int(chunk_size or len(payload)) or len(payload)
        for start in range(0, len(payload), step):
            yield payload[start:start + step]

    async def send_message(self, peer, text):
        self.sent.append((peer, text))
        return SimpleNamespace(id=99, raw_text=text, date=time.time(), out=True, sender=None)

    async def mark_read(self, entity):
        self.read += 1


class UndownloadableClient:
    """A connection with no download method at all. The guard should stop before Telegram is read."""

    def __init__(self, account_id="acct-0"):
        self.account_id = account_id
        self.reads = 0

    async def get_messages(self, peer, limit=50, offset_id=0, ids=None):
        self.reads += 1
        return None



@pytest.fixture
def accounts(monkeypatch, workspace):
    from account_manager import account_manager

    clients = {}

    def an_account(account_id="acct-0", session="saved-session"):
        account_manager.accounts[account_id] = {"id": account_id, "session_string": session}
        clients.setdefault(account_id, FakeClient(account_id))
        return account_id

    async def get_client(account_id="default", proxy=None):
        return clients.get(account_id)

    monkeypatch.setattr(account_manager, "accounts", {})
    monkeypatch.setattr(account_manager, "get_client", get_client)
    monkeypatch.setattr(account_manager, "saved_proxy", lambda account_id: {"proxy_type": 0})
    monkeypatch.setattr(account_manager, "_paused_by_cooldown", lambda account_id: False)
    inbox._CLIENTS.clear()
    inbox._HANDLERS.clear()
    yield an_account, clients
    inbox._CLIENTS.clear()
    inbox._HANDLERS.clear()


def test_state_of_reads_the_blockers_it_can_see_before_connecting(accounts):
    an_account, _ = accounts
    from account_manager import account_manager

    account_id = an_account()
    assert inbox.state_of(account_id) == "live"
    assert inbox.state_of("does-not-exist") == "no-account"

    monkey = account_manager
    monkey._paused_by_cooldown = lambda account_id: True
    assert inbox.state_of(account_id) == "cooldown"

    monkey._paused_by_cooldown = lambda account_id: False
    monkey.accounts[account_id]["session_string"] = ""
    assert inbox.state_of(account_id) == "no-session"

    monkey.accounts[account_id]["session_string"] = "saved-session"

    def broken_proxy(_account_id):
        raise ValueError("the saved proxy is gone")

    monkey.saved_proxy = broken_proxy
    assert inbox.state_of(account_id) == "proxy-unavailable"


def test_attach_registers_one_handler_per_account_not_per_operator(accounts):
    an_account, clients = accounts
    account_id = an_account()
    first = asyncio.run(inbox.attach("op-1", [account_id]))
    second = asyncio.run(inbox.attach("op-2", [account_id]))
    assert first == second == {account_id: "live"}
    assert len(clients[account_id].handlers) == 1
    assert inbox.watched() == [account_id]


def test_a_re_attach_does_not_stack_a_second_handler(accounts):
    an_account, clients = accounts
    account_id = an_account()
    asyncio.run(inbox.attach("op-1", [account_id]))
    asyncio.run(inbox.attach("op-1", [account_id]))
    assert len(clients[account_id].handlers) == 1


def test_attach_reports_a_connection_we_could_not_make(accounts):
    an_account, _ = accounts
    from account_manager import account_manager

    account_id = an_account()
    account_manager.accounts[account_id]["session_string"] = ""
    assert asyncio.run(inbox.attach("op-1", [account_id])) == {account_id: "no-session"}
    assert inbox.watched() == []


def test_attach_reports_a_client_that_telethon_would_not_give_back(accounts):
    an_account, clients = accounts
    account_id = an_account()
    clients[account_id].fail_add = True
    assert asyncio.run(inbox.attach("op-1", [account_id])) == {account_id: "connection-unavailable"}
    assert inbox.watched() == []
    assert inbox._CLIENTS == {}


def test_detach_removes_the_handler_exactly_once(accounts):
    an_account, clients = accounts
    account_id = an_account()
    asyncio.run(inbox.attach("op-1", [account_id]))
    assert inbox.detach(account_id) is True
    assert clients[account_id].removed and not clients[account_id].handlers
    assert inbox.detach(account_id) is False
    assert inbox.detach_all() == 0


def test_switching_off_detaches_everything(accounts):
    an_account, _ = accounts
    asyncio.run(inbox.attach("op-1", [an_account(), an_account("acct-1")]))
    assert len(inbox.watched()) == 2
    assert inbox.detach_all() == 2
    assert inbox.watched() == []


def test_the_hub_caps_subscribers_and_survives_a_slow_tab(accounts):
    assert inbox.HUB.subscribe("op-9") is not None
    for _ in range(inbox.MAX_SUBSCRIBERS - 1):
        inbox.HUB.subscribe("op-9")
    assert inbox.HUB.subscribe("op-9") is None
    assert inbox.HUB.subscribers("op-9") == inbox.MAX_SUBSCRIBERS
    inbox.HUB._queues.pop("op-9", None)


def an_event(peer=4242, text="the thing said", age=0, out=False, *, entity=None):
    return SimpleNamespace(
        chat=entity if entity is not None else a_user(pid=peer),
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
    assert result["stored"] is True and result["published"] == 2
    row = inbox.WALLS["op-1"].rows()[0]
    assert row["bucket"] == "personal" and row["unread"] == 1
    assert row["name"] == "Alice Lee"
    assert row["preview"] == "the thing said"
    payloads = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [p["type"] for p in payloads] == ["chat", "message"]
    assert "peer" not in payloads[0]["chat"]


def test_a_thread_page_grows_from_the_updates_it_sees(accounts):
    account_id = watched_operator(accounts)
    ref = inbox.peer_ref("op-1", account_id, 4242)
    asyncio.run(inbox.note(account_id, an_event(text="first")))
    asyncio.run(inbox.note(account_id, an_event(text="second")))
    page = inbox.WALLS["op-1"].messages(ref)
    assert [item["text"] for item in page] == ["second", "first"]


def test_an_outgoing_message_does_not_make_an_unread(accounts):
    account_id = watched_operator(accounts)
    asyncio.run(inbox.note(account_id, an_event()))
    result = asyncio.run(inbox.note(account_id, an_event(text="my own reply", out=True)))
    row = inbox.WALLS["op-1"].rows()[0]
    assert result["stored"] is True and row["unread"] == 1 and row["outgoing"] is True
    assert inbox.counters("op-1")[row["ref"]] == 1


def test_two_operators_on_one_account_get_different_refs_and_their_own_rows(accounts):
    """The ref is salted with the operator, so two watchers never share one handle."""
    an_account, _ = accounts
    account_id = an_account()
    inbox.set_account_enabled("op-1", account_id, True)
    inbox.set_account_enabled("op-2", account_id, True)
    asyncio.run(inbox.attach("op-1", [account_id]))
    one, two = inbox.HUB.subscribe("op-1"), inbox.HUB.subscribe("op-2")
    asyncio.run(inbox.note(account_id, an_event()))
    assert one.qsize() == two.qsize() == 2
    assert inbox.WALLS["op-1"].rows()[0]["ref"] != inbox.WALLS["op-2"].rows()[0]["ref"]


def test_an_update_older_than_the_stale_window_is_dropped(accounts):
    account_id = watched_operator(accounts)
    queue = inbox.HUB.subscribe("op-1")
    result = asyncio.run(inbox.note(account_id, an_event(age=inbox.STALE_EVENT_SECONDS + 60)))
    assert result["reason"] == "stale" and result["stored"] is False
    assert queue.qsize() == 0 and inbox.WALLS == {}


def test_an_unclassifiable_chat_is_stored_nowhere(accounts):
    account_id = watched_operator(accounts)
    inbox.HUB.subscribe("op-1")
    result = asyncio.run(inbox.note(account_id, an_event(entity=SimpleNamespace(id=5))))
    assert result["reason"] == "unknown_bucket"
    assert inbox.WALLS == {}


def test_a_message_for_an_account_nobody_watched_is_ignored(accounts):
    asyncio.run(inbox.note("acct-never-opted", an_event()))
    assert inbox.WALLS == {}


def test_a_handler_that_raises_never_closes_the_stream(accounts, monkeypatch):
    account_id = watched_operator(accounts)
    queue = inbox.HUB.subscribe("op-1")

    async def explode(*args, **kwargs):
        raise RuntimeError("a chat we could not read")

    monkeypatch.setattr(inbox, "note", explode)
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
    assert slow.qsize() == inbox.QUEUE_MAXSIZE
    drained = [slow.get_nowait() for _ in range(slow.qsize())]
    assert drained[0] == {"type": "ping", "i": 1}          # the oldest frame was discarded
    assert drained[-1] == {"type": "state", "state": "slowed"}
    inbox.HUB._queues.clear()


@pytest.fixture
def api(workspace, accounts):
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
    test_client = TestClient(app)
    test_client.inbox_account = holder
    return test_client


def opted_in_api(api):
    account_id = api.inbox_account["account"]
    api.put(f"/api/inbox/accounts/{account_id}", json={"enabled": True})
    return account_id


def test_every_route_refuses_while_the_switch_is_off(api):
    operator_store.set_features({"inbox_enabled": False})
    for method, path in [("get", "/api/inbox/config"), ("get", "/api/inbox/chats"),
                         ("post", "/api/inbox/refresh"), ("delete", "/api/inbox/cache"),
                         ("get", "/api/inbox/media/r1/1"), ("get", "/api/inbox/stream")]:
        response = getattr(api, method)(path)
        assert response.status_code == 403, path
        assert response.json()["detail"] == inbox.OFF_MESSAGE


def test_config_and_opt_in_round_trip(api):
    account_id = api.inbox_account["account"]
    body = api.get("/api/inbox/config").json()
    assert body["enabled"] is True and body["accounts"] == []
    assert body["labels"]["no-session"] == inbox.STATE_LABELS["no-session"]

    assert api.put(f"/api/inbox/accounts/{account_id}", json={"enabled": True}).status_code == 200
    assert api.get("/api/inbox/config").json()["accounts"] == [account_id]
    assert api.get("/api/inbox/config").json()["state"][account_id] == "live"

    assert api.put(f"/api/inbox/accounts/{account_id}", json={"enabled": False}).status_code == 200
    assert api.get("/api/inbox/config").json()["accounts"] == []


def test_opting_out_detaches_that_account(api):
    account_id = opted_in_api(api)
    assert account_id in inbox.watched()
    api.put(f"/api/inbox/accounts/{account_id}", json={"enabled": False})
    assert inbox.watched() == []


def test_an_unknown_account_and_a_bad_bucket_are_refused_with_our_sentences(api):
    assert api.put("/api/inbox/accounts/ghost", json={"enabled": True}).status_code == 404
    opted_in_api(api)
    refusal = api.get("/api/inbox/chats", params={"bucket": "everything"})
    assert refusal.status_code == 400
    assert "personal" in refusal.json()["detail"]


def test_the_wall_reads_back_with_the_bucket_and_no_peer_id(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242, text="ship it")))
    rows = api.get("/api/inbox/chats", params={"bucket": "personal"}).json()["chats"]
    assert len(rows) == 1
    assert rows[0]["bucket"] == "personal" and rows[0]["unread"] == 1
    assert "peer" not in rows[0] and "username" not in rows[0]
    assert "4242" not in str(rows[0])
    assert api.get("/api/inbox/chats", params={"bucket": "channel"}).json()["chats"] == []


def test_a_thread_page_and_marking_read_zero_the_badge(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]

    page = api.get(f"/api/inbox/chats/{ref}/messages", params={"limit": 10}).json()
    assert page["state"] == "live" and page["messages"][0]["text"].startswith("line")

    assert api.post(f"/api/inbox/chats/{ref}/read").status_code == 200
    assert inbox.counters("op-1") == {}
    assert api.get("/api/inbox/chats").json()["chats"][0]["unread"] == 0
    assert api.post("/api/inbox/chats/short/read").status_code == 404
    assert api.inbox_account["clients"][account_id].read == 1


def test_a_reply_is_sent_as_typed_and_a_refusal_stays_ours(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242)))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = api.inbox_account["clients"][account_id]

    answer = api.post(f"/api/inbox/chats/{ref}/messages", json={"text": "on it"})
    assert answer.status_code == 200
    assert fake.sent == [(4242, "on it")]
    assert answer.json()["message"]["outgoing"] is True

    assert api.post(f"/api/inbox/chats/{ref}/messages", json={"text": " "}).status_code == 400
    too_long = api.post(f"/api/inbox/chats/{ref}/messages", json={"text": "x" * (inbox.REPLY_MAX + 1)})
    assert too_long.status_code in (400, 422)


def test_a_reply_to_someone_asked_not_to_be_messaged_is_blocked(api, monkeypatch):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(entity=a_user(username="asked_me_to_stop"))))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    monkeypatch.setattr(inbox.outreach_policy, "is_suppressed", lambda subject: True)
    blocked = api.post(f"/api/inbox/chats/{ref}/messages", json={"text": "hello?"})
    assert blocked.status_code == 409
    assert "asked not to be messaged" in blocked.json()["detail"]
    assert api.inbox_account["clients"][account_id].sent == []


def test_an_unreadable_opt_out_ledger_refuses_the_reply_as_our_own_sentence(api):
    """No ledger means no send, and the refusal stays a clean 409 rather than a crash."""
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    inbox.outreach_policy.path.unlink()

    blocked = api.post(f"/api/inbox/chats/{ref}/messages", json={"text": "hi"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == inbox.UNREADABLE_LEDGER
    assert api.inbox_account["clients"][account_id].sent == []


def test_a_telegram_refusal_returns_our_sentence_and_no_exception_text(api, monkeypatch):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event()))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = api.inbox_account["clients"][account_id]

    async def explode(peer, text):
        raise RuntimeError("FLOOD_WAIT_42 with session ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")

    monkeypatch.setattr(fake, "send_message", explode)
    refused = api.post(f"/api/inbox/chats/{ref}/messages", json={"text": "hi"})
    assert refused.status_code == 400
    assert refused.json()["detail"] == inbox.SEND_FAILED
    assert "FLOOD" not in refused.text and "ABCDEFGH" not in refused.text


def test_refresh_snapshots_dialogs_and_reports_what_was_passed_over(api):
    account_id = opted_in_api(api)
    result = api.post("/api/inbox/refresh").json()
    assert result["chats"] == 3 and result["accounts"] == [account_id]
    assert result["skipped_unknown"] == 1
    assert api.get("/api/inbox/chats").json()["chats"]

    clear = api.delete("/api/inbox/cache").json()
    assert clear["cleared"] is True and api.get("/api/inbox/chats").json()["chats"] == []
    assert inbox.opted_in_accounts("op-1") == [account_id]


def test_the_stream_opens_ready_then_the_wall_then_live_events(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242)))
    inbox.HUB._queues.clear()

    async def pull():
        queue = inbox.HUB.subscribe("op-1")
        source = inbox.event_source("op-1", queue)
        frames = [await anext(source), await anext(source)]
        inbox.HUB.publish("op-1", {"type": "ping"})
        frames.append(await anext(source))
        await source.aclose()
        return frames

    ready, bootstrap, live = [json.loads(f[len("data: "):].strip())
                              for f in asyncio.run(pull())]
    assert ready["type"] == "ready" and ready["state"]["accounts"] == [account_id]
    assert bootstrap["type"] == "chat" and bootstrap["chat"]["unread"] == 1
    assert "peer" not in bootstrap["chat"]
    assert live["type"] == "ping"
    assert inbox.HUB.subscribers("op-1") == 0


def seed_chat(api, event):
    """Put one message on the wall and make it that chat's, so the route can find it by id."""
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, event))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = api.inbox_account["clients"][account_id]
    fake.attachments[(event.chat_id, event.id)] = event
    fake.threads[event.chat_id] = [event]
    return ref, fake


def test_media_describes_the_file_and_leaves_the_telegram_handles_out():
    photo = inbox.media_of(an_attachment("photo", mime="image/png"))
    assert photo["kind"] == "photo" and photo["label"] == "Photo"
    assert photo["mime"] == "image/png" and photo["inline"] is True
    assert photo["size"] == len(FAKE_PNG) and photo["human"] == f"{len(FAKE_PNG)} B"

    paper = inbox.media_of(an_attachment("document", payload=FAKE_PDF, name="notes.pdf",
                                         mime="application/pdf"))
    assert paper["kind"] == "document" and paper["name"] == "notes.pdf"
    assert paper["inline"] is False
    assert "AgAD" not in str(paper) and "1234567890123" not in str(paper)


def test_a_photo_with_no_declared_type_is_still_worth_rendering():
    assert inbox.media_of(an_attachment("photo"))["mime"] == "image/jpeg"


@pytest.mark.parametrize("kind,label", [("sticker", "Sticker"), ("voice", "Voice message"),
                                        ("video_note", "Video note"), ("gif", "GIF"),
                                        ("video", "Video"), ("audio", "Audio")])
def test_every_telegram_media_kind_gets_a_name(kind, label):
    shape = inbox.media_of(an_attachment(kind, payload=b"x" * 10, mime="video/mp4"))
    assert shape["kind"] == kind and shape["label"] == label


def test_a_file_is_the_last_choice_because_telegram_marks_gifs_as_documents_too():
    event = an_attachment("gif", payload=b"x" * 10, mime="video/mp4")
    assert event.document is not None          # the fake mirrors what Telethon actually sets
    assert inbox.media_of(event)["kind"] == "gif"


def test_a_file_gives_the_chat_a_line_instead_of_a_blank_preview():
    shot = inbox.message_shape(an_attachment("photo"))
    assert inbox.preview_of(shot) == f"Photo · {len(FAKE_PNG)} B"

    paper = inbox.message_shape(an_attachment("document", payload=FAKE_PDF,
                                              name="quarterly-report.pdf", mime="application/pdf"))
    assert inbox.preview_of(paper) == f"quarterly-report.pdf · {len(FAKE_PDF)} B"

    said = inbox.message_shape(an_attachment("photo", text="look at this"))
    assert inbox.preview_of(said) == "look at this"


def test_a_photo_on_the_wall_is_a_line_not_a_blank(accounts):
    account_id = watched_operator(accounts)
    asyncio.run(inbox.note(account_id, an_attachment("photo")))
    assert inbox.WALLS["op-1"].rows()[0]["preview"] == f"Photo · {len(FAKE_PNG)} B"


def test_a_thread_page_carries_the_descriptor_and_no_telegram_handle(api):
    paper = an_attachment("document", payload=FAKE_PDF, name="notes.pdf",
                          mime="application/pdf", text="see the chart")
    ref, _ = seed_chat(api, paper)
    page = api.get(f"/api/inbox/chats/{ref}/messages", params={"limit": 10}).json()
    shot = page["messages"][0]
    assert shot["text"] == "see the chart"
    assert shot["media"]["name"] == "notes.pdf" and shot["media"]["inline"] is False
    assert "AgAD" not in str(page) and "file_id" not in str(page)
    assert "payload" not in str(page)


def test_a_photo_streams_inline_and_a_file_is_offered_as_a_download(api):
    ref, fake = seed_chat(api, an_attachment("photo", mime="image/png"))
    shot = api.get(f"/api/inbox/media/{ref}/701")
    assert shot.status_code == 200
    assert shot.content == FAKE_PNG
    assert shot.headers["content-type"] == "image/png"
    assert shot.headers["content-disposition"] == 'inline; filename="photo"'
    assert shot.headers["cache-control"] == "no-store"
    assert shot.headers["x-content-type-options"] == "nosniff"
    assert fake.downloads == [{"file_size": len(FAKE_PNG), "chunk_size": inbox.MEDIA_CHUNK_BYTES}]


def test_a_document_is_served_as_a_named_download(api):
    ref, _ = seed_chat(api, an_attachment("document", payload=FAKE_PDF,
                                         name="quarterly report.pdf", mime="application/pdf"))
    answer = api.get(f"/api/inbox/media/{ref}/701")
    assert answer.status_code == 200
    assert answer.content == FAKE_PDF
    assert answer.headers["content-type"] == "application/octet-stream"
    assert answer.headers["content-disposition"] == 'attachment; filename="quarterly report.pdf"'


def test_a_file_name_arriving_with_a_path_cannot_bear_a_header(api):
    ref, _ = seed_chat(api, an_attachment("document", name='../../evil";x.pdf',
                                         mime="application/pdf"))
    answer = api.get(f"/api/inbox/media/{ref}/701")
    assert answer.headers["content-disposition"] == 'attachment; filename="evilx.pdf"'


def test_a_message_with_no_file_attached_says_so(api):
    ref, fake = seed_chat(api, an_attachment("photo", mime="image/png"))
    fake.attachments[(4242, 900)] = SimpleNamespace(id=900, raw_text="plain words",
                                                    date=time.time(), out=False, sender=None)
    refusal = api.get(f"/api/inbox/media/{ref}/900")
    assert refusal.status_code == 404 and refusal.json()["detail"] == inbox.NO_MEDIA


def test_an_id_only_a_different_chat_has_is_not_a_file_here(api):
    """The id is resolved inside this chat, so a message id from another conversation is nothing."""
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242)))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    fake = api.inbox_account["clients"][account_id]
    fake.attachments[(999999, 701)] = an_attachment("photo", peer=999999, mime="image/png")

    denial = api.get(f"/api/inbox/media/{ref}/701")
    assert denial.status_code == 404 and denial.json()["detail"] == inbox.NO_MEDIA
    assert fake.downloads == []


def test_another_operators_ref_is_not_a_chat_you_can_download(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242)))
    foreign = inbox.peer_ref("op-2", account_id, 4242)
    assert api.get(f"/api/inbox/media/{foreign}/701").status_code == 404


def test_a_file_past_the_cap_is_refused_before_the_bytes_move(api):
    event = an_attachment("document", payload=b"x" * 16, name="huge.bin",
                          mime="application/octet-stream")
    event.document.size = inbox.MAX_MEDIA_BYTES + 1
    ref, fake = seed_chat(api, event)
    refusal = api.get(f"/api/inbox/media/{ref}/701")
    assert refusal.status_code == 413 and refusal.json()["detail"] == inbox.MEDIA_TOO_BIG
    assert fake.downloads == []


def test_bytes_past_the_cap_are_dropped_when_telegram_declared_no_size(api, monkeypatch):
    """Telegram sometimes says nothing about the size, so the cap is held while streaming too."""
    monkeypatch.setattr(inbox, "MAX_MEDIA_BYTES", 20)
    monkeypatch.setattr(inbox, "MEDIA_CHUNK_BYTES", 8)
    event = an_attachment("document", payload=b"y" * 40, name="stream.bin",
                          mime="application/octet-stream")
    event.document.size = 0
    ref, _ = seed_chat(api, event)
    answer = api.get(f"/api/inbox/media/{ref}/701")
    assert answer.status_code == 200 and len(answer.content) <= 20
    assert answer.content == b"y" * 16


def test_a_connection_that_cannot_hand_back_bytes_keeps_our_sentence(api):
    account_id = opted_in_api(api)
    asyncio.run(inbox.note(account_id, an_event(peer=4242)))
    ref = inbox.WALLS["op-1"].rows()[0]["ref"]
    plain = UndownloadableClient(account_id)
    inbox._CLIENTS[account_id] = plain

    refusal = api.get(f"/api/inbox/media/{ref}/701")
    assert refusal.status_code == 400 and refusal.json()["detail"] == inbox.MEDIA_FAILED
    assert plain.reads == 0


def test_the_config_publishes_the_media_bounds(api):
    body = api.get("/api/inbox/config").json()
    assert body["media_limit"] == inbox.MAX_MEDIA_BYTES
    assert "image/png" in body["media_inline"] and "application/pdf" not in body["media_inline"]


def test_the_dashboard_mounts_the_inbox_routes():
    """The MCP app is mounted at "/", so the product routers must be registered too."""
    import dashboard

    paths = dashboard.app.openapi()["paths"]
    for path in ["/api/inbox/config", "/api/inbox/chats", "/api/inbox/chats/{ref}/messages",
                 "/api/inbox/chats/{ref}/read", "/api/inbox/media/{ref}/{message_id}",
                 "/api/inbox/refresh", "/api/inbox/cache",
                 "/api/inbox/stream", "/api/inbox/accounts/{account_id}"]:
        assert path in paths, path


def test_the_lifespan_detaches_every_handler_on_shutdown():
    """A handler left registered past shutdown is a leak into a dead loop."""
    source = open("dashboard.py", encoding="utf-8").read()
    assert "inbox_detach_all()" in source
    assert source.index("inbox_detach_all()") < source.index("scheduler_task.cancel()")
