"""Auto Reply: what it may answer, what it must never do, and who presses Send.

The invariant under test is the one that keeps this inside Telegram's terms: a reply
only follows a private message that arrived after the switch, never to a group, a
channel, a bot, an opted-out person, or a conversation the operator has taken over.
In AI mode nothing leaves the dashboard without a human pressing Send.
"""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from telethon import errors

import auto_reply
from open_teleset import operator_store
from outreach_policy import OutreachPolicy, hash_identifier

REQUEST = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="operator-1")))
ANON = SimpleNamespace(state=SimpleNamespace(user=None))
ACCOUNT = "acct-1"
POLICY = {"version": 1, "approvals": {}, "consents": [], "suppressions": []}


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """An operator store, an empty opt-out ledger and one account in the workspace."""
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY))
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("OUTREACH_POLICY_FILE", str(policy))
    operator_store.set_features({"auto_reply_enabled": True})
    monkeypatch.setattr(auto_reply, "account_cooldown_remaining", lambda account: 0)
    monkeypatch.setattr(auto_reply, "outreach_policy", OutreachPolicy(policy))
    monkeypatch.setattr(auto_reply.log_manager, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(auto_reply.knowledge, "settings_for",
                        lambda operator: {"auto_collect_replies": False, "auto_collect_copilot": False,
                                          "include_templates": False})
    from account_manager import account_manager
    monkeypatch.setattr(account_manager, "accounts", {ACCOUNT: SimpleNamespace(id=ACCOUNT)})
    monkeypatch.setattr(auto_reply.worker, "task", None)
    monkeypatch.setattr(auto_reply.worker, "error", None)
    monkeypatch.setattr(auto_reply.worker, "last_tick", None)
    yield


class Clock:
    """Pass times far enough apart to clear the chat cooldown and the send spacing."""

    def __init__(self):
        self.t = time.time()

    def step(self):
        self.t += auto_reply.CHAT_COOLDOWN_SECONDS + auto_reply.SEND_SPACING_SECONDS + 10
        return self.t

    def date(self, at=None):
        return datetime.fromtimestamp((self.t if at is None else at) - 5)


def an_entity(pid=111, username="alice_x", first="Alice", last="Lee", bot=False, deleted=False):
    return SimpleNamespace(id=pid, username=username, first_name=first, last_name=last,
                           bot=bot, deleted=deleted)


def a_dialog(clock, message_id=11, text="are you there", *, entity=None, out=False, private=True,
             name="", age_seconds=5):
    entity = entity or an_entity()
    return SimpleNamespace(entity=entity, name=name, is_private=private,
                           message=SimpleNamespace(id=message_id, text=text, raw_text=text, out=out,
                                                   date=datetime.fromtimestamp(clock.t - age_seconds)))


class FakeClient:
    def __init__(self, dialogs=(), send_id=900, raise_on_send=None):
        self.dialogs = list(dialogs)
        self.sent = []
        self.send_id = send_id
        self.raise_on_send = raise_on_send

    async def get_dialogs(self, limit=None):
        self.limit = limit
        return list(self.dialogs)

    async def get_entity(self, peer):
        return an_entity()

    async def send_message(self, target, text):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.send_id += 1
        self.sent.append((getattr(target, "id", None), text))
        return SimpleNamespace(id=self.send_id)


def a_client(*dialogs):
    client = FakeClient(dialogs)

    async def getter(account_id):
        return client

    return getter, client


async def configure(**updates):
    body = auto_reply.ConfigBody(**{"enabled": True, "mode": auto_reply.RULES_MODE, **updates})
    return await auto_reply.put_config(body, REQUEST)


async def add_rule(**overrides):
    body = auto_reply.RuleBody(**{"name": "Price", "keywords": ["price"], "reply": "It is 10 dollars.",
                                  **overrides})
    return await auto_reply.create_rule(body, REQUEST)


async def watch(account_id=ACCOUNT):
    return await auto_reply.set_account(account_id, auto_reply.AccountBody(enabled=True), REQUEST)


def poll(get_client, now):
    return asyncio.run(auto_reply.poll_operator("operator-1", get_client=get_client, now=now))


def run(coro):
    return asyncio.run(coro)


def seen_conversation():
    record = auto_reply._record("operator-1")
    ref = sorted(record["conversations"], key=lambda key: record["conversations"][key]["cursor"])[-1]
    return ref, record["conversations"][ref]


def opt_out(username="alice_x", *, unreadable=False):
    """Write the operator's opt-out ledger the way the consent forms leave it."""
    if unreadable:
        auto_reply.outreach_policy.path.write_text("{ not json")
        return
    data = json.loads(auto_reply.outreach_policy.path.read_text())
    data["suppressions"] = [{"subject_hash": hash_identifier(username), "active": True}]
    auto_reply.outreach_policy.path.write_text(json.dumps(data))


# ---------------------------------------------------------------- the feature switch


def test_the_switch_ships_dark(tmp_path, monkeypatch):
    """A brand-new install has nothing stored, so Auto Reply is off until switched on."""
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path / "fresh"))

    assert operator_store.feature_enabled("auto_reply") is False


