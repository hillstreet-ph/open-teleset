"""No live Telegram requests: connection choice, cancellation and cleanup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import account_manager as am
import proxy_manager as pm


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(am, 'CONFIG_FILE', str(tmp_path / 'accounts.json'))
    monkeypatch.setattr(am, 'ACCOUNTS_DIR', str(tmp_path))
    instance = am.AccountManager()
    instance._save_config = Mock()
    return instance


@pytest.fixture
def proxies(monkeypatch):
    instance = pm.ProxyManager.__new__(pm.ProxyManager)
    instance.proxies = {'one': {'protocol': 'socks5', 'host': '127.0.0.1', 'port': 1080, 'assigned_to': ['a']}}
    instance.global_proxy = {'protocol': 'http', 'host': '127.0.0.2', 'port': 8080}
    monkeypatch.setattr(pm, 'proxy_manager', instance)
    return instance


def client():
    return SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='synthetic')),
        qr_login=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=1, phone='15550000000', username='fixture', first_name='', last_name='')),
        session=SimpleNamespace(save=lambda: 'synthetic'))


def test_explicit_proxy_modes_and_legacy_assignment(manager, proxies):
    manager.accounts['a'] = {}
    assert manager.saved_proxy('a') == proxies.get_proxy('one')
    for mode, expected in [('direct', None), ('default', proxies.get_global_proxy()), ('selected', proxies.get_proxy('one'))]:
        manager.accounts['a']['proxy_settings'] = {'mode': mode, 'proxy_id': 'one'}
        assert manager.saved_proxy('a') == expected
    del proxies.proxies['one']
    with pytest.raises(ValueError, match='unavailable'):
        manager.saved_proxy('a')


@pytest.mark.asyncio
async def test_connection_change_persists_and_releases_cached_client(manager):
    manager.accounts['a'] = {'proxy_settings': {'mode': 'direct'}}
    old = client(); manager.clients['a'] = old
    await manager.set_account_connection('a', {'mode': 'selected', 'proxy_id': 'one'})
    assert manager.accounts['a']['proxy_settings']['proxy_id'] == 'one'
    manager._save_config.assert_called_once()
    old.disconnect.assert_awaited_once()
    assert 'a' not in manager.clients


@pytest.mark.asyncio
async def test_connection_save_failure_preserves_route_and_client(manager):
    manager.accounts['a'] = {'proxy_settings': {'mode': 'direct'}}
    old = client(); manager.clients['a'] = old
    manager._save_config.side_effect = OSError('synthetic')
    with pytest.raises(OSError):
        await manager.set_account_connection('a', {'mode': 'default'})
    assert manager.accounts['a']['proxy_settings'] == {'mode': 'direct'}
    old.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['phone', 'qr'])
async def test_pending_login_is_reserved_and_cancelled_before_rpc(manager, monkeypatch, kind):
    fake = client(); entered = asyncio.Event(); release = asyncio.Event()
    async def connect():
        entered.set(); await release.wait()
    fake.connect.side_effect = connect
    factory = Mock(return_value=fake); monkeypatch.setattr(am, 'TelegramClient', factory)
    async def start():
        return await manager.send_phone_code('a', '+15550000000') if kind == 'phone' else await manager.generate_qr_code('a')
    task = asyncio.create_task(start()); await entered.wait()
    assert not (await start())['success']
    factory.assert_called_once()
    await getattr(manager, f'cancel_{kind}_login')('a')
    release.set()
    assert not (await task)['success']
    fake.send_code_request.assert_not_awaited(); fake.qr_login.assert_not_awaited()
    assert not getattr(manager, f'{kind}_sessions') and not manager.accounts


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['phone', 'qr'])
async def test_old_completion_cannot_save_or_remove_replacement(manager, kind):
    fake = client(); entered = asyncio.Event(); release = asyncio.Event()
    identity = fake.get_me.return_value
    async def get_me():
        entered.set(); await release.wait(); return identity
    fake.get_me.side_effect = get_me
    sessions = getattr(manager, f'{kind}_sessions')
    old = {'client': fake}; sessions['a'] = old
    method = manager._complete_phone_login if kind == 'phone' else manager._complete_login
    task = asyncio.create_task(method('a', fake, old)); await entered.wait()
    await getattr(manager, f'cancel_{kind}_login')('a')
    replacement = {'status': 'connecting'}; sessions['a'] = replacement
    release.set()
    if kind == 'phone':
        with pytest.raises(ValueError, match='cancelled'): await task
    else: assert await task is False
    assert sessions['a'] is replacement and not manager.accounts
    manager._save_config.assert_not_called()


@pytest.mark.asyncio
async def test_successful_login_keeps_proxy_choice_and_bounds_disconnect(manager, monkeypatch):
    fake = client()
    fake.disconnect.side_effect = lambda: None
    async def hang(): await asyncio.Event().wait()
    fake.disconnect.side_effect = hang
    monkeypatch.setattr(am, 'LOGIN_DISCONNECT_TIMEOUT', .01)
    session = {'client': fake, 'proxy_settings': {'mode': 'direct'}}
    manager.phone_sessions['a'] = session
    await asyncio.wait_for(manager._complete_phone_login('a', fake, session), .2)
    assert manager.accounts['a']['proxy_settings'] == {'mode': 'direct'}
    assert not manager.phone_sessions


@pytest.mark.asyncio
async def test_phone_rpc_timeout_cleans_pending_session_and_sanitizes_errors(manager, monkeypatch):
    fake = client()
    async def hang(*args): await asyncio.Event().wait()
    fake.send_code_request.side_effect = hang
    monkeypatch.setattr(am, 'LOGIN_RPC_TIMEOUT', .01)
    monkeypatch.setattr(am, 'TelegramClient', lambda **kwargs: fake)
    result = await asyncio.wait_for(manager.send_phone_code('a', '+15550000000'), .2)
    assert not result['success'] and 'timed out' in result['error']
    assert not manager.phone_sessions
    fake.disconnect.assert_awaited_once()


@pytest.mark.parametrize('protocol,host,port', [('https', 'host', 80), ('socks5', '', 80), ('socks5', 'host', 0), ('http', 'host', 65536), ('socks5', 'host', True)])
def test_invalid_proxy_is_rejected_before_persistence(proxies, protocol, host, port):
    proxies._save_proxies = Mock()
    assert not proxies.add_proxy('bad', protocol, host, port)
    assert not proxies.set_global_proxy(protocol, host, port)
    proxies._save_proxies.assert_not_called()
