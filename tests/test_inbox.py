"""The inbox classifies, remembers briefly, and forgets on purpose.

Two invariants carry the safety weight: a chat is filed by the type Telegram actually
gave it, and nothing a third party said is ever written to disk. The wall lives in this
process, so a container restart costs a re-read rather than leaving a corpus behind.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import inbox
from open_teleset import operator_store


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
        self.handlers.append((handler, args, kwargs))

    def remove_event_handler(self, handler, *args, **kwargs):
        self.removed.append(handler)
        self.handlers = [entry for entry in self.handlers if entry[0] is not handler]


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