@pytest.mark.parametrize("index", range(8))
def test_every_endpoint_is_closed_until_the_feature_is_on(index, monkeypatch):
    operator_store.set_features({"auto_reply_enabled": False})
    calls = [
        lambda: auto_reply.get_config(REQUEST),
        lambda: auto_reply.put_config(auto_reply.ConfigBody(enabled=True), REQUEST),
        lambda: auto_reply.create_rule(auto_reply.RuleBody(reply="hi", keywords=["x"]), REQUEST),
        lambda: auto_reply.set_account(ACCOUNT, auto_reply.AccountBody(enabled=True), REQUEST),
        lambda: auto_reply.test_reply(auto_reply.TestBody(text="hi"), REQUEST),
        lambda: auto_reply.conversations(REQUEST),
        lambda: auto_reply.outbox(REQUEST),
        lambda: auto_reply.run_now(REQUEST),
    ]

    with pytest.raises(HTTPException) as error:
        run(calls[index]())

    assert error.value.status_code == 403
    assert error.value.detail == auto_reply.OFF_MESSAGE


def test_an_anonymous_request_gets_nothing():
    with pytest.raises(HTTPException) as error:
        run(auto_reply.get_config(ANON))

    assert error.value.status_code == 403


# ---------------------------------------------------------------- configuration


def test_defaults_are_off_and_conservative():
    config = auto_reply.public_config("operator-1")

    assert config["enabled"] is False
    assert config["mode"] == auto_reply.RULES_MODE
    assert config["poll_seconds"] == auto_reply.DEFAULT_POLL_SECONDS
    assert config["daily_cap"] == auto_reply.DEFAULT_DAILY_CAP
    assert config["accounts"] == []
    assert config["rules"] == []
    assert config["feature_enabled"] is True
    assert config["limits"]["min_poll_seconds"] == auto_reply.MIN_POLL_SECONDS


def test_numbers_are_clamped_to_the_documented_range():
    run(configure(poll_seconds=1, daily_cap=99_999))
    config = auto_reply.public_config("operator-1")

    assert config["poll_seconds"] == auto_reply.MIN_POLL_SECONDS
    assert config["daily_cap"] == auto_reply.MAX_DAILY_CAP


def test_an_unknown_mode_is_refused():
    with pytest.raises(HTTPException) as error:
        run(configure(mode="silent"))

    assert error.value.status_code == 400


def test_ai_mode_says_a_provider_is_needed():
    run(configure(mode=auto_reply.AI_MODE))

    assert auto_reply.public_config("operator-1")["provider_needed"] is True


# ---------------------------------------------------------------- rules


def test_a_keyword_rule_needs_a_keyword():
    with pytest.raises(HTTPException) as error:
        run(add_rule(keywords=[]))

    assert error.value.status_code == 400


def test_a_match_anything_rule_needs_no_keyword():
    rule = run(add_rule(match="any", keywords=[]))["rules"][0]

    assert rule["match"] == "any"


@pytest.mark.parametrize("bad", ["", "   "])
def test_the_reply_text_is_required(bad):
    with pytest.raises(HTTPException) as error:
        run(add_rule(reply=bad))

    assert error.value.status_code == 400


def test_an_oversized_reply_is_refused_at_both_layers():
    """The form rejects it, and so does the cleaner the suggestion accept path uses."""
    with pytest.raises(ValidationError):
        auto_reply.RuleBody(reply="x" * (auto_reply.REPLY_MAX + 1), keywords=["price"])

    with pytest.raises(HTTPException) as error:
        auto_reply._clean_rule({"keywords": ["price"], "reply": "x" * (auto_reply.REPLY_MAX + 1)})

    assert error.value.status_code == 400


def test_only_the_known_placeholders_are_accepted():
    with pytest.raises(HTTPException) as error:
        run(add_rule(reply="Hi {name}, your {order} is ready."))

    assert error.value.status_code == 400
    assert "{order}" in error.value.detail


def test_the_rule_name_falls_back_to_the_first_keyword():
    rule = run(add_rule(name="", keywords=["refund"]))["rules"][0]

    assert rule["name"] == "refund"


def test_rules_are_bounded_rather_than_growing_forever():
    async def fill():
        for index in range(auto_reply.MAX_RULES):
            await add_rule(name=f"R{index}")
        return await add_rule(name="One too many")

    with pytest.raises(HTTPException) as error:
        run(fill())

    assert error.value.status_code == 409
    assert error.value.detail == auto_reply.TOO_MANY_RULES


def test_keywords_are_lowered_trimmed_and_bounded():
    rule = run(add_rule(keywords=["  PRICE  ", " Refund "]))["rules"][0]

    assert rule["keywords"] == ["price", "refund"]

    cleaned = auto_reply._clean_rule({"keywords": ["  PRICE  "] + [f"w{i}" for i in range(20)],
                                      "reply": "Ten dollars."})
    assert cleaned["keywords"] == ["price"] + [f"w{i}" for i in range(auto_reply.MAX_KEYWORDS - 1)]

    long_word = auto_reply._clean_rule({"keywords": ["x" * (auto_reply.KEYWORD_MAX + 20)], "reply": "Hi"})
    assert long_word["keywords"] == ["x" * auto_reply.KEYWORD_MAX]


def test_a_rule_can_be_edited_and_deleted():
    async def scenario():
        created = await add_rule()
        rule_id = created["rules"][0]["id"]
        edited = await auto_reply.edit_rule(rule_id, auto_reply.RuleBody(name="Cost", keywords=["cost"],
                                                                        reply="Ten dollars."), REQUEST)
        return edited, await auto_reply.delete_rule(rule_id, REQUEST)

    edited, deleted = run(scenario())

    assert [rule["name"] for rule in edited["rules"]] == ["Cost"]
    assert deleted["rules"] == []
    with pytest.raises(HTTPException) as error:
        run(auto_reply.delete_rule("nope", REQUEST))
    assert error.value.status_code == 404


