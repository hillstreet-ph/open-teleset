import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from telethon.tl.types import PeerUser

import outreach_policy as policy_module
import telegram_ops
import scheduler
import batch_operations
import main


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    document = {
        "version": 1,
        "approvals": {action: {"action": action, "expires_at": "2099-01-01T00:00:00Z",
                                 "account_hashes": [policy_module.hash_identifier("account")]} for action in policy_module.ACTIONS},
        "consents": [{"subject_hash": policy_module.hash_identifier(user), "actions": sorted(policy_module.ACTIONS),
                      "source": "explicit", "granted_at": "2020-01-01T00:00:00Z",
                      "expires_at": "2099-01-01T00:00:00Z", "account_hashes": [policy_module.hash_identifier("account")]}
                     for user in (111, 222)],
        "suppressions": [],
    }
    def save():
        path.write_text(json.dumps(document))
    save()
    evaluator = policy_module.OutreachPolicy(str(path))
    for module in (policy_module, telegram_ops, scheduler, batch_operations):
        monkeypatch.setattr(module, "outreach_policy", evaluator)
    monkeypatch.setenv("OUTREACH_RATE_DB", str(tmp_path / "rate.sqlite3"))
    clock = [1000.0]
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(policy_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return SimpleNamespace(data=document, save=save, evaluator=evaluator, sleeps=sleeps)


@pytest.mark.parametrize("field,value", [("active", "true"), ("active", 1), ("active", None)])
def test_malformed_suppression_denies(policy, field, value):
    policy.data["suppressions"] = [{"subject_hash": policy_module.hash_identifier(111), field: value}]
    policy.save()
    decision = policy.evaluator.authorize(action="personal_send", subject=111, account_id="account", approval_id="personal_send")
    assert not decision.allowed and decision.reason == "policy_unavailable"


@pytest.mark.parametrize("value", ["personal_send", None, {}, ["personal_send", {}]])
def test_malformed_actions_deny(policy, value):
    policy.data["consents"][0]["actions"] = value
    policy.save()
    assert not policy.evaluator.authorize(action="personal_send", subject=111, account_id="account", approval_id="personal_send").allowed


@pytest.mark.asyncio
async def test_recheck_optout_after_rate_wait(policy, monkeypatch):
    send = AsyncMock()
    kwargs = dict(action="personal_send", subject=111, account_id="account", approval_id="personal_send", send=send)
    await policy_module.dispatch_outreach(**kwargs)
    async def revoke(_):
        policy.data["suppressions"] = [{"subject_hash": policy_module.hash_identifier(111), "active": True}]
        policy.save()
    monkeypatch.setattr(asyncio, "sleep", revoke)
    with pytest.raises(policy_module.OutreachDenied, match="suppressed"):
        await policy_module.dispatch_outreach(**kwargs)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_failed_attempt_is_still_rate_limited(policy):
    send = AsyncMock(side_effect=[RuntimeError("private message"), None])
    kwargs = dict(action="member_add", subject=111, account_id="account", approval_id="member_add", send=send)
    with pytest.raises(RuntimeError):
        await policy_module.dispatch_outreach(**kwargs)
    await policy_module.dispatch_outreach(**kwargs)
    assert policy.sleeps == [35]


@pytest.mark.parametrize("kwargs", [{"limit": -1}, {"limit": 0}, {"limit": 11}, {"delay": float("nan")}, {"delay": float("inf")}])
def test_member_bounds_are_not_bypassable(kwargs):
    with pytest.raises(ValidationError):
        telegram_ops.AddMembersRequest(account_id="account", dest_target="999", usernames=["111"], **kwargs)


@pytest.mark.asyncio
async def test_bulk_honors_mid_batch_suppression(policy, monkeypatch):
    async def sent(*args, **kwargs):
        policy.data["suppressions"] = [{"subject_hash": policy_module.hash_identifier(222), "active": True}]
        policy.save()
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=lambda value: PeerUser(int(value))),
                             send_message=AsyncMock(side_effect=sent))
    monkeypatch.setattr(telegram_ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await telegram_ops.bulk_send_personal(telegram_ops.BulkSendRequest(
        account_id="account", message="test", targets=["111", "222"], approval_id="personal_send"))
    assert client.send_message.await_count == 1
    assert result["failed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_message", "invented_action"])
