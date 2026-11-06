"""Re-login replaces the authorization of a saved account, never its identity.

A logged-out account keeps its ID, usage history and proxy. Signing in again
runs through a temporary login id and the fresh authorization is adopted only
when Telegram reports the same user id as the saved account.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import account_manager as module
import dashboard


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_RATE_DB", str(tmp_path / "rate.sqlite"))
    monkeypatch.setattr(module, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "CONFIG_FILE", str(tmp_path / "config.json"))
    instance = module.AccountManager()
    instance._save_config = Mock()
    return instance


def saved_account(**overrides):
    account = {
        "account_id": "account_1792075145144",
        "session_string": "old-session",
        "phone": "918207275001",
        "username": "old_name",
        "user_id": 1792075144,
        "first_name": "Old",
        "last_name": "Name",
        "is_premium": False,
        "status": "offline",
        "created_at": "2026-10-01T00:00:00+00:00",
        "last_online": "2026-10-19T00:00:00+00:00",
        "use_count": 895,
        "proxy_settings": {"mode": "selected", "proxy_id": "proxy-1"},
    }
    account.update(overrides)
    return account


def login_client(user_id=1792075144, session_string="fresh-session"):
    return SimpleNamespace(
        sign_in=AsyncMock(return_value=None),
        is_user_authorized=AsyncMock(return_value=True),
        is_connected=Mock(return_value=True),
        connect=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(
            phone="918207275001", username="new_name", id=user_id,
            first_name="New", last_name="Handle", premium=True,
        )),
        session=SimpleNamespace(save=lambda: session_string),
        disconnect=AsyncMock(),
    )


def qr_session(manager, client, replace_account_id):
    manager.qr_sessions["temporary-login"] = {
        "client": client,
        "qr_login": None,
        "status": "need_password",
        "created_at": datetime.now(),
        "proxy_settings": {"mode": "default"},
        "replace_account_id": replace_account_id,
    }


@pytest.mark.asyncio
async def test_same_user_adopts_the_fresh_authorization_and_keeps_the_account(manager):
    manager.accounts["account_1792075145144"] = saved_account()
    stale = SimpleNamespace(disconnect=AsyncMock())
    manager.clients["account_1792075145144"] = stale
    manager.connection_errors["account_1792075145144"] = "Telegram requires a wait of 26651 seconds."
    qr_session(manager, login_client(), "account_1792075145144")

    result = await manager.submit_2fa_password("temporary-login", "pw")

    assert result == {"success": True}
    assert list(manager.accounts) == ["account_1792075145144"]
    account = manager.accounts["account_1792075145144"]
    assert account["session_string"] == "fresh-session"
    assert account["status"] == "online"
    assert account["use_count"] == 895
    assert account["proxy_settings"] == {"mode": "selected", "proxy_id": "proxy-1"}
    assert account["username"] == "new_name" and account["is_premium"] is True
    assert "account_1792075145144" not in manager.connection_errors
    assert "account_1792075145144" not in manager.clients
    stale.disconnect.assert_awaited_once()
    manager._save_config.assert_called_once()
    assert manager.qr_sessions["temporary-login"]["status"] == "success"


@pytest.mark.asyncio
async def test_a_different_telegram_user_is_refused_and_nothing_changes(manager):
    manager.accounts["account_1792075145144"] = saved_account()
    qr_session(manager, login_client(user_id=555), "account_1792075145144")

    result = await manager.submit_2fa_password("temporary-login", "pw")

    assert result["success"] is False
    assert "not the one being re-signed in" in result["error"]
    assert manager.accounts["account_1792075145144"]["session_string"] == "old-session"
    manager._save_config.assert_not_called()


@pytest.mark.asyncio
async def test_a_deleted_target_asks_for_a_fresh_account_list(manager):
    qr_session(manager, login_client(), "missing-account")

    result = await manager.submit_2fa_password("temporary-login", "pw")

    assert result["success"] is False
    assert "no longer exists" in result["error"]
    assert not manager.accounts


@pytest.mark.asyncio
async def test_phone_code_login_adopts_the_saved_account(manager):
    manager.accounts["account_1792075145144"] = saved_account()
    client = login_client()
    manager.phone_sessions["temporary-login"] = {
        "client": client,
        "phone": "+918207275001",
        "phone_code_hash": "hash",
        "status": "need_2fa",
        "has_2fa": True,
        "created_at": datetime.now(),
        "replace_account_id": "account_1792075145144",
    }

    result = await manager.submit_2fa_for_phone("temporary-login", "pw")

    assert result == {"success": True}
    assert list(manager.accounts) == ["account_1792075145144"]
    assert manager.accounts["account_1792075145144"]["session_string"] == "fresh-session"
    assert "temporary-login" not in manager.phone_sessions


@pytest.mark.asyncio
async def test_phone_code_login_reports_a_mismatched_user(manager):
    manager.accounts["account_1792075145144"] = saved_account()
    manager.phone_sessions["temporary-login"] = {
        "client": login_client(user_id=555),
        "phone": "+918207275001",
        "phone_code_hash": "hash",
        "status": "need_2fa",
        "has_2fa": True,
        "created_at": datetime.now(),
        "replace_account_id": "account_1792075145144",
    }

    result = await manager.submit_2fa_for_phone("temporary-login", "pw")

    assert result["success"] is False
    assert "not the one being re-signed in" in result["error"]
    assert manager.accounts["account_1792075145144"]["session_string"] == "old-session"


@pytest.mark.asyncio
async def test_starting_a_relogin_for_a_deleted_account_fails_before_connecting(manager, monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(module, "TelegramClient", constructor)

    qr = await manager.generate_qr_code("temporary-login", replace_account_id="missing-account")
    phone = await manager.send_phone_code("temporary-login", "+918207275001",
                                          replace_account_id="missing-account")

    assert qr["success"] is False and "no longer exists" in qr["error"]
    assert phone["success"] is False and "no longer exists" in phone["error"]
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_generate_qr_route_rejects_a_deleted_target(monkeypatch):
    monkeypatch.setattr(dashboard, "account_manager", SimpleNamespace(accounts={}))
    with pytest.raises(HTTPException) as excinfo:
        await dashboard.generate_qr_code({"account_id": "temporary-login",
                                          "replace_account_id": "missing-account"})
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_send_code_route_rejects_a_deleted_target(monkeypatch):
    monkeypatch.setattr(dashboard, "account_manager", SimpleNamespace(accounts={}))
    with pytest.raises(HTTPException) as excinfo:
        await dashboard.send_phone_code({"account_id": "temporary-login",
                                         "phone": "+918207275001",
                                         "replace_account_id": "missing-account"})
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_login_routes_pass_the_replacement_account_through(monkeypatch):
    seen = {}

    async def fake_qr(account_id, proxy, proxy_settings=None, replace_account_id=None):
        seen["qr"] = (account_id, replace_account_id)
        return {"success": True}

    async def fake_phone(account_id, phone, proxy, proxy_settings=None, replace_account_id=None):
        seen["phone"] = (account_id, replace_account_id)
        return {"success": True}

    monkeypatch.setattr(dashboard, "account_manager", SimpleNamespace(
        accounts={"account_1792075145144": saved_account()},
        generate_qr_code=fake_qr,
        send_phone_code=fake_phone,
    ))
    monkeypatch.setattr(dashboard, "login_connection", lambda request, account_id: (None, {"mode": "default"}))
    monkeypatch.setattr(dashboard, "stats_tracker", SimpleNamespace(record_use=lambda account_id: None))
    monkeypatch.setattr(dashboard, "log_manager", SimpleNamespace(add_log=lambda *args, **kwargs: None))

    await dashboard.generate_qr_code({"account_id": "temporary-login",
                                      "replace_account_id": "account_1792075145144"})
    await dashboard.send_phone_code({"account_id": "temporary-login", "phone": "+918207275001",
                                     "replace_account_id": "account_1792075145144"})

    assert seen["qr"] == ("temporary-login", "account_1792075145144")
    assert seen["phone"] == ("temporary-login", "account_1792075145144")