def test_a_disabled_rule_never_answers():
    rules = [{"id": "off", "enabled": False, "match": "keyword", "keywords": ["price"], "reply": "x"},
             {"id": "refund", "enabled": True, "match": "keyword", "keywords": ["refund"], "reply": "y"},
             {"id": "any", "enabled": True, "match": "any", "keywords": [], "reply": "z"}]

    assert auto_reply.match_rule(rules, "what is the price")["id"] == "any"
    assert auto_reply.match_rule(rules, "need a REFUND today")["id"] == "refund"
    assert auto_reply.match_rule(rules[:1], "anything at all") is None
    assert auto_reply.match_rule([], "anything") is None


def test_placeholders_render_for_the_person_who_wrote():
    assert auto_reply.render("Hi {name}, {first} note", name="Ada Lovelace") == "Hi Ada Lovelace, Ada note"
    assert auto_reply.render("Hi {first}", name="") == "Hi there"


# ---------------------------------------------------------------- what it answers


def test_the_first_message_seen_never_gets_an_answer():
    """Those messages arrived before the switch; replying would be us messaging first."""
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        getter, client = a_client(a_dialog(clock, 10, "hello"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["watching"] == 1
    assert counts["replied"] == 0
    assert client.sent == []
    assert auto_reply._record("operator-1")["sent"] == {}


def test_a_message_that_arrives_after_the_switch_is_answered_by_the_rule():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(keywords=["price"], reply="It is ten dollars, {first}.")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "What is the PRICE?"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["replied"] == 1
    assert client.sent == [(111, "It is ten dollars, Alice.")]
    assert seen_conversation()[1]["status"] == "replied"
    assert auto_reply.public_config("operator-1")["sent_today"] == 1


def test_an_unmatched_message_is_left_for_a_human():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(keywords=["price"])
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "Can you help with invoicing?"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []
    assert any("No rule matched" in item["detail"] for item in auto_reply._record("operator-1")["activity"])


@pytest.mark.parametrize("kind, dialog_kwargs", [
    ("group", {"private": False}),
    ("channel", {"private": False}),
    ("bot", {"entity": an_entity(bot=True)}),
    ("deleted", {"entity": an_entity(deleted=True)}),
])
def test_a_group_a_channel_a_bot_or_a_dead_account_is_never_read(kind, dialog_kwargs):
    assert auto_reply._private(a_dialog(Clock(), **dialog_kwargs)) is False


def test_a_dialog_with_no_entity_or_no_message_is_ignored():
    empty = SimpleNamespace(entity=None, is_private=True, message=a_dialog(Clock()).message, name="")
    no_message = SimpleNamespace(entity=an_entity(), is_private=True, message=None, name="")
    counts = {"checked": 0, "replied": 0, "drafted": 0, "paused": 0, "skipped": 0, "watching": 0, "reason": ""}

    record = auto_reply._record("operator-1")
    outcomes = [run(auto_reply._look_at_dialog("operator-1", record, ACCOUNT, dialog, FakeClient(),
                                                 time.time(), counts))
                for dialog in (empty, no_message)]

    assert outcomes == ["none", "none"]
    assert record["conversations"] == {}


def test_a_group_conversation_never_gets_a_reply_from_a_pass():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        group = a_dialog(clock, 10, "hello", private=False)
        await auto_reply.poll_operator("operator-1", get_client=a_client(group)[0], now=clock.t)
        clock.step()
        group.message.id = 11
        getter, client = a_client(group)
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert client.sent == []
    assert counts["watching"] == 0
    assert auto_reply._record("operator-1")["conversations"] == {}


def test_our_own_last_message_is_never_answered():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "sent by me", out=True))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["replied"] == 0
    assert counts["paused"] == 1
    assert client.sent == []
    assert any(auto_reply.TAKEOVER_NOTE in item["detail"] for item in auto_reply._record("operator-1")["activity"])


def test_a_taken_over_conversation_stays_paused():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "mine", out=True))[0], now=clock.step())
        clock.step()
        getter, client = a_client(a_dialog(clock, 12, "still there?"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []


def test_resuming_a_conversation_lets_it_answer_again():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "mine", out=True))[0], now=clock.step())
        ref = seen_conversation()[0]
        await auto_reply.resume_conversation(ref, REQUEST)
        clock.step()
        getter, client = a_client(a_dialog(clock, 12, "are you there"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["replied"] == 1
    assert len(client.sent) == 1


def test_muting_a_conversation_stops_replies_there():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        ref = seen_conversation()[0]
        await auto_reply.mute_conversation(ref, REQUEST)
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "are you there"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []
    assert auto_reply.public_config("operator-1")["conversations"][0]["muted"] is True


def test_an_unknown_conversation_cannot_be_resumed_or_muted():
    for call in (lambda: auto_reply.resume_conversation("deadbeef", REQUEST),
                 lambda: auto_reply.mute_conversation("deadbeef", REQUEST)):
        with pytest.raises(HTTPException) as error:
            run(call())
        assert error.value.status_code == 404


def test_a_message_that_arrived_long_ago_is_not_answered():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        clock.step()
        old = a_dialog(clock, 11, "price?", age_seconds=auto_reply.STALE_MESSAGE_SECONDS + 120)
        getter, client = a_client(old)
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []
    assert any("too old" in item["detail"] for item in auto_reply._record("operator-1")["activity"])


def test_a_second_message_waits_for_the_chat_cooldown_then_gets_its_answer():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        ref = seen_conversation()[0]
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "first"))[0], now=clock.step())
        soon = clock.t + 30
        deferred = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 12, "second"))[0], now=soon)
        later = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 12, "second"))[0], now=clock.step())
        return ref, deferred, later

    ref, deferred, later = run(scenario())

    assert deferred["replied"] == 0 and deferred["skipped"] == 1
    assert later["replied"] == 1


