from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from telethon import errors, types

import outreach_policy as policy_module
import telegram_ops as ops
from test_outreach_execution import policy as base_policy


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.fixture(name="policy")
def policy_fixture(tmp_path, monkeypatch):
    return base_policy.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("selected,days,expected", [
    ("recently", 3, True), ("recently", 3.001, False),
    ("7_days", 7, True), ("7_days", 7.001, False),
    ("15_days", 15, True), ("15_days", 15.001, False),
    ("30_days", 30, True), ("30_days", 30.001, False),
    ("7_days", -1, False),
])
def test_exact_activity_boundaries(selected, days, expected):
    user = types.User(id=111, status=types.UserStatusOffline(NOW - timedelta(days=days)))
    assert ops.matches_activity(user, selected, NOW) is expected


@pytest.mark.parametrize("status,accepted", [
    (types.UserStatusOnline(NOW + timedelta(seconds=30)), {"recently", "7_days", "15_days", "30_days"}),
    (types.UserStatusRecently(), {"recently", "7_days", "15_days", "30_days"}),
    (types.UserStatusLastWeek(), {"7_days", "15_days", "30_days"}),
    (types.UserStatusLastMonth(), {"30_days"}),
    (types.UserStatusEmpty(), set()), (None, set()),
])
def test_approximate_status_never_invents_timestamp(status, accepted):
    user = types.User(id=111, status=status)
    assert ops.activity_details(user)["last_seen"] is None
    for selected in ("recently", "7_days", "15_days", "30_days"):
        assert ops.matches_activity(user, selected, NOW) is (selected in accepted)
    assert ops.matches_activity(user, "all", NOW)


def test_naive_timestamp_is_utc():
    user = types.User(id=111, status=types.UserStatusOffline(NOW.replace(tzinfo=None)))
    assert ops.matches_activity(user, "recently", NOW)
    assert ops.activity_details(user)["last_seen"].endswith("+00:00")


@pytest.mark.parametrize("kwargs", [
    {"rotation_mode": "round_robin"}, {"account_ids": ["account"]},
    {"account_id": "", "rotation_mode": "round_robin", "account_ids": ["account", "account"]},
    {"activity_filter": "60_days"}, {"per_account_limit": 21},
    {"per_account_limit": True}, {"rotation_mode": "automatic_fallback"},
])
def test_invalid_rotation_input(kwargs):
    with pytest.raises(ValidationError):
        ops.BulkSendRequest(**{"account_id": "account", "message": "synthetic", "targets": ["111"], **kwargs})


@pytest.mark.parametrize("limit", [0, -1, 10001, True])
def test_scraper_scan_bounds(limit):
    with pytest.raises(ValidationError):
        ops.ScrapeRequest(account_id="account", target="group", limit=limit)


@pytest.mark.asyncio
async def test_scraper_filters_bots_unknown_and_approximate_month(monkeypatch):
    participants = [types.User(id=1, bot=False, status=types.UserStatusRecently()),
                    types.User(id=2, bot=True, status=types.UserStatusRecently()),
                    types.User(id=3, bot=False, status=types.UserStatusLastMonth()),
                    types.User(id=4, bot=False)]
    client = SimpleNamespace(get_entity=AsyncMock(), get_participants=AsyncMock(return_value=participants))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.scrape_members(ops.ScrapeRequest(account_id="account", target="group", activity_filter="15_days"))
    assert [m["id"] for m in result["members"]] == [1]
    assert result["scanned"] == 4 and result["excluded"] == 3
    assert result["members"][0]["last_seen"] is None


def allow_second_account(policy):
    second = policy_module.hash_identifier("second")
    for approval in policy.data["approvals"].values():
        approval["account_hashes"].append(second)
    for consent in policy.data["consents"]:
        consent["account_hashes"].append(second)
    policy.save()


def fake_client(status=None):
    return SimpleNamespace(get_entity=AsyncMock(side_effect=lambda value: types.User(id=int(value), status=status)),
                           send_message=AsyncMock())


@pytest.mark.asyncio
async def test_round_robin_deduplicates_and_keeps_global_delay(policy, monkeypatch):
    allow_second_account(policy)
    first, second = fake_client(), fake_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(side_effect=[first, second]))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(message="synthetic", targets=["111", "@111", "222"],
        account_ids=["account", "second"], rotation_mode="round_robin", per_account_limit=1, approval_id="personal_send"))
    assert result["sent"] == 2 and result["skipped"] == 1 and result["pending"] == 0
    assert [r["account_id"] for r in result["results"]] == ["account", "second"]
    first.send_message.assert_awaited_once()
    second.send_message.assert_awaited_once()
    assert policy.sleeps == [3]