async def test_legacy_or_unknown_schedule_cannot_bypass(policy, monkeypatch, action):
    task = scheduler.TaskScheduler.__new__(scheduler.TaskScheduler)
    task._save_schedules = lambda: None
    client = AsyncMock()
    monkeypatch.setattr(scheduler.account_manager, "get_client", client)
    record = {"action": action, "accounts": ["account"], "friend_ids": [111], "name": "legacy"}
    assert not await task._execute_schedule(record)
    assert record["enabled"] is False
    client.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("send_message", (111, "private")), ("reply_message", (111, 1, "private")),
    ("forward_message", (222, 1, 111)), ("send_photo", (111, "photo.jpg")),
    ("send_location", (111, 1.0, 2.0)),
])
@pytest.mark.asyncio
async def test_direct_tools_require_approval(policy, monkeypatch, name, args):
    client = SimpleNamespace(get_entity=AsyncMock(return_value=PeerUser(111)), send_message=AsyncMock(),
                             send_file=AsyncMock(), forward_messages=AsyncMock())
    monkeypatch.setattr(main, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(main, "get_default_account_id", lambda: "account")
    result = await getattr(main, name)(*args)
    assert "failed" in result.lower()
    client.send_message.assert_not_called()
    client.send_file.assert_not_called()
    client.forward_messages.assert_not_called()


@pytest.mark.asyncio
async def test_template_fanout_limit_before_render(policy, monkeypatch):
    render = AsyncMock()
    monkeypatch.setattr(batch_operations.template_manager, "render_template", render)
    result = await batch_operations.BatchOperations().batch_send_template("111", "template", account_ids=list("abcdef"))
    assert not result["success"]
    render.assert_not_called()


@pytest.mark.parametrize("record", [
    {"action": "send_message", "friend_ids": list(range(21))},
    {"action": "send_message", "interval": float("nan")},
    {"action": "add_members", "add_usernames": [111], "add_limit": -1},
    {"action": "add_members", "source_target": "scraped", "add_usernames": [111]},
])
def test_persisted_schedule_bounds(record):
    with pytest.raises(ValueError):
        scheduler.schedule_requirements(record)


@pytest.mark.asyncio
async def test_username_resolves_to_consented_peer(policy, monkeypatch):
    client = SimpleNamespace(get_entity=AsyncMock(return_value=PeerUser(111)), send_message=AsyncMock())
    monkeypatch.setattr(telegram_ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await telegram_ops.bulk_send_personal(telegram_ops.BulkSendRequest(
        account_id="account", message="synthetic", targets=["@alias"], approval_id="personal_send"))
    assert result["success"] is True
    client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_delete_summary_without_real_deletion(monkeypatch):
    monkeypatch.setattr(batch_operations.account_manager, "remove_account", AsyncMock(return_value=True))
    result = await batch_operations.BatchOperations().batch_delete_accounts(["synthetic-test"])
    assert result["success"] is True and result["success_count"] == 1


@pytest.mark.asyncio
async def test_mcp_login_uses_encrypted_account_manager(monkeypatch):
    import account_manager
    monkeypatch.setattr(account_manager.account_manager, "accounts", {"test": {"session_string": "synthetic"}})
    monkeypatch.setattr(main, "get_default_account_id", lambda: "test")
    assert await main.check_login()

@pytest.mark.asyncio
async def test_saved_file_uses_only_literal_self_peer(monkeypatch):
    from telethon.tl.types import InputPeerSelf
    client = SimpleNamespace(send_file=AsyncMock())
    monkeypatch.setattr(main, 'get_client', AsyncMock(return_value=client))
    await main.save_file('synthetic.txt')
    assert client.send_file.await_count == 1
    assert isinstance(client.send_file.await_args.args[0], InputPeerSelf)