def test_two_conversations_do_not_answer_each_other_back_to_back():
    clock = Clock()
    alice = a_dialog(clock, 10, "hello", entity=an_entity(pid=111, username="alice_x"))
    bob = a_dialog(clock, 10, "hello", entity=an_entity(pid=222, username="bob_z"))

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(alice, bob)[0], now=clock.t)
        clock.step()
        alice.message.id = 11
        bob.message.id = 11
        client = FakeClient([alice, bob])

        async def getter(account_id):
            return client

        counts = await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)
        return counts, client

    counts, client = run(scenario())

    assert counts["replied"] == 1
    assert len(client.sent) == 1
    assert counts["skipped"] == 1


def test_a_person_on_the_opt_out_list_is_not_answered():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        opt_out("alice_x")
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "are you there"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []
    activity = auto_reply._record("operator-1")["activity"]
    assert any("opt-out" in item["detail"] for item in activity)
    assert seen_conversation()[1]["status"] == "opted_out"


def test_an_unreadable_opt_out_list_blocks_the_reply():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        opt_out(unreadable=True)
        clock.step()
        getter, client = a_client(a_dialog(clock, 11, "are you there"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert client.sent == []
    assert any("could not be read" in item["detail"] for item in auto_reply._record("operator-1")["activity"])


def test_a_repeat_of_the_same_message_id_is_not_answered_twice():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        dialog = a_dialog(clock, 11, "are you there")
        first = await auto_reply.poll_operator("operator-1", get_client=a_client(dialog)[0], now=clock.step())
        second = await auto_reply.poll_operator("operator-1", get_client=a_client(dialog)[0], now=clock.step())
        return first, second

    first, second = run(scenario())

    assert first["replied"] == 1
    assert second["replied"] == 0


# ---------------------------------------------------------------- AI mode


def test_ai_mode_queues_a_draft_and_sends_nothing_itself():
    clock = Clock()

    async def scenario():
        await configure(mode=auto_reply.AI_MODE)
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        clock.step()

        async def fake_compose(operator, inbound, name):
            return "Yes, we ship to Malaysia."

        getter, client = a_client(a_dialog(clock, 11, "do you ship to Malaysia?"))
        original = auto_reply._compose
        auto_reply._compose = fake_compose
        try:
            counts = await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)
        finally:
            auto_reply._compose = original
        return counts, client

    counts, client = run(scenario())

    assert counts["drafted"] == 1
    assert counts["replied"] == 0
    assert client.sent == []
    pending = auto_reply.public_config("operator-1")["outbox"]
    assert len(pending) == 1
    assert pending[0]["reply"] == "Yes, we ship to Malaysia."
    assert pending[0]["status"] == "pending"


def test_ai_mode_says_when_there_is_no_draft_to_approve():
    clock = Clock()

    async def scenario():
        await configure(mode=auto_reply.AI_MODE)
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)

        async def no_provider(operator, inbound, name):
            return ""

        clock.step()
        original = auto_reply._compose
        auto_reply._compose = no_provider
        try:
            return await auto_reply.poll_operator("operator-1", get_client=a_client(
                a_dialog(clock, 11, "hi"))[0], now=clock.t)
        finally:
            auto_reply._compose = original

    counts = run(scenario())

    assert counts["skipped"] == 1
    assert any("No AI draft" in item["detail"] for item in auto_reply._record("operator-1")["activity"])


def test_the_outbox_is_bounded_so_drafts_cannot_pile_up():
    record = auto_reply._record("operator-1")
    record["enabled"] = True
    record["mode"] = auto_reply.AI_MODE
    ref = auto_reply._peer_ref("operator-1", ACCOUNT, 111)
    record["conversations"] = {ref: {"account": ACCOUNT, "peer": "111", "name": "Alice Lee",
                                     "suppress_key": "alice_x", "cursor": 1, "seen": time.time(),
                                     "status": "waiting", "taken_over": False, "muted": False,
                                     "our_ids": [], "last_reply_at": 0}}
    for index in range(auto_reply.MAX_OUTBOX + 5):
        auto_reply._queue_draft(record, ACCOUNT, ref, record["conversations"][ref], "asked", f"draft {index}")

    assert len(record["outbox"]) == auto_reply.MAX_OUTBOX
    assert record["outbox"][0]["reply"] == f"draft {auto_reply.MAX_OUTBOX + 4}"


def test_the_model_is_asked_in_the_operators_own_words_and_grounds_it_in_the_notes(monkeypatch):
    import dashboard_features

    seen = {}

    async def fake_chat(base_url, key, model, messages, max_tokens=None):
        seen["messages"] = messages
        seen["base_url"] = base_url
        return "  Refunds   take five days.  "

    monkeypatch.setattr(dashboard_features, "_ai_config",
                        lambda operator: {"provider": "openai", "base_url": "https://example.test/v1",
                                          "model": "gpt-mini"})
    monkeypatch.setattr(dashboard_features, "_ai_keys", lambda operator: {"openai": "sk-test-key"})
    monkeypatch.setattr(dashboard_features, "_ai_chat", fake_chat)
    monkeypatch.setattr(auto_reply.knowledge, "context_for",
                        lambda operator, query, characters=1200: "- Refunds: five working days")

    answer = run(auto_reply._compose("operator-1", "what is the refund policy", "Alice"))

    assert answer == "Refunds take five days."
    system, user = seen["messages"]
    assert "Never invent" in system["content"]
    assert "what is the refund policy" in user["content"]
    assert "- Refunds: five working days" in user["content"]
    assert "sk-test-key" not in json.dumps(seen["messages"])
    assert seen["base_url"] == "https://example.test/v1"


