"""Synthetic authorization keys only; never connects to Telegram."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from telethon import errors
from telethon.sessions import StringSession

import account_manager as module
from hex_session import PRODUCTION_DCS, hex_to_string_session

KEY = bytes(range(256)).hex()


@pytest.mark.parametrize('dc', range(1, 6))
def test_hex_roundtrip(dc):
    session = StringSession(hex_to_string_session('\n'.join(KEY.upper()[i:i+32] for i in range(0, 512, 32)), dc))
    assert session.auth_key.key == bytes(range(256))
    assert (session.dc_id, session.server_address, session.port) == (dc, PRODUCTION_DCS[dc], 443)


@pytest.mark.parametrize('key,dc', [(KEY, x) for x in [None, True, '5', 0, 6, 1.0]] + [(x, 5) for x in [None, [], '', 'a'*511, 'a'*513, 'g'*512, '0'*512, 'Hex Session: '+KEY, ' '*4097]])
def test_invalid_input_never_echoes_key(key, dc):
    with pytest.raises(ValueError) as exc:
        hex_to_string_session(key, dc)
    assert KEY not in str(exc.value)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(module, 'ACCOUNTS_DIR', str(tmp_path))
    monkeypatch.setattr(module, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    manager = module.AccountManager()
    session = StringSession(hex_to_string_session(KEY, 5))
    client = Mock()
    client.session = session
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=SimpleNamespace(id=7, phone='15550000000', username='fixture', first_name='Test', last_name='', bot=False, premium=False))
    probe = AsyncMock()
    manager._probe_saved_authorization = probe
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, 'TelegramClient', factory)
    return manager, client, factory, tmp_path, probe


@pytest.mark.asyncio
async def test_success_stores_encrypted_verified_identity(setup):
    manager, client, factory, path, probe = setup
    result = await manager.import_hex_session('new', KEY, 5)
    assert result == {'success': True, 'account_id': 'new'}
    factory.assert_called_once()
    assert factory.call_args.kwargs['preserve_saved_authorization'] is True
    probe.assert_awaited_once_with(client)
    client.disconnect.assert_awaited_once()
    stored = (path / 'config.json').read_text()
    assert KEY not in stored and client.session.save() not in stored
    assert 'session_string' not in json.loads(stored)['new']
    restored = module.AccountManager()
    assert restored.accounts['new']['session_string'] == client.session.save()
    assert restored.accounts['new']['user_id'] == 7


@pytest.mark.asyncio
async def test_disk_failure_rolls_back(setup):
    manager, client, _, _, _ = setup
    manager._save_config = Mock(side_effect=OSError(KEY))
    result = await manager.import_hex_session('new', KEY, 5)
    assert not result['success'] and 'save' in result['error']
    assert not manager.accounts and KEY not in str(result)
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error,part', [(errors.AuthKeyUnregisteredError(None), 'revoked'), (errors.UserMigrateError(None, capture=2), 'DC'), (TimeoutError(), 'timed out'), (RuntimeError(KEY), 'Unable to verify'), (errors.FloodWaitError(None, capture=60), '60 seconds')])
async def test_verification_failures_are_safe_and_cleaned(setup, error, part):
    manager, client, _, _, probe = setup
    probe.side_effect = error
    result = await manager.import_hex_session('new', KEY, 5)
    assert not result['success'] and part in result['error']
    assert KEY not in str(result) and not manager.accounts
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_cleanup(setup):
    manager, client, _, _, probe = setup
    probe.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await manager.import_hex_session('new', KEY, 5)
    client.disconnect.assert_awaited_once()
    assert not manager.accounts


@pytest.mark.asyncio
async def test_concurrent_duplicate_identity_saved_once(setup):
    manager, client, _, _, _ = setup
    results = await asyncio.gather(manager.import_hex_session('one', KEY, 5), manager.import_hex_session('two', KEY, 5))
    assert sum(x['success'] for x in results) == 1
    assert len(manager.accounts) == 1
    assert client.disconnect.await_count == 2


@pytest.mark.asyncio
async def test_existing_id_cannot_be_overwritten(setup):
    manager, _, factory, _, _ = setup
    manager.accounts['existing'] = {'user_id': 99}
    result = await manager.import_hex_session('existing', KEY, 5)
    assert not result['success']
    factory.assert_not_called()
    assert manager.accounts['existing'] == {'user_id': 99}


@pytest.mark.asyncio
async def test_invalid_key_never_connects(setup):
    manager, _, factory, _, _ = setup
    assert not (await manager.import_hex_session('new', 'not-a-key', 5))['success']
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [None, SimpleNamespace(bot=True)])
async def test_missing_or_bot_identity_is_not_saved(setup, identity):
    manager, client, _, path, _ = setup
    client.get_me.return_value = identity
    assert not (await manager.import_hex_session('new', KEY, 5))['success']
    assert not manager.accounts and not (path / 'config.json').exists()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_constructor_failure_does_not_leak_session(setup):
    manager, _, factory, _, _ = setup
    factory.side_effect = RuntimeError(KEY)
    result = await manager.import_hex_session('new', KEY, 5)
    assert not result['success'] and KEY not in str(result)
    assert not manager.accounts


@pytest.mark.asyncio
async def test_verification_timeout_cancels_probe_and_disconnects(setup, monkeypatch):
    manager, client, _, _, probe = setup
    real_wait_for = asyncio.wait_for
    cancelled = asyncio.Event()

    async def blocked(_client):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    probe.side_effect = blocked

    async def quick_wait(awaitable, timeout):
        return await real_wait_for(awaitable, 0.01 if timeout == 20 else timeout)

    monkeypatch.setattr(module.asyncio, 'wait_for', quick_wait)
    result = await manager.import_hex_session('new', KEY, 5)
    assert not result['success'] and 'timed out' in result['error']
    assert cancelled.is_set() and not manager.accounts
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['_complete_login', '_complete_phone_login'])
async def test_pending_interactive_login_cannot_overwrite_import(setup, method):
    manager, client, _, _, _ = setup
    original = {'user_id': 99, 'session_string': 'existing'}
    manager.accounts['same'] = original
    state = {}
    (manager.phone_sessions if method == '_complete_phone_login' else manager.qr_sessions)['same'] = state
    if method == '_complete_phone_login':
        with pytest.raises(ValueError, match='already exists'):
            await getattr(manager, method)('same', client, state)
    else:
        assert await getattr(manager, method)('same', client, state) is False
    assert manager.accounts['same'] is original
    assert state['status'] == 'failed'
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['_complete_login', '_complete_phone_login'])
async def test_interactive_verification_failure_cannot_remove_existing_account(setup, method):
    manager, client, _, _, _ = setup
    original = {'user_id': 99, 'session_string': 'existing'}
    manager.accounts['same'] = original
    client.get_me.side_effect = RuntimeError('verification failed')
    if method == '_complete_phone_login':
        with pytest.raises(RuntimeError):
            await getattr(manager, method)('same', client, {})
    else:
        assert await getattr(manager, method)('same', client, {}) is False
    assert manager.accounts['same'] is original
