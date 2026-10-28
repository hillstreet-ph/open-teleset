"""QR-login two-step verification must stay retryable.

Regression: a single slow/failed Telegram round-trip (common through the free
global proxy) marked the QR session "failed", so every later submit - even with
the correct password - was rejected with a Chinese "state does not need a
password" message and the user could never finish signing in.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors

import account_manager as module


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTREACH_RATE_DB', str(tmp_path / 'rate.sqlite'))
    monkeypatch.setattr(module, 'ACCOUNTS_DIR', str(tmp_path))
    monkeypatch.setattr(module, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    result = module.AccountManager()
    result._save_config = Mock()
    return result


def waiting_for_password(manager, sign_in, connected=True, user_id=42):
    client = SimpleNamespace(
        sign_in=sign_in,
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(
            phone='15550001111', username='fixture', id=user_id, first_name='F', last_name='')),
        session=SimpleNamespace(save=lambda: 'synthetic-session'),
        is_connected=Mock(return_value=connected),
        connect=AsyncMock(),
        disconnect=AsyncMock(),
    )
    manager.qr_sessions['acc'] = {'client': client, 'qr_login': None, 'status': 'need_password',
                                  'created_at': datetime.now(), 'proxy_settings': {'mode': 'default'}}
    return client


@pytest.mark.asyncio
async def test_correct_password_succeeds_after_a_transient_timeout(manager):
    sign_in = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    client = waiting_for_password(manager, sign_in)
    first = await manager.submit_2fa_password('acc', 'right-password')
    assert first['success'] is False and 'try again' in first['error'].lower()
    assert manager.qr_sessions['acc']['status'] == 'need_password'
    client.disconnect.assert_not_awaited()
    second = await manager.submit_2fa_password('acc', 'right-password')
    assert second == {'success': True}
    assert 'acc' in manager.accounts


@pytest.mark.asyncio
async def test_connection_error_keeps_the_session_retryable(manager):
    waiting_for_password(manager, AsyncMock(side_effect=[ConnectionError('proxy reset'), None]))
    assert (await manager.submit_2fa_password('acc', 'pw'))['success'] is False
    assert (await manager.submit_2fa_password('acc', 'pw'))['success'] is True


@pytest.mark.asyncio
async def test_wrong_password_is_reported_in_english_and_can_be_retried(manager):
    waiting_for_password(manager, AsyncMock(side_effect=[errors.PasswordHashInvalidError(None), None]))
    wrong = await manager.submit_2fa_password('acc', 'typo')
    assert wrong == {'success': False, 'error': 'Incorrect two-step verification password'}
    assert (await manager.submit_2fa_password('acc', 'right'))['success'] is True


@pytest.mark.asyncio
async def test_flood_wait_reports_the_wait_and_stays_retryable(manager):
    flood = errors.FloodWaitError(None, capture=37)
    waiting_for_password(manager, AsyncMock(side_effect=[flood]))
    result = await manager.submit_2fa_password('acc', 'pw')
    assert result['success'] is False and '37' in result['error']
    assert manager.qr_sessions['acc']['status'] == 'need_password'


@pytest.mark.asyncio
async def test_disconnected_client_is_reconnected_before_checking_the_password(manager):
    client = waiting_for_password(manager, AsyncMock(return_value=None), connected=False)
    assert (await manager.submit_2fa_password('acc', 'pw'))['success'] is True
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_or_finished_sessions_explain_how_to_recover(manager):
    missing = await manager.submit_2fa_password('nope', 'pw')
    assert missing['success'] is False and 'scan a new qr code' in missing['error'].lower()
    waiting_for_password(manager, AsyncMock())
    manager.qr_sessions['acc']['status'] = 'timeout'
    ended = await manager.submit_2fa_password('acc', 'pw')
    assert ended['success'] is False and 'scan a new qr code' in ended['error'].lower()
    for result in (missing, ended):
        assert not any('一' <= ch <= '鿿' for ch in result['error'])


@pytest.mark.asyncio
async def test_duplicate_account_reason_is_shown_instead_of_a_generic_save_error(manager):
    manager.accounts['existing'] = {'session_string': '', 'user_id': 42}
    waiting_for_password(manager, AsyncMock(return_value=None), user_id=42)
    result = await manager.submit_2fa_password('acc', 'pw')
    assert result['success'] is False and 'already' in result['error'].lower()


@pytest.mark.asyncio
async def test_password_is_never_echoed_in_errors(manager):
    waiting_for_password(manager, AsyncMock(side_effect=RuntimeError('boom secret-pw')))
    result = await manager.submit_2fa_password('acc', 'secret-pw')
    assert 'secret-pw' not in str(result)