def test_a_failing_provider_call_yields_no_draft_and_no_key_leak(monkeypatch):
    import dashboard_features

    async def boom(base_url, key, model, messages, max_tokens=None):
        raise HTTPException(502, "provider exploded")

    monkeypatch.setattr(dashboard_features, "_ai_config",
                        lambda operator: {"provider": "openai", "base_url": "https://example.test/v1",
                                          "model": "gpt-mini"})
    monkeypatch.setattr(dashboard_features, "_ai_keys", lambda operator: {"openai": "sk-test-key"})
    monkeypatch.setattr(dashboard_features, "_ai_chat", boom)

    assert run(auto_reply._compose("operator-1", "hi", "Alice")) == ""


def test_no_provider_settings_means_no_call_at_all(monkeypatch):
    import dashboard_features

    calls = []

    async def never(base_url, key, model, messages, max_tokens=None):
        calls.append(messages)

    monkeypatch.setattr(dashboard_features, "_ai_config", lambda operator: {"provider": ""})
    monkeypatch.setattr(dashboard_features, "_ai_keys", lambda operator: {})
    monkeypatch.setattr(dashboard_features, "_ai_chat", never)

    assert run(auto_reply._compose("operator-1", "hi", "Alice")) == ""
    assert calls == []


# ---------------------------------------------------------------- the sending gates


def test_a_telegram_wait_is_recorded_and_its_detail_never_leaks(monkeypatch):
    clock = Clock()
    recorded = []

    def capture(account, exc):
        recorded.append((account, exc.seconds))

    monkeypatch.setattr(auto_reply, "record_provider_restriction", capture)

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        client = FakeClient([a_dialog(clock, 11, "are you there")],
                            raise_on_send=errors.FloodWaitError(None, capture=37))

        async def getter(account_id):
            return client

        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.step())

    counts = run(scenario())

    assert counts["skipped"] == 1
    assert recorded == [(ACCOUNT, 37)]
    raw = json.dumps(auto_reply._record("operator-1")["activity"])
    assert "37" not in raw
    assert any("did not accept" in item["detail"] for item in auto_reply._record("operator-1")["activity"])


def test_a_failed_send_is_never_counted_as_sent():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        client = FakeClient([a_dialog(clock, 11, "are you there")], raise_on_send=RuntimeError("boom"))

        async def getter(account_id):
            return client

        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.step())

    counts = run(scenario())

    assert counts["replied"] == 0
    assert auto_reply.public_config("operator-1")["sent_today"] == 0


def test_the_daily_cap_stops_the_next_pass():
    clock = Clock()

    async def scenario():
        await configure(daily_cap=1)
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        first = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "are you there"))[0], now=clock.step())
        second = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 12, "again"))[0], now=clock.step())
        return first, second

    first, second = run(scenario())

    assert first["replied"] == 1
    assert second["reason"] == "daily_cap"


def test_a_zero_daily_cap_means_nothing_is_sent_at_all():
    clock = Clock()

    async def scenario():
        await configure(daily_cap=0)
        await add_rule(match="any")
        await watch()
        getter, client = a_client(a_dialog(clock, 11, "are you there"))
        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client

    counts, client = run(scenario())

    assert counts["reason"] == "daily_cap"
    assert client.sent == []


def test_an_account_telegram_told_us_to_leave_alone_is_not_touched():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        original = auto_reply.account_cooldown_remaining
        auto_reply.account_cooldown_remaining = lambda account: 120
        try:
            getter, client = a_client(a_dialog(clock, 11, "are you there"))
            return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t), client
        finally:
            auto_reply.account_cooldown_remaining = original

    counts, client = run(scenario())

    assert counts["skipped"] == 1
    assert counts["checked"] == 0
    assert client.sent == []


def test_nothing_happens_while_the_switch_or_the_account_is_off():
    clock = Clock()

    async def scenario():
        await configure(enabled=False)
        await add_rule(match="any")
        await watch()
        off = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "hi"))[0], now=clock.t)
        await configure(enabled=True)
        record = auto_reply._record("operator-1")
        record["accounts"] = {}
        auto_reply._save("operator-1", record)
        no_accounts = await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "hi"))[0], now=clock.t)
        return off, no_accounts

    off, no_accounts = run(scenario())

    assert off["reason"] == "off"
    assert no_accounts["reason"] == "no_accounts"


def test_an_unreachable_account_is_skipped_not_fatal():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()

        async def getter(account_id):
            raise RuntimeError("connection refused")

        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)

    assert run(scenario())["skipped"] == 1


def test_a_client_that_is_none_is_skipped():
    clock = Clock()

    async def scenario():
        await configure()
        await watch()

        async def getter(account_id):
            return None

        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)

    assert run(scenario())["skipped"] == 1


def test_a_conversation_list_that_fails_ends_the_pass_without_a_send():
    clock = Clock()

    class Broken:
        async def get_dialogs(self, limit=None):
            raise RuntimeError("no dialogs")

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()

        async def getter(account_id):
            return Broken()

        return await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)

    counts = run(scenario())

    assert counts["checked"] == 1
    assert counts["replied"] == 0


