"""Exercise real Telethon bootstrap and cancellation without Telegram traffic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, functions
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

import account_manager as module
from outreach_client import ConsentTelegramClient


@pytest.fixture
def manager():
    result = module.AccountManager.__new__(module.AccountManager)
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    result.accounts = {"saved": {"session_string": session.save(), "user_id": 1}}
    result.clients = {}
    result._client_tasks = {}
    result.connection_errors = {}
    result._save_config = Mock()
    return result


@pytest.mark.asyncio
async def test_real_connect_bootstrap_redirect_preserves_saved_key(manager, monkeypatch):
    """Do not mock connect/get_me/_switch_dc: redirects can occur inside connect."""
    requests = []
    clients = []

    def factory(*args, **kwargs):
        client = ConsentTelegramClient(*args, **kwargs)

        def send(request, ordered=False):
            requests.append(type(request).__name__)
            future = asyncio.get_running_loop().create_future()
            if isinstance(request, functions.InvokeWithLayerRequest):
                future.set_result(True)
            else:
                future.set_exception(errors.UserMigrateError(request, capture=5))
            return future

        # All connection and RPC logic remains real; only the network sender is fake.
        client._sender = SimpleNamespace(
            auth_key=client.session.auth_key,
            connect=AsyncMock(return_value=True),
            disconnect=AsyncMock(),
            send=send,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(module, "TelegramClient", factory)
    monkeypatch.setattr(module, "API_ID", 12345)
    monkeypatch.setattr(module, "API_HASH", "fixture-hash")
    original = manager.accounts["saved"]["session_string"]

    assert await manager.get_client("saved", proxy={}) is None

    assert requests == ["InvokeWithLayerRequest", "GetUsersRequest"]
    client = clients[0]
    assert client.session.dc_id == 2
    assert client.session.save() == original
    assert manager.accounts["saved"]["session_string"] == original
    assert "different data center" in manager.connection_errors["saved"]
    assert not manager.clients
    manager._save_config.assert_not_called()
    client._sender.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_abort_shared_failed_probe(manager, monkeypatch):
    started = asyncio.Event()
    proceed = asyncio.Event()
    requests = []

    async def connect():
        started.set()
        await proceed.wait()

    def send(request, ordered=False):
        requests.append(request)
        future = asyncio.get_running_loop().create_future()
        future.set_exception(errors.AuthKeyUnregisteredError(request))
        return future

    client = ConsentTelegramClient(
        StringSession(manager.accounts["saved"]["session_string"]),
        12345, "fixture-hash", preserve_saved_authorization=True,
    )
    client.connect = AsyncMock(side_effect=connect)
    client.disconnect = AsyncMock()
    client._sender = SimpleNamespace(send=send)
    monkeypatch.setattr(module, "TelegramClient", lambda *args, **kwargs: client)
    first = asyncio.create_task(manager.get_client("saved", proxy={}))
    second = asyncio.create_task(manager.get_client("saved", proxy={}))
    await asyncio.wait_for(started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    client.disconnect.assert_not_awaited()

    proceed.set()
    assert await asyncio.wait_for(second, timeout=1) is None
    await asyncio.sleep(0)
    assert len(requests) == 1
    client.connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    assert not manager.clients
    assert not manager._client_tasks
    manager._save_config.assert_not_called()
