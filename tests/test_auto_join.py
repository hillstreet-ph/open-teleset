"""Auto Join: link parsing, join, already-member, invalid-skip, rotation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import errors

import telegram_ops as ops


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())


def join_client(side_effect=None):
    """A callable Telethon-style client: client(request) joins; .get_entity resolves."""
    client = AsyncMock(side_effect=side_effect)
    client.get_entity = AsyncMock(side_effect=lambda v: SimpleNamespace(id=1, username=str(v)))
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
    client.get_entity.assert_awaited_once()
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_already_member_counts_as_joined(monkeypatch):
    client = join_client(side_effect=errors.UserAlreadyParticipantError(None))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    res = await ops.join_channels(ops.JoinChannelsRequest(
        account_id="acct", urls=["https://t.me/+InviteHash12"], delay=30))
    assert res["joined"] == 1 and res["results"][0]["already"] is True


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