def test_a_pass_reads_only_a_bounded_number_of_conversations():
    clock = Clock()

    async def scenario():
        await configure()
        await watch()
        client = FakeClient([a_dialog(clock, 10, "hello", entity=an_entity(pid=pid))
                             for pid in range(60)])

        async def getter(account_id):
            return client

        await auto_reply.poll_operator("operator-1", get_client=getter, now=clock.t)
        return client

    client = run(scenario())

    assert client.limit == auto_reply.MAX_DIALOGS


# ---------------------------------------------------------------- the outbox


def a_draft():
    """Put one AI draft in the outbox the way the listener leaves it."""
    record = auto_reply._record("operator-1")
    record["enabled"] = True
    record["mode"] = auto_reply.AI_MODE
    record["accounts"] = {ACCOUNT: {"enabled": True}}
    ref = auto_reply._peer_ref("operator-1", ACCOUNT, 111)
    record["conversations"] = {ref: {"account": ACCOUNT, "peer": "111", "name": "Alice Lee",
                                     "suppress_key": "alice_x", "cursor": 11, "seen": time.time(),
                                     "status": "waiting", "taken_over": False, "muted": False,
                                     "our_ids": [], "last_reply_at": 0}}
    auto_reply._queue_draft(record, ACCOUNT, ref, record["conversations"][ref],
                            "do you ship to Malaysia?", "Yes, we ship to Malaysia.")
    auto_reply._save("operator-1", record)
    return ref, record["outbox"][0]["id"]


def test_a_human_pressing_send_is_the_only_way_model_words_leave():
    ref, draft_id = a_draft()
    delivered = []

    async def fake_deliver(record, conv, account_id, text):
        delivered.append((conv["peer"], account_id, text))
        return True

    original = auto_reply._deliver
    auto_reply._deliver = fake_deliver
    try:
        result = run(auto_reply.send_draft(draft_id, REQUEST))
    finally:
        auto_reply._deliver = original

    assert delivered == [("111", ACCOUNT, "Yes, we ship to Malaysia.")]
    assert result["outbox"] == []
    assert auto_reply.public_config("operator-1")["sent_today"] == 1


def test_a_draft_can_be_edited_before_it_is_approved():
    ref, draft_id = a_draft()

    async def scenario():
        await auto_reply.edit_draft(draft_id, auto_reply.OutboxEdit(reply="Yes, five days."), REQUEST)
        return auto_reply._record("operator-1")["outbox"][0]

    draft = run(scenario())

    assert draft["reply"] == "Yes, five days."
    assert draft["edited"] is True


def test_a_draft_rejected_by_the_operator_is_dropped_without_sending():
    ref, draft_id = a_draft()
    sent = []

    async def fake_deliver(*args):
        sent.append(args)
        return True

    original = auto_reply._deliver
    auto_reply._deliver = fake_deliver
    try:
        result = run(auto_reply.discard_draft(draft_id, REQUEST))
    finally:
        auto_reply._deliver = original

    assert result["outbox"] == []
    assert sent == []
    assert auto_reply.public_config("operator-1")["sent_today"] == 0


def test_send_rechecks_the_daily_cap():
    ref, draft_id = a_draft()
    run(configure(daily_cap=0))

    with pytest.raises(HTTPException) as error:
        run(auto_reply.send_draft(draft_id, REQUEST))

    assert error.value.status_code == 409
    assert "daily limit" in error.value.detail


def test_send_rechecks_the_account_cooldown():
    ref, draft_id = a_draft()
    original = auto_reply.account_cooldown_remaining
    auto_reply.account_cooldown_remaining = lambda account: 120
    try:
        with pytest.raises(HTTPException) as error:
            run(auto_reply.send_draft(draft_id, REQUEST))
    finally:
        auto_reply.account_cooldown_remaining = original

    assert error.value.status_code == 409
    assert "wait" in error.value.detail


def test_send_rechecks_the_opt_out_list():
    ref, draft_id = a_draft()
    opt_out("alice_x")

    with pytest.raises(HTTPException) as error:
        run(auto_reply.send_draft(draft_id, REQUEST))

    assert error.value.status_code == 409
    assert "opt-out" in error.value.detail


def test_send_stops_when_the_opt_out_list_cannot_be_read():
    ref, draft_id = a_draft()
    opt_out(unreadable=True)

    with pytest.raises(HTTPException) as error:
        run(auto_reply.send_draft(draft_id, REQUEST))

    assert error.value.status_code == 503
    assert "could not be read" in error.value.detail


def test_a_send_telegram_refused_is_not_marked_sent():
    ref, draft_id = a_draft()

    async def fail_deliver(record, conv, account_id, text):
        return False

    original = auto_reply._deliver
    auto_reply._deliver = fail_deliver
    try:
        with pytest.raises(HTTPException) as error:
            run(auto_reply.send_draft(draft_id, REQUEST))
    finally:
        auto_reply._deliver = original

    assert error.value.status_code == 502
    assert "Nothing was marked as sent" in error.value.detail
    assert len(auto_reply.public_config("operator-1")["outbox"]) == 1
    assert auto_reply.public_config("operator-1")["sent_today"] == 0


def test_a_draft_that_is_not_there_cannot_be_touched():
    a_draft()

    for call in (lambda: auto_reply.send_draft("nope", REQUEST),
                 lambda: auto_reply.edit_draft("nope", auto_reply.OutboxEdit(reply="x"), REQUEST),
                 lambda: auto_reply.discard_draft("nope", REQUEST)):
        with pytest.raises(HTTPException) as error:
            run(call())
        assert error.value.status_code == 404
        assert error.value.detail == auto_reply.GONE_OUTBOX


