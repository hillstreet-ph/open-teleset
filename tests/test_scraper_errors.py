"""Scraper failures must be actionable and must not leak Telegram internals."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import errors, functions, types, utils

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


class NonAdminClient:
    """A non-admin account: the roster is hidden, recent messages are readable."""

    def __init__(self, entity, messages, member_count=1500):
        self.get_entity = AsyncMock(return_value=entity)
        self.get_participants = AsyncMock(
            side_effect=errors.ChatAdminRequiredError(request=None))
        self.iter_messages = self._messages(messages)
        self.member_count = member_count
        self.history_limit = None

    def _messages(self, messages):
        async def walk(entity, limit):
            self.history_limit = limit
            for message in messages:
                yield message
        return walk

    async def __call__(self, request):
        assert isinstance(request, functions.channels.GetFullChannelRequest)
        return SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=self.member_count))


@pytest.mark.asyncio
async def test_non_admin_scrape_returns_recent_posters_with_a_clear_notice(monkeypatch):
    entity = types.Channel(id=9, title="Hidden Members", photo=types.ChatPhotoEmpty(),
                           date=None)
    ana = types.User(id=11, first_name="Ana", username="ana")
    ben = types.User(id=12, first_name="Ben")
    channel_post = types.Channel(id=9, title="Hidden Members",
                                 photo=types.ChatPhotoEmpty(), date=None)
    client = NonAdminClient(entity, [
        SimpleNamespace(sender=ana),
        SimpleNamespace(sender=ana),
        SimpleNamespace(sender=channel_post),
        SimpleNamespace(sender=ben),
    ])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    result = await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="@hidden_members", limit=50)
    )

    assert result["success"] is True
    assert result["source"] == "recent_posters"
    assert result["limited"] is True
    assert result["member_count"] == 1500
    assert "non-admin" in result["notice"].lower()
    assert [member["id"] for member in result["members"]] == [11, 12]
    assert result["scanned"] == 2
    assert client.history_limit <= 1000


@pytest.mark.asyncio
async def test_non_admin_scrape_without_visible_senders_still_explains_itself(monkeypatch):
    entity = types.Channel(id=9, title="Announcements", photo=types.ChatPhotoEmpty(),
                           date=None)
    client = NonAdminClient(entity, [], member_count=88)
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    result = await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="@announcements", limit=20)
    )

    assert result["success"] is True
    assert result["total"] == 0
    assert result["limited"] is True
    assert "88" in result["notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["metadata", "sender", "history"])
async def test_non_admin_fallback_stops_on_flood_wait(monkeypatch, stage):
    class RateLimitedClient(NonAdminClient):
        async def __call__(self, request):
            if stage == "metadata":
                raise errors.FloodWaitError(request=None, capture=73)
            return await super().__call__(request)

    async def sender():
        raise errors.FloodWaitError(request=None, capture=73)

    async def history(entity, limit):
        raise errors.FloodWaitError(request=None, capture=73)
        yield

    entity = types.Channel(id=9, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    client = RateLimitedClient(entity, [SimpleNamespace(sender=None, get_sender=sender)])
    if stage == "history":
        client.iter_messages = history
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(ops.ScrapeRequest(account_id="account", target="@hidden_members"))
    assert exc.value.status_code == 429
    assert "73s" in exc.value.detail
    if stage == "metadata":
        assert client.history_limit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["metadata", "sender"])
async def test_non_admin_fallback_does_not_hide_unexpected_failures(monkeypatch, stage):
    class BrokenClient(NonAdminClient):
        async def __call__(self, request):
            if stage == "metadata":
                raise RuntimeError("credential=private-test-value")
            return await super().__call__(request)

    async def sender():
        raise RuntimeError("credential=private-test-value")

    entity = types.Channel(id=9, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    client = BrokenClient(entity, [SimpleNamespace(sender=None, get_sender=sender)])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(ops.ScrapeRequest(account_id="account", target="@hidden_members"))
    assert exc.value.status_code == 500
    assert "private-test-value" not in exc.value.detail


@pytest.mark.asyncio
async def test_non_admin_sender_lookup_reports_a_revoked_session(monkeypatch):
    entity = types.Channel(id=9, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    client = NonAdminClient(entity, [SimpleNamespace(
        sender=None, get_sender=AsyncMock(side_effect=errors.SessionRevokedError(request=None)))])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(ops.ScrapeRequest(account_id="account", target="@hidden_members"))
    assert exc.value.status_code == 400
    assert "SessionRevokedError" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("flood_wait", [False, True])
async def test_non_admin_scrape_cannot_silently_skip_admin_exclusion(monkeypatch, flood_wait):
    class AdminLookupDenied(NonAdminClient):
        async def __call__(self, request):
            if isinstance(request, functions.channels.GetParticipantsRequest):
                if flood_wait:
                    raise errors.FloodWaitError(request=None, capture=73)
                raise errors.ChatAdminRequiredError(request=None)
            return await super().__call__(request)

    entity = types.Channel(id=9, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    client = AdminLookupDenied(entity, [SimpleNamespace(sender=types.User(id=11, username="admin"))])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(ops.ScrapeRequest(
            account_id="account", target="@hidden_members", admin_filter="exclude"))
    assert exc.value.status_code == (429 if flood_wait else 400)
    assert ("73s" if flood_wait else "Include admins") in exc.value.detail


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


@pytest.mark.asyncio
async def test_zero_member_limit_asks_telegram_for_every_visible_member(monkeypatch):
    entity = SimpleNamespace()
    client = fake_client(entity=entity, participants=[types.User(id=1, first_name="Ana")])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    result = await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="@everyone", limit=0))

    assert result["success"] is True
    client.get_participants.assert_awaited_once_with(entity, limit=None)


@pytest.mark.asyncio
async def test_large_member_limit_raises_the_recent_poster_scan_ceiling(monkeypatch):
    entity = types.Channel(id=9, title="Hidden", photo=types.ChatPhotoEmpty(), date=None)
    client = NonAdminClient(entity, [SimpleNamespace(sender=types.User(id=11, first_name="Ana"))])
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))

    await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="@hidden_members", limit=50000))

    assert client.history_limit == ops.RECENT_POSTER_SCAN_MAX
    assert ops.RECENT_POSTER_SCAN_MAX > 1000

