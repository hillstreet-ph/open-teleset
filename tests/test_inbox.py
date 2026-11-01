"""The inbox classifies, remembers briefly, and forgets on purpose.

Two invariants carry the safety weight: a chat is filed by the type Telegram actually
gave it, and nothing a third party said is ever written to disk. The wall lives in this
process, so a container restart costs a re-read rather than leaving a corpus behind.
"""

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
