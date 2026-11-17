"""Auto Forwarder: mirror new messages from a source chat into a target chat.

The invariants under test: a rule only forwards messages it has not forwarded
before, only within the operator's own record, never past a Telegram flood wait,
and a failure is reported by a safe code rather than by exception text.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from telethon import errors

import auto_forward
from open_teleset import operator_store

REQUEST = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="operator-1")))
OTHER = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="operator-2")))
ANON = SimpleNamespace(state=SimpleNamespace(user=None))
ACCOUNT = "acct-1"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({"auto_forward_enabled": True})
    monkeypatch.setattr(auto_forward.log_manager, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(auto_forward.worker, "task", None)
    monkeypatch.setattr(auto_forward.worker, "error", None)
    monkeypatch.setattr(auto_forward.worker, "last_tick", None)
    yield


class Message:
    def __init__(self, mid):
        self.id = mid


class FakeClient:
    """A source chat with a growing history and a target that records forwards."""

    def __init__(self, messages=None, *, fail=None):
        self.history = list(messages or [])
        self.fail = fail
        self.forwarded = []
        self.reads = []

    async def get_entity(self, ref):
        if ref in ("@missing_source", "@missing_target"):
            raise ValueError("cannot resolve")
        return SimpleNamespace(id=7, username=str(ref).lstrip("@"))

    async def iter_messages(self, entity, min_id=0, limit=None, reverse=False):
        self.reads.append({"entity": entity.username, "min_id": min_id, "limit": limit})
        for message in self.history:
            if message.id > min_id:
                yield message

    async def forward_messages(self, entity, messages, from_peer=None):
        if self.fail:
            raise self.fail
        self.forwarded.append({"to": entity.username, "ids": list(messages),
                               "from": from_peer.username})
        return [SimpleNamespace(id=1000 + index) for index, _ in enumerate(messages)]


class YieldingClient(FakeClient):
    """Pauses between reading the cursor and forwarding so a second pass can start."""

    async def iter_messages(self, entity, min_id=0, limit=None, reverse=False):
        await asyncio.sleep(0)
        async for message in super().iter_messages(entity, min_id, limit, reverse):
            yield message


def getter_for(client):
    async def get_client(account_id):
        return client if account_id == ACCOUNT else None

    return get_client


async def add_rule(source="@sourcechat", target="@targetchat", *, account=ACCOUNT, name="", enabled=True):
    return await auto_forward.create_rule(auto_forward.RuleBody(
        name=name, account=account, source=source, target=target, enabled=enabled), REQUEST)


async def switch_on():
    await auto_forward.put_config(auto_forward.ConfigBody(enabled=True), REQUEST)


def test_config_starts_off_and_toggles():
    config = run(auto_forward.get_config(REQUEST))
    assert config["enabled"] is False and config["rules"] == []

    run(switch_on())
    assert run(auto_forward.get_config(REQUEST))["enabled"] is True


def test_poll_seconds_are_clamped_to_the_supported_range():
    run(switch_on())
    run(auto_forward.put_config(auto_forward.ConfigBody(enabled=True, poll_seconds=5), REQUEST))
    assert run(auto_forward.get_config(REQUEST))["poll_seconds"] == auto_forward.MIN_POLL_SECONDS

    run(auto_forward.put_config(auto_forward.ConfigBody(enabled=True, poll_seconds=5000), REQUEST))
    assert run(auto_forward.get_config(REQUEST))["poll_seconds"] == auto_forward.MAX_POLL_SECONDS

    run(auto_forward.put_config(auto_forward.ConfigBody(enabled=True, poll_seconds=120), REQUEST))
    assert run(auto_forward.get_config(REQUEST))["poll_seconds"] == 120

    # A toggle that omits the field keeps the stored interval.
    run(auto_forward.put_config(auto_forward.ConfigBody(enabled=False), REQUEST))
    assert run(auto_forward.get_config(REQUEST))["poll_seconds"] == 120


def test_a_rule_needs_a_source_a_target_and_an_account():
    with pytest.raises(HTTPException) as missing:
        run(add_rule(source="  "))
    assert missing.value.status_code == 400

    with pytest.raises(HTTPException) as same:
        run(add_rule(source="@same", target="https://t.me/Same"))
    assert same.value.status_code == 400
    assert "different" in same.value.detail.lower()


def test_targets_are_normalised():
    rule = run(add_rule(source="https://t.me/SourceChat", target="@DestChat", name="  Mirror  "))
    assert rule["rule"]["source"] == "sourcechat"
    assert rule["rule"]["target"] == "destchat"
    assert rule["rule"]["name"] == "Mirror"
    assert rule["rule"]["last_id"] == 0


def test_rules_are_operator_scoped():
    run(add_rule())
    assert len(run(auto_forward.get_config(REQUEST))["rules"]) == 1
    assert run(auto_forward.get_config(OTHER))["rules"] == []


def test_an_anonymous_caller_is_rejected():
    with pytest.raises(HTTPException) as denied:
        run(auto_forward.get_config(ANON))
    assert denied.value.status_code == 403


def test_the_feature_switch_blocks_every_route():
    operator_store.set_features({"auto_forward_enabled": False})
    with pytest.raises(HTTPException) as off:
        run(auto_forward.get_config(REQUEST))
    assert off.value.status_code == 403


@pytest.mark.asyncio
async def test_a_pass_forwards_only_messages_it_has_not_seen(monkeypatch):
    await add_rule()
    await switch_on()
    client = FakeClient([Message(1), Message(2), Message(3)])
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    first = await auto_forward.poll_operator("operator-1")
    assert first["forwarded"] == 3
    assert client.forwarded == [{"to": "targetchat", "ids": [1, 2, 3], "from": "sourcechat"}]

    client.history.append(Message(4))
    second = await auto_forward.poll_operator("operator-1")
    assert second["forwarded"] == 1
    assert client.forwarded[-1]["ids"] == [4]
    assert client.reads[-1]["min_id"] == 3


@pytest.mark.asyncio
async def test_a_manual_pass_and_a_scheduled_pass_cannot_forward_the_same_message(monkeypatch):
    """Both passes would read the same cursor and forward one message twice."""
    await add_rule()
    await switch_on()
    client = YieldingClient([Message(1)])
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    first, second = await asyncio.gather(
        auto_forward.poll_operator("operator-1"),
        auto_forward.poll_operator("operator-1"))

    assert first["forwarded"] + second["forwarded"] == 1
    assert sorted([first["reason"], second["reason"]]) == ["", "already_running"]
    assert client.forwarded == [{"to": "targetchat", "ids": [1], "from": "sourcechat"}]


@pytest.mark.asyncio
async def test_a_disabled_rule_never_runs(monkeypatch):
    await add_rule(enabled=False)
    await switch_on()
    client = FakeClient([Message(1)])
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    result = await auto_forward.poll_operator("operator-1")
    assert result["forwarded"] == 0 and client.reads == []


@pytest.mark.asyncio
async def test_an_unresolved_chat_is_a_safe_error(monkeypatch):
    await add_rule(source="@missing_source")
    await switch_on()
    client = FakeClient([Message(1)])
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    result = await auto_forward.poll_operator("operator-1")
    assert result["forwarded"] == 0 and result["errors"] == 1
    rule = (await auto_forward.get_config(REQUEST))["rules"][0]
    assert rule["error"] == "chat_not_found"
    assert "cannot resolve" not in json.dumps(rule)


@pytest.mark.asyncio
async def test_a_telegram_refusal_is_not_leaked(monkeypatch):
    await add_rule()
    await switch_on()
    client = FakeClient([Message(1)], fail=errors.ChatForwardsRestrictedError(request=None))
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    result = await auto_forward.poll_operator("operator-1")
    assert result["errors"] == 1
    rule = (await auto_forward.get_config(REQUEST))["rules"][0]
    assert rule["error"] == "ChatForwardsRestrictedError"
    assert rule["last_id"] == 0


@pytest.mark.asyncio
async def test_a_flood_wait_parks_the_account_until_it_clears(monkeypatch):
    await add_rule()
    await add_rule(source="@second", target="@targettwo")
    await switch_on()
    client = FakeClient([Message(1)], fail=errors.FloodWaitError(request=None, capture=120))
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    first = await auto_forward.poll_operator("operator-1", now=1000.0)
    assert first["forwarded"] == 0 and first["errors"] == 2
    reads_after_flood = len(client.reads)

    # The rules must not hit Telegram again until the wait has passed.
    second = await auto_forward.poll_operator("operator-1", now=1060.0)
    assert second["reason"] == "waiting"
    assert len(client.reads) == reads_after_flood


@pytest.mark.asyncio
async def test_run_now_runs_one_pass_and_reports(monkeypatch):
    await add_rule()
    await switch_on()
    client = FakeClient([Message(9)])
    monkeypatch.setattr(auto_forward, "_client_getter", lambda: getter_for(client))

    response = await auto_forward.run_now(REQUEST)
    assert response["success"] is True
    assert response["result"]["forwarded"] == 1
    assert response["config"]["rules"][0]["last_id"] == 9


@pytest.mark.asyncio
async def test_run_now_requires_the_switch():
    await add_rule()
    with pytest.raises(HTTPException) as off:
        await auto_forward.run_now(REQUEST)
    assert off.value.status_code == 409


def test_the_rule_cap_is_enforced():
    for index in range(auto_forward.MAX_RULES):
        run(add_rule(source=f"@source{index}", target="@dest"))
    with pytest.raises(HTTPException) as too_many:
        run(add_rule(source="@onemore", target="@dest"))
    assert too_many.value.status_code == 409