def test_an_approved_draft_is_offered_to_the_knowledge_base(monkeypatch):
    saved = []
    ref, draft_id = a_draft()
    monkeypatch.setattr(auto_reply.knowledge, "settings_for",
                        lambda operator: {"auto_collect_replies": True})
    monkeypatch.setattr(auto_reply.knowledge, "add_document", lambda operator, **kwargs: saved.append(kwargs))

    async def fake_deliver(*args):
        return True

    original = auto_reply._deliver
    auto_reply._deliver = fake_deliver
    try:
        run(auto_reply.send_draft(draft_id, REQUEST))
    finally:
        auto_reply._deliver = original

    assert saved[0]["source"] == "auto_reply"
    assert saved[0]["text"] == "Yes, we ship to Malaysia."


def test_a_sent_rule_reply_is_offered_to_the_knowledge_base(monkeypatch):
    saved = []
    monkeypatch.setattr(auto_reply.knowledge, "settings_for",
                        lambda operator: {"auto_collect_replies": True})
    monkeypatch.setattr(auto_reply.knowledge, "add_document", lambda operator, **kwargs: saved.append(kwargs))
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(keywords=["ship"], reply="We ship worldwide.")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        return await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "do you ship?"))[0], now=clock.step())

    counts = run(scenario())

    assert counts["replied"] == 1
    assert saved[0]["source"] == "auto_reply"
    assert saved[0]["dedupe"] is True


def test_nothing_is_collected_when_the_operator_switched_it_off():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        return await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "hi"))[0], now=clock.step())

    run(scenario())

    assert knowledge_documents() == []


def knowledge_documents():
    import knowledge as kb

    return kb.list_documents("operator-1")["documents"]


# ---------------------------------------------------------------- test and run-now


def test_the_preview_shows_the_answer_and_sends_nothing():
    getter, client = a_client()

    async def scenario():
        await configure()
        await add_rule(keywords=["price"], reply="Ten dollars, {first}.")
        return await auto_reply.test_reply(
            auto_reply.TestBody(text="what is the price", name="Ada Lovelace"), REQUEST)

    result = run(scenario())

    assert result["matched"]["name"] == "Price"
    assert result["reply"] == "Ten dollars, Ada."
    assert result["would_send"] is True
    assert client.sent == []


def test_the_preview_says_when_no_rule_matches():
    async def scenario():
        await configure()
        await add_rule(keywords=["price"])
        return await auto_reply.test_reply(auto_reply.TestBody(text="hello"), REQUEST)

    result = run(scenario())

    assert result["matched"] is None
    assert result["would_send"] is False


def test_the_preview_in_ai_mode_promises_no_send():
    async def scenario():
        await configure(mode=auto_reply.AI_MODE)
        return await auto_reply.test_reply(auto_reply.TestBody(text="hello"), REQUEST)

    result = run(scenario())

    assert result["would_send"] is False
    assert "approval" in result["detail"].lower()


def test_an_empty_preview_text_is_refused():
    with pytest.raises(HTTPException) as error:
        run(auto_reply.test_reply(auto_reply.TestBody(text="   "), REQUEST))

    assert error.value.status_code == 400


def test_run_now_needs_the_switch_then_an_account():
    with pytest.raises(HTTPException) as error:
        run(auto_reply.run_now(REQUEST))
    assert error.value.status_code == 409
    assert "Switch Auto Reply on" in error.value.detail

    async def scenario():
        await configure(enabled=True)
        return await auto_reply.run_now(REQUEST)

    with pytest.raises(HTTPException) as error:
        run(scenario())

    assert error.value.detail == auto_reply.NO_ACCOUNTS


def test_run_now_runs_the_same_pass_the_listener_runs(monkeypatch):
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        first, _ = a_client(a_dialog(clock, 10, "hello there"))
        monkeypatch.setattr(auto_reply, "_client_getter", lambda: first)
        await auto_reply.run_now(REQUEST)
        ref, conv = seen_conversation()
        assert conv["cursor"] == 10
        assert conv["status"] == "watching"
        getter, client = a_client(a_dialog(clock, 11, "are you there"))
        monkeypatch.setattr(auto_reply, "_client_getter", lambda: getter)
        return await auto_reply.run_now(REQUEST), client

    result, client = run(scenario())

    assert result["result"]["replied"] == 1
    assert len(client.sent) == 1


# ---------------------------------------------------------------- accounts


def test_only_an_account_in_this_workspace_can_be_watched():
    with pytest.raises(HTTPException) as error:
        run(auto_reply.set_account("not-here", auto_reply.AccountBody(enabled=True), REQUEST))

    assert error.value.status_code == 404


def test_watching_more_accounts_than_the_bound_is_refused(monkeypatch):
    from account_manager import account_manager
    monkeypatch.setattr(account_manager, "accounts",
                        {f"a{i}": SimpleNamespace(id=f"a{i}") for i in range(20)})

    async def scenario():
        for index in range(auto_reply.MAX_ACCOUNTS):
            await auto_reply.set_account(f"a{index}", auto_reply.AccountBody(enabled=True), REQUEST)
        return await auto_reply.set_account("a19", auto_reply.AccountBody(enabled=True), REQUEST)

    with pytest.raises(HTTPException) as error:
        run(scenario())

    assert error.value.status_code == 409


def test_stopping_to_watch_an_account_drops_it():
    async def scenario():
        await watch()
        await auto_reply.set_account(ACCOUNT, auto_reply.AccountBody(enabled=False), REQUEST)
        return auto_reply.public_config("operator-1")["accounts"]

    assert run(scenario()) == []


def test_a_watched_account_appears_in_the_panel():
    async def scenario():
        await watch()
        return auto_reply.public_config("operator-1")["accounts"]

    assert run(scenario()) == [{"id": ACCOUNT, "enabled": True}]