@pytest.mark.asyncio
async def test_recipient_aliases_cannot_send_twice(policy, monkeypatch):
    client = fake_client()
    client.get_entity = AsyncMock(return_value=types.User(id=111))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", message="synthetic",
        targets=["alias", "111"], approval_id="personal_send"))
    assert result["sent"] == 1 and result["skipped"] == 1
    client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unmatched_activity_is_skipped_without_send(policy, monkeypatch):
    client = fake_client(types.UserStatusLastMonth())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", message="synthetic",
        targets=["111"], activity_filter="15_days", approval_id="personal_send"))
    assert result["sent"] == 0 and result["skipped"] == 1
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_rotation_approval_missing_for_account_denies_before_client(policy, monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(ops.account_manager, "get_client", connect)
    with pytest.raises(HTTPException) as exc:
        await ops.bulk_send_personal(ops.BulkSendRequest(message="synthetic", targets=["111"],
            rotation_mode="round_robin", account_ids=["account", "second"], approval_id="personal_send"))
    assert exc.value.status_code == 403
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_rotation_does_not_fallback_when_recipient_lacks_sender_consent(policy, monkeypatch):
    allow_second_account(policy)
    policy.data["consents"][1]["account_hashes"].remove(policy_module.hash_identifier("second"))
    policy.save()
    first, second = fake_client(), fake_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(side_effect=[first, second]))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(message="synthetic", targets=["111", "222"],
        rotation_mode="round_robin", account_ids=["account", "second"], approval_id="personal_send"))
    assert result["sent"] == 1 and result["stopped_reason"] == "consent_missing_or_invalid"
    assert first.send_message.await_count == 1
    second.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [errors.FloodWaitError(None, 120), errors.PeerFloodError(None)])
@pytest.mark.parametrize("during_lookup", [True, False])
async def test_provider_restriction_stops_pool_and_survives_new_batch(policy, monkeypatch, failure, during_lookup):
    allow_second_account(policy)
    first, second = fake_client(), fake_client()
    if during_lookup:
        first.get_entity.side_effect = failure
    else:
        first.send_message.side_effect = failure
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(side_effect=[first, second, first]))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(message="synthetic", targets=["111", "222"],
        rotation_mode="round_robin", account_ids=["account", "second"], approval_id="personal_send"))
    assert result["failed"] == 1 and result["pending"] == 1 and result["stopped_reason"]
    second.send_message.assert_not_called()
    first.get_entity.side_effect = lambda value: types.User(id=int(value))
    first.send_message.reset_mock(side_effect=True)
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", message="synthetic",
        targets=["111"], approval_id="personal_send"))
    assert result["stopped_reason"] == "account_cooldown"
    first.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_cannot_be_shortened_and_expires(policy):
    policy_module.account_cooldown("account", 120)
    policy_module.account_cooldown("account", 10)
    send = AsyncMock()
    with pytest.raises(policy_module.OutreachDenied, match="account_cooldown"):
        await policy_module.dispatch_outreach(action="personal_send", subject=111, account_id="account",
            approval_id="personal_send", send=send)
    await policy_module.asyncio.sleep(121)
    await policy_module.dispatch_outreach(action="personal_send", subject=111, account_id="account",
        approval_id="personal_send", send=send)
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_unavailable_sends_nothing(policy, monkeypatch):
    allow_second_account(policy)
    client = fake_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(side_effect=[client, None]))
    with pytest.raises(HTTPException) as exc:
        await ops.bulk_send_personal(ops.BulkSendRequest(message="synthetic", targets=["111"],
            rotation_mode="round_robin", account_ids=["account", "second"], approval_id="personal_send"))
    assert exc.value.status_code == 400
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_capacity_denied_before_connecting(policy, monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(ops.account_manager, "get_client", connect)
    with pytest.raises(HTTPException) as exc:
        await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", message="synthetic",
            targets=["111", "222"], per_account_limit=1, approval_id="personal_send"))
    assert exc.value.status_code == 400
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_from_another_worker_during_rate_wait(policy, monkeypatch):
    send = AsyncMock()
    kwargs = dict(action="personal_send", subject=111, account_id="account",
                  approval_id="personal_send", send=send)
    await policy_module.dispatch_outreach(**kwargs)
    async def restrict(_):
        policy_module.account_cooldown("account", 120)
    monkeypatch.setattr(policy_module.asyncio, "sleep", restrict)
    with pytest.raises(policy_module.OutreachDenied, match="account_cooldown"):
        await policy_module.dispatch_outreach(**kwargs)
    assert send.await_count == 1
