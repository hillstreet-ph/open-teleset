"""Auto Join: link parsing, join, already-member, invalid-skip, rotation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import errors, functions, types

import telegram_ops as ops


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())


def join_client(side_effect=None, join_request=False, membership_error=None):
    """A callable Telethon-style client: client(request) joins; .get_entity resolves.

    Telegram is the only authority on membership, so the fake answers the join
    RPC and then the separate getParticipant probe.
    """
    channel = types.Channel(id=1, title='Synthetic', photo=types.ChatPhotoEmpty(),
                            date=None, username='synthetic', join_request=join_request)

    async def respond(request):
        if isinstance(request, functions.channels.GetParticipantRequest):
            if membership_error is not None:
                raise membership_error
            return SimpleNamespace(participant=types.ChannelParticipantCreator(
                user_id=1, admin_rights=types.ChatAdminRights()))
        if side_effect is not None:
            raise side_effect
        return SimpleNamespace(chats=[channel], updates=[])

    client = AsyncMock(side_effect=respond)
    client.get_entity = AsyncMock(return_value=channel)
    return client


@pytest.mark.parametrize("raw,expected", [
    ("https://t.me/+AbCdEf123456", ("invite", "AbCdEf123456")),
    ("t.me/joinchat/XyZ12345", ("invite", "XyZ12345")),
    ("+AbCdEf123456", ("invite", "AbCdEf123456")),
    ("https://t.me/durov", ("public", "durov")),
    ("@durov", ("public", "durov")),
    ("durov", ("public", "durov")),
    ("t.me/s/durov", ("public", "durov")),
    ("https://t.me/addlist/xyz", None),   # reserved path
    ("t.me/joinchat/", None),             # empty hash
    ("", None),
    ("not a link!!", None),
    # telegram.me domain
    ("https://telegram.me/durov", ("public", "durov")),
    ("https://telegram.me/+AbCdEf123456", ("invite", "AbCdEf123456")),
    ("telegram.me/joinchat/XyZ12345", ("invite", "XyZ12345")),
    # telegram.dog domain
    ("telegram.dog/durov", ("public", "durov")),
    ("https://telegram.dog/+AbCdEf123456", ("invite", "AbCdEf123456")),
    # query string params (e.g. ?start= deep links)
    ("https://t.me/durov?start=abc", ("public", "durov")),
    ("https://telegram.me/botname?start=ref123", ("public", "botname")),
])
def test_parse_join_target(raw, expected):
    assert ops._parse_join_target(raw) == expected


@pytest.mark.parametrize("kwargs", [
    {"account_id": "a", "urls": ["t.me/x"], "account_ids": ["a"]},          # single + ids
    {"urls": ["t.me/x"], "rotation_mode": "round_robin", "account_ids": []},  # rotation no ids
    {"urls": ["t.me/x"], "rotation_mode": "round_robin",
     "account_ids": ["a", "a"]},                                            # dup ids
    {"urls": ["   ", ""]},                                                   # no valid links
])
def test_invalid_request(kwargs):
    with pytest.raises(Exception):
        ops.JoinChannelsRequest(**kwargs)


@pytest.mark.asyncio
async def test_joins_public_and_invite(monkeypatch):
    client = join_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/examplechan", "https://t.me/+InviteHash12"], delay=30))
    assert res["success"] and res["joined"] == 2 and res["failed"] == 0 and res["skipped"] == 0
    # Public resolves an entity first; invite joins by hash without get_entity.
    # Each of the two joins is followed by one membership probe.
    client.get_entity.assert_awaited_once()
    assert client.call_count == 4
    probes = [c for c in client.call_args_list
              if isinstance(c.args[0], functions.channels.GetParticipantRequest)]
    assert len(probes) == 2


@pytest.mark.asyncio
async def test_already_member_counts_as_joined(monkeypatch):
    client = join_client(side_effect=errors.UserAlreadyParticipantError(None))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/+InviteHash12"], delay=30))
    assert res["joined"] == 1 and res["results"][0]["already"] is True


@pytest.mark.asyncio
async def test_a_join_telegram_did_not_record_is_failed_not_reported_as_joined(monkeypatch):
    client = join_client(membership_error=errors.UserNotParticipantError(None))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/s5philippines"], delay=30))
    assert res["joined"] == 0 and res["failed"] == 1
    assert res["results"][0]["error"] == "join_not_confirmed"


@pytest.mark.asyncio
async def test_an_approval_required_channel_reports_the_pending_request(monkeypatch):
    client = join_client(join_request=True,
                         membership_error=errors.UserNotParticipantError(None))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/approvalonly"], delay=30))
    assert res["joined"] == 0 and res["skipped"] == 1
    assert res["results"][0]["skipped"] == "join_request_pending"


@pytest.mark.asyncio
async def test_an_invite_request_sent_is_reported_as_pending_approval(monkeypatch):
    client = join_client(side_effect=errors.InviteRequestSentError(None))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/+InviteHash12"], delay=30))
    assert res["joined"] == 0 and res["skipped"] == 1
    assert res["results"][0]["skipped"] == "join_request_pending"


@pytest.mark.asyncio
async def test_an_unexpected_error_is_not_echoed_as_a_class_name(monkeypatch):
    client = join_client(side_effect=TypeError("Cannot cast Chat to any kind of InputChannel."))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/legacychat"], delay=30))
    assert res["failed"] == 1
    assert res["results"][0]["error"] == "join_failed"


@pytest.mark.asyncio
async def test_invalid_link_skipped(monkeypatch):
    client = join_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["not a link!!", "https://t.me/goodone"], delay=30))
    assert res["joined"] == 1 and res["skipped"] == 1
    assert res["results"][0]["skipped"] == "invalid_link"


@pytest.mark.asyncio
async def test_single_account_flood_stops(monkeypatch):
    client = join_client(side_effect=errors.FloodWaitError(None, 60))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/chanone", "https://t.me/chantwo"], delay=30))
    assert res["joined"] == 0 and res["failed"] == 1
    assert res["stopped_reason"] == "FloodWaitError" and res["pending"] == 1


@pytest.mark.asyncio
async def test_rotation_rotates_off_flooded_account(monkeypatch):
    flooded = join_client(side_effect=errors.FloodWaitError(None, 60))
    healthy = join_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(side_effect=[flooded, healthy]))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        urls=["https://t.me/chanone", "https://t.me/chantwo"], rotation_mode="round_robin",
        account_ids=["flooded", "healthy"], delay=30))
    # The flooded account is dropped; both links are joined by the healthy one.
    assert res["joined"] == 2 and res["failed"] == 0 and res["stopped_reason"] is None
    assert {r["account_id"] for r in res["results"]} == {"healthy"}


@pytest.mark.asyncio
async def test_pool_unavailable_returns_400(monkeypatch):
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await ops.join_channels(ops.JoinChannelsRequest(account_id="acct", urls=["https://t.me/a"]))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_limit_caps_joins(monkeypatch):
    client = join_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=[f"https://t.me/chan{i}" for i in range(10)], limit=3, delay=30))
    assert res["joined"] == 3 and res["total"] == 10


def test_request_defaults_to_no_per_run_cap():
    request = ops.JoinChannelsRequest(
        account_id="acct", urls=[f"https://t.me/chan{i}" for i in range(120)])
    assert request.limit == 0


@pytest.mark.asyncio
async def test_zero_limit_joins_every_link(monkeypatch):
    client = join_client()
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=[f"https://t.me/chan{i}" for i in range(75)], limit=0, delay=30))
    assert res["joined"] == 75 and res["total"] == 75


def test_request_rejects_a_negative_cap():
    with pytest.raises(Exception):
        ops.JoinChannelsRequest(account_id="acct", urls=["@chanone"], limit=-1)
