"""Saved-session reconnects exercise real Telethon RPC error handling, offline."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, functions
from telethon.sessions import StringSession
from telethon.crypto import AuthKey

import account_manager as module
from outreach_client import ConsentTelegramClient


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'ACCOUNTS_DIR', str(tmp_path))
    monkeypatch.setattr(module, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    result = module.AccountManager()
    result.accounts['saved'] = {'session_string': '', 'user_id': 1}
    result._save_config = Mock()
    return result


def client_with_replies(monkeypatch, replies):
    session = StringSession()
    session.set_dc(2, '149.154.167.51', 443)
    session.auth_key = AuthKey(bytes(range(256)))
    client = ConsentTelegramClient(session, 12345, 'fixture-hash')
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = Mock(return_value=True)
    requests = []

    def send(request, ordered=False):
        requests.append(request)
        future = asyncio.get_running_loop().create_future()
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            future.set_exception(reply)
        else:
            future.set_result(reply)
        return future

    async def switch(dc):
        client.session.set_dc(dc, '149.154.167.52', 443)

    client._sender = SimpleNamespace(send=send)
    client._switch_dc = AsyncMock(side_effect=switch)
    monkeypatch.setattr(module, 'TelegramClient', lambda *args, **kwargs: client)
    return client, requests


@pytest.mark.asyncio
async def test_migration_reprobes_once_and_persists_changed_session(manager, monkeypatch):
    client, requests = client_with_replies(monkeypatch, [errors.UserMigrateError(None, capture=5), True])
    before = client.session.save()
    assert await manager.get_client('saved') is client
    assert len(requests) == 2
    assert all(isinstance(r, functions.updates.GetStateRequest) for r in requests)
    client._switch_dc.assert_awaited_once_with(5)
    assert manager.accounts['saved']['session_string'] == client.session.save() != before
    assert manager.accounts['saved']['last_online'].endswith('+00:00')
    assert client._request_retries == 0 and client.flood_sleep_threshold == 0
    client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,message', [
    (errors.AuthKeyUnregisteredError(None), 'expired or revoked'),
    (errors.FloodWaitError(None, capture=60), 'wait of 60 seconds'),
    (errors.ServerError(None, 'synthetic', 500), 'temporarily unavailable'),
    (TimeoutError(), 'timed out'),
    (RuntimeError('secret-proxy-password'), 'connection failed'),
])
async def test_errors_are_distinct_cleaned_up_and_never_retried(manager, monkeypatch, failure, message):
    client, requests = client_with_replies(monkeypatch, [failure])
    assert await manager.get_client('saved') is None
    assert len(requests) == 1
    assert message in manager.connection_errors['saved']
    assert 'secret-proxy-password' not in str(manager.list_accounts())
    assert not manager.clients
    client.disconnect.assert_awaited_once()
    manager._save_config.assert_not_called()


@pytest.mark.asyncio
async def test_second_redirect_stops_and_cleans_up(manager, monkeypatch):
    client, requests = client_with_replies(monkeypatch, [errors.UserMigrateError(None, capture=5), errors.UserMigrateError(None, capture=4)])
    assert await manager.get_client('saved') is None
    assert len(requests) == 2 and 'redirected' in manager.connection_errors['saved']
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_connection(manager, monkeypatch):
    client, requests = client_with_replies(monkeypatch, [True])
    results = await asyncio.gather(manager.get_client('saved'), manager.get_client('saved'))
    assert results == [client, client]
    assert len(requests) == 1
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_connection_is_disconnected(manager, monkeypatch):
    client, _ = client_with_replies(monkeypatch, [])
    client.connect.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await manager.get_client('saved')
    assert not manager.clients
    client.disconnect.assert_awaited_once()

@pytest.mark.asyncio
async def test_qr_persistence_failure_does_not_report_success(manager):
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(
        phone='15550001111', username='fixture', id=1, first_name='Fixture', last_name='')),
        session=SimpleNamespace(save=lambda: 'synthetic'), disconnect=AsyncMock())
    manager._save_config.side_effect = OSError('disk unavailable')
    state = {}
    with pytest.raises(OSError):
        await manager._complete_login('new', client, state)
    assert 'new' not in manager.accounts and state['status'] == 'failed'
    client.disconnect.assert_awaited_once()

@pytest.mark.asyncio
async def test_invalid_saved_session_returns_safe_diagnostic(manager):
    manager.accounts['saved']['session_string'] = 'invalid-sensitive-value'
    assert await manager.get_client('saved') is None
    assert 'invalid-sensitive-value' not in manager.connection_errors['saved']
    assert not manager.clients


@pytest.mark.asyncio
async def test_successful_health_check_clears_old_connection_error(manager, monkeypatch):
    import health_monitor as health
    import proxy_manager
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='fixture', phone='15550001111')))
    manager.get_client = AsyncMock(return_value=client)
    manager.connection_errors['saved'] = 'Old network error'
    monkeypatch.setattr(health, 'account_manager', manager)
    monkeypatch.setattr(proxy_manager.proxy_manager, 'get_proxy_for_account', lambda _: None)
    monitor = health.HealthMonitor.__new__(health.HealthMonitor)
    monitor.health_data = {}
    monitor.record_login_success = Mock()
    assert (await monitor.check_account_health('saved'))['status'] == 'healthy'
    assert 'saved' not in manager.connection_errors