# ---------------------------------------------------------------- the background listener


def test_only_an_operator_whose_interval_elapsed_is_due():
    async def scenario():
        await configure(poll_seconds=auto_reply.MIN_POLL_SECONDS)
        await watch()

    run(scenario())
    elapsed = auto_reply.MIN_POLL_SECONDS + 1
    record = auto_reply._record("operator-1")
    record["last_pass"] = time.time() - elapsed
    auto_reply._save("operator-1", record)

    assert auto_reply._due_operators() == ["operator-1"]

    record["last_pass"] = time.time()
    auto_reply._save("operator-1", record)
    assert auto_reply._due_operators() == []


def test_an_operator_with_no_watched_account_is_never_polled():
    run(configure())
    record = auto_reply._record("operator-1")
    record["last_pass"] = 0
    auto_reply._save("operator-1", record)

    assert auto_reply._due_operators() == []


def test_the_listener_does_nothing_while_the_feature_is_off():
    operator_store.set_features({"auto_reply_enabled": False})

    run(auto_reply.worker.tick())

    assert auto_reply.worker.last_tick is None


def test_the_listener_keeps_running_after_a_failing_pass(monkeypatch):
    calls = []

    async def exploding_tick():
        calls.append(1)
        raise RuntimeError("store unreadable")

    async def scenario():
        monkeypatch.setattr(auto_reply.worker, "tick", exploding_tick)
        auto_reply.worker.task = None
        auto_reply.worker.start()
        await asyncio.sleep(0.05)
        error = auto_reply.worker.error
        await auto_reply.worker.stop()
        return error

    assert run(scenario()) == "RuntimeError"
    assert calls and auto_reply.worker.task is None


def test_one_bad_pass_still_ticks_and_reports_to_the_panel(monkeypatch):
    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        monkeypatch.setattr(auto_reply, "_client_getter", lambda: broken_getter)
        await auto_reply.worker.tick()
        return auto_reply._worker_state()

    async def broken_getter(account_id):
        raise RuntimeError("no connection")

    state = run(scenario())

    assert state["error"] is None
    assert auto_reply.worker.last_tick


def test_the_panel_sees_the_worker_state():
    config = auto_reply.public_config("operator-1", worker_state=auto_reply._worker_state())

    assert config["worker"]["running"] is False
    assert config["worker"]["error"] is None


# ---------------------------------------------------------------- what the panel sees


def test_the_panel_sees_the_rules_the_limits_and_no_sent_messages():
    async def scenario():
        await configure()
        await add_rule(keywords=["price"], reply="It is ten dollars.")
        await watch()
        return await auto_reply.get_config(REQUEST)

    body = run(scenario())["config"]

    assert body["rules"][0]["reply"] == "It is ten dollars."
    assert body["placeholders"] == ["{name}", "{first}"]
    assert body["limits"]["max_daily_cap"] == auto_reply.MAX_DAILY_CAP
    assert body["sent_today"] == 0
    assert body["outbox"] == []


def test_the_activity_list_names_the_rule_not_the_message():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(name="Price", keywords=["price"], reply="Ten dollars.")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        return await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 11, "my card is 4111111111111111 price"))[0], now=clock.step())

    run(scenario())

    activity = auto_reply._record("operator-1")["activity"]
    assert any("Price" in item["detail"] for item in activity)
    assert not any("4111111111111111" in item["detail"] for item in activity)


def test_a_conversation_row_keeps_the_peer_anonymous():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(match="any")
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        return auto_reply.public_config("operator-1")

    conv = run(scenario())["conversations"][0]

    assert len(conv["peer_ref"]) == auto_reply.PEER_REF_LENGTH
    assert conv["name"] == "Alice Lee"
    assert "peer" not in conv
    assert "suppress_key" not in conv


def test_the_outbox_view_lists_only_pending_drafts():
    ref, draft_id = a_draft()
    record = auto_reply._record("operator-1")
    record["outbox"].append({"id": "old", "status": "sent", "reply": "gone", "peer_ref": ref,
                             "account": ACCOUNT, "name": "Alice Lee", "suppress_key": "alice_x",
                             "inbound": "hi", "created": auto_reply._now_iso()})
    auto_reply._save("operator-1", record)

    listed = run(auto_reply.outbox(REQUEST))["outbox"]

    assert [draft["id"] for draft in listed] == [draft_id]


def test_the_stored_record_holds_no_plaintext_credential():
    clock = Clock()

    async def scenario():
        await configure()
        await add_rule(keywords=["price"], reply="Ten dollars, my key is abcdef123456.")
        await watch()
        return await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)

    run(scenario())

    raw = (operator_store.store_dir() / "auto_reply.secret.json").read_text()
    assert "abcdef123456" not in raw
    assert set(json.loads(raw)) == {"ciphertext"}


def test_the_router_offers_no_direct_send_and_no_group_or_scrape_surface():
    paths = sorted({route.path for route in auto_reply.router.routes})

    assert "/api/auto-reply/send" not in paths
    assert not any(word in path for path in paths for word in ("join", "add-member", "scrape", "factory"))
    assert "/api/auto-reply/outbox/{draft_id}/send" in paths


def test_the_panel_reads_the_conversation_list():
    clock = Clock()

    async def scenario():
        await configure()
        await watch()
        await auto_reply.poll_operator("operator-1", get_client=a_client(
            a_dialog(clock, 10, "hello"))[0], now=clock.t)
        return await auto_reply.conversations(REQUEST)

    assert len(run(scenario())["conversations"]) == 1
