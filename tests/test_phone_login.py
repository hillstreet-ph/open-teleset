"""Synthetic phone authentication, including Telethon's real zero-retry path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, functions, types
from telethon.sessions import StringSession

import account_manager as module
from outreach_client import ConsentTelegramClient


@pytest.fixture
def manager():
    instance = module.AccountManager.__new__(module.AccountManager)
    instance.accounts = {}
    instance.phone_sessions = {}
    instance._save_config = Mock()
    return instance


def sent_code():
    return types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="fixture-hash",
    )


def install_client(monkeypatch, replies):
    client = ConsentTelegramClient(StringSession(), 12345, "fixture-api-hash")
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)
    requests = []

    def send(request, ordered=False):
        requests.append((request, client.session.dc_id))
        future = asyncio.get_running_loop().create_future()
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            future.set_exception(reply)
        else:
            future.set_result(reply)
        return future

    async def switch_dc(dc):
        client.session.set_dc(dc, "127.0.0.1", 443)

    client._sender = SimpleNamespace(send=send)
    client._switch_dc = AsyncMock(side_effect=switch_dc)
    monkeypatch.setattr(module, "TelegramClient", lambda **kwargs: client)
    return client, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("migration", [errors.PhoneMigrateError, errors.NetworkMigrateError])
async def test_authorization_redirect_repeats_once_on_the_migrated_dc(manager, monkeypatch, migration):
    client, requests = install_client(monkeypatch, [migration(None, capture=5), sent_code()])
    result = await manager.send_phone_code("fixture", "15550001111")
    assert result["success"]
    assert result["has_2fa"] is False
    assert len(requests) == 2
    assert all(isinstance(r, functions.auth.SendCodeRequest) for r, _ in requests)
    assert requests[1][1] == 5
    client._switch_dc.assert_awaited_once_with(5)
    assert client._request_retries == 0 and client.flood_sleep_threshold == 0
    assert manager.phone_sessions["fixture"]["phone_code_hash"] == "fixture-hash"
    client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_migration_is_bounded_and_discards_the_temporary_client(manager, monkeypatch):
    client, requests = install_client(monkeypatch, [
        errors.PhoneMigrateError(None, capture=5), errors.PhoneMigrateError(None, capture=4),
    ])
    result = await manager.send_phone_code("fixture", "+15550001111")
    assert not result["success"] and "redirected login again" in result["error"]
    assert len(requests) == 2 and not manager.phone_sessions
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [errors.FloodWaitError(None, capture=60), RuntimeError("network error")])
async def test_other_failures_never_retry_or_retain_a_client(manager, monkeypatch, failure):
    client, requests = install_client(monkeypatch, [failure])
    result = await manager.send_phone_code("fixture", "+15550001111")
    assert not result["success"]
    assert len(requests) == 1 and not manager.phone_sessions
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_timeout_disconnects_without_sending_a_code(manager, monkeypatch):
    client, requests = install_client(monkeypatch, [])
    client.connect.side_effect = asyncio.TimeoutError
    result = await manager.send_phone_code("fixture", "+15550001111")
    assert not result["success"] and "timed out" in result["error"]
    assert requests == []
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_login_cannot_be_overwritten(manager, monkeypatch):
    existing = {"client": object(), "status": "code_sent"}
    manager.phone_sessions["fixture"] = existing
    constructor = Mock()
    monkeypatch.setattr(module, "TelegramClient", constructor)
    assert not (await manager.send_phone_code("fixture", "+15550001111"))["success"]
    assert manager.phone_sessions["fixture"] is existing
    constructor.assert_not_called()


def verification_session(manager, sign_in_error=None):
    client = SimpleNamespace(
        sign_in=AsyncMock(side_effect=sign_in_error), disconnect=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(
            phone="15550001111", username="fixture", id=1, first_name="Fixture", last_name="",
        )), session=SimpleNamespace(save=lambda: "fixture-session"),
    )
    manager.phone_sessions["fixture"] = {
        "client": client, "phone": "+15550001111", "phone_code_hash": "fixture-hash",
        "status": "code_sent", "has_2fa": False,
    }
    return client


@pytest.mark.asyncio
async def test_code_then_password_uses_public_telethon_authentication(manager):
    client = verification_session(manager, errors.SessionPasswordNeededError(None))
    result = await manager.verify_phone_code("fixture", "12345")
    assert result["needs_2fa"] and not result["success"]
    assert manager.phone_sessions["fixture"]["has_2fa"]
    client.sign_in.assert_awaited_once_with(
        phone="+15550001111", code="12345", phone_code_hash="fixture-hash",
    )
    client.sign_in.side_effect = None
    assert (await manager.submit_2fa_for_phone("fixture", "fixture-password"))["success"]
    client.sign_in.assert_awaited_with(password="fixture-password")
    assert "fixture" in manager.accounts and not manager.phone_sessions
    manager._save_config.assert_called_once()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_password_keeps_two_step_session_available(manager):
    client = verification_session(manager, errors.PasswordHashInvalidError(None))
    manager.phone_sessions["fixture"]["status"] = "need_2fa"
    result = await manager.submit_2fa_for_phone("fixture", "fixture-password")
    assert not result["success"] and "Incorrect" in result["error"]
    assert manager.phone_sessions["fixture"]["status"] == "need_2fa"
    assert not manager.accounts
    client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [errors.PhoneCodeInvalidError(None), errors.PhoneCodeExpiredError(None)])
async def test_code_errors_keep_the_session_without_saving_an_account(manager, failure):
    verification_session(manager, failure)
    result = await manager.verify_phone_code("fixture", "12345")
    assert not result["success"] and not result["needs_2fa"]
    assert "fixture" in manager.phone_sessions and not manager.accounts
    manager._save_config.assert_not_called()


@pytest.mark.asyncio
async def test_failed_persistence_does_not_report_a_successful_login(manager):
    client = verification_session(manager)
    manager._save_config.side_effect = OSError("fixture storage failure")
    result = await manager.verify_phone_code("fixture", "12345")
    assert not result["success"]
    assert not manager.accounts and not manager.phone_sessions
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence_failure", [False, True])
async def test_disconnect_failure_preserves_the_persistence_result(manager, persistence_failure):
    client = verification_session(manager)
    client.disconnect.side_effect = RuntimeError("fixture disconnect failure")
    if persistence_failure:
        manager._save_config.side_effect = OSError("fixture storage failure")
    result = await manager.verify_phone_code("fixture", "12345")
    assert result["success"] is not persistence_failure
    assert bool(manager.accounts) is not persistence_failure
    assert not manager.phone_sessions
    if persistence_failure:
        assert result["error"] == "fixture storage failure"


@pytest.mark.asyncio
async def test_send_failure_is_not_replaced_by_disconnect_failure(manager, monkeypatch):
    client, requests = install_client(monkeypatch, [errors.FloodWaitError(None, capture=60)])
    client.disconnect.side_effect = RuntimeError("fixture disconnect failure")
    result = await manager.send_phone_code("fixture", "+15550001111")
    assert result == {"success": False, "error": "Too many requests; try again after 60 seconds"}
    assert len(requests) == 1 and not manager.phone_sessions
