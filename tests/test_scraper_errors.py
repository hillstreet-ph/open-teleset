"""Scraper failures must be actionable and must not leak Telegram internals."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import types, utils

import telegram_ops as ops


def fake_client(entity=None, participants=None, error=None):
    get_entity = AsyncMock(return_value=entity, side_effect=error)
    get_participants = AsyncMock(return_value=participants or [])
    return SimpleNamespace(get_entity=get_entity, get_participants=get_participants)


@pytest.mark.asyncio
async def test_tme_url_is_resolved_to_a_username(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="https://t.me/example_channel")
    )
    assert result["success"] is True
    client.get_entity.assert_awaited_once_with("@example_channel")


@pytest.mark.asyncio
async def test_numeric_channel_id_is_sent_as_an_integer(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="-1001234567890")
    )
    client.get_entity.assert_awaited_once_with(-1001234567890)


@pytest.mark.asyncio
async def test_private_invite_link_is_rejected_without_calling_telegram(monkeypatch):
    client = fake_client()
    get_client = AsyncMock(return_value=client)
    monkeypatch.setattr(ops.account_manager, "get_client", get_client)
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="https://t.me/+AbCdEf123456")
        )
    assert exc.value.status_code == 400
    assert "invite" in exc.value.detail.lower()
    client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolvable_target_returns_400_not_500(monkeypatch):
    client = fake_client(error=ValueError("Cannot find any entity corresponding to 'ghost'"))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="@ghost")
        )
    assert exc.value.status_code == 400
    assert "could not find" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_unexpected_error_is_generic_and_does_not_leak_details(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    client.get_participants.side_effect = RuntimeError("proxy password=hunter2")
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="@example")
        )
    assert exc.value.status_code == 500
    assert "hunter2" not in exc.value.detail
    assert "RuntimeError" not in exc.value.detail


@pytest.mark.parametrize("target,expected", [
    ("https://t.me/example_channel/123", "@example_channel"),
    ("t.me/s/example_channel", "@example_channel"),
    ("telegram.me/example_channel?ref=share", "@example_channel"),
    ("@example_channel", "@example_channel"),
    ("example_channel", "@example_channel"),
    ("-1001234567890", -1001234567890),
])
def test_scrape_target_accepts_links_and_usernames(target, expected):
    assert ops._normalise_scrape_target(target) == expected


@pytest.mark.parametrize("target", [
    "https://t.me/+AbCdEf123456",
    "t.me/joinchat/XyZ12345",
    "t.me/c/123456/78",
    "https://t.me/share/url",
    "not a target",
    "",
])
def test_scrape_target_rejects_private_invites_and_garbage(target):
    assert ops._normalise_scrape_target(target) is None


@pytest.mark.asyncio
async def test_numeric_peer_id_falls_back_to_dialogs():
    channel = types.Channel(id=555, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    peer_id = utils.get_peer_id(channel)

    async def dialogs():
        yield SimpleNamespace(entity=channel)

    client = SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("cannot find")),
                             iter_dialogs=dialogs)
    assert await ops._resolve_target_entity(client, peer_id) is channel


@pytest.mark.asyncio
async def test_detected_groups_endpoint_lists_groups_and_channels(monkeypatch):
    channel = types.Channel(id=123, title="Big Channel", photo=types.ChatPhotoEmpty(), date=None,
                            username="bigchan", participants_count=42)
    chat = types.Chat(id=7, title="Small Group", photo=types.ChatPhotoEmpty(), participants_count=5,
                      date=None, version=1)
    dialogs = [
        SimpleNamespace(is_group=True, is_channel=False, entity=chat, name="Small Group", id=7),
        SimpleNamespace(is_group=False, is_channel=True, entity=channel, name="Big Channel", id=123),
    ]
    client = SimpleNamespace(get_dialogs=AsyncMock(return_value=dialogs))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.list_account_groups(ops.GroupListRequest(account_id="account"))
    assert result["success"] is True and len(result["groups"]) == 2
    by_type = {group["type"]: group for group in result["groups"]}
    assert by_type["channel"]["username"] == "bigchan"
    assert by_type["channel"]["members"] == 42
    assert by_type["group"]["name"] == "Small Group"
    assert all(isinstance(group["peer_id"], int) for group in result["groups"])


@pytest.mark.asyncio
async def test_detected_groups_requires_an_account():
    with pytest.raises(HTTPException) as exc:
        await ops.list_account_groups(ops.GroupListRequest(account_id=""))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_detected_groups_surfaces_the_account_connection_error(monkeypatch):
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=None))
    monkeypatch.setitem(
        ops.account_manager.connection_errors, "account",
        "Account has no saved Telegram session. Sign in again using Add account.",
    )
    with pytest.raises(HTTPException) as exc:
        await ops.list_account_groups(ops.GroupListRequest(account_id="account"))
    assert exc.value.status_code == 400
    assert "no saved Telegram session" in exc.value.detail


@pytest.mark.asyncio
async def test_connect_without_a_saved_session_reports_an_actionable_error():
    from account_manager import AccountManager

    manager = AccountManager.__new__(AccountManager)
    manager.accounts = {"broken": {"name": "Broken"}}
    manager.connection_errors = {}
    manager.clients = {}
    manager._client_tasks = {}
    manager.saved_proxy = lambda account_id: None
    manager._paused_by_cooldown = lambda account_id: False

    assert await manager._connect_saved_account("broken") is None
    assert "no saved Telegram session" in manager.connection_errors["broken"]
