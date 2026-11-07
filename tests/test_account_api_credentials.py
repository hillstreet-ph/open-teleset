"""Per-account Telegram app credentials. Synthetic keys only; never connects."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from telethon.sessions import StringSession

import account_manager as module
from hex_session import hex_to_string_session

KEY = bytes(range(256)).hex()
API_HASH = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(module, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(module, "API_ID", 999)
    monkeypatch.setattr(module, "API_HASH", "server-default-hash")
    return module.AccountManager(), tmp_path


def test_validate_accepts_pair_and_normalises():
    assert module.validate_api_credentials("12345", API_HASH.upper()) == (12345, API_HASH)
    assert module.validate_api_credentials(12345, API_HASH) == (12345, API_HASH)


@pytest.mark.parametrize("api_id,api_hash", [
    (None, None), ("", ""), (0, None), ("   ", ""),
])
def test_blank_pair_means_server_default(api_id, api_hash):
    assert module.validate_api_credentials(api_id, api_hash) == (None, None)


@pytest.mark.parametrize("api_id,api_hash,part", [
    ("12345", "", "both"),
    ("", API_HASH, "both"),
    ("abc", API_HASH, "whole number"),
    ("-5", API_HASH, "valid"),
    ("99999999999", API_HASH, "valid"),
    ("12345", "xyz", "32 hexadecimal"),
    ("12345", API_HASH + "00", "32 hexadecimal"),
])
def test_invalid_credentials_are_rejected_without_echoing_values(api_id, api_hash, part):
    with pytest.raises(ValueError) as exc:
        module.validate_api_credentials(api_id, api_hash)
    assert part in str(exc.value)
    assert API_HASH not in str(exc.value)


def test_account_credentials_fall_back_to_server_default(manager):
    mgr, _ = manager
    mgr.accounts = {
        "own": {"api_id": 4242, "api_hash": API_HASH},
        "plain": {"session_string": "s"},
    }
    assert mgr._api_credentials("own") == (4242, API_HASH)
    assert mgr._api_credentials("plain") == (999, "server-default-hash")
    assert mgr._api_credentials("missing") == (999, "server-default-hash")


@pytest.mark.asyncio
async def test_import_hex_with_custom_credentials_encrypts_and_reloads(manager):
    mgr, path = manager
    session_string = StringSession(hex_to_string_session(KEY, 5)).save()
    client = Mock()
    client.session = StringSession(session_string)
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=SimpleNamespace(
        id=7, phone="15550000000", username="fixture", first_name="Test",
        last_name="", bot=False, premium=False))
    factory = Mock(return_value=client)
    mgr._probe_saved_authorization = AsyncMock()
    module_telegram = module.TelegramClient
    module.TelegramClient = factory
    try:
        result = await mgr.import_hex_session("own", KEY, 5, api_id="4242", api_hash=API_HASH)
    finally:
        module.TelegramClient = module_telegram
    assert result == {"success": True, "account_id": "own"}
    args = factory.call_args.args
    assert args[1:] == (4242, API_HASH)
    raw = (path / "config.json").read_text()
    assert API_HASH not in raw and '"api_hash":' not in raw
    assert "api_hash_encrypted" in raw
    restored = module.AccountManager()
    assert restored.accounts["own"]["api_id"] == 4242
    assert restored.accounts["own"]["api_hash"] == API_HASH
    listed = restored.list_accounts()
    assert listed[0]["has_custom_api"] is True and listed[0]["api_id"] == 4242
    assert "api_hash" not in listed[0]


@pytest.mark.asyncio
async def test_import_hex_without_custom_credentials_uses_server_default(manager):
    mgr, path = manager
    client = Mock()
    client.session = StringSession(hex_to_string_session(KEY, 4))
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=SimpleNamespace(
        id=8, phone="15550000001", username="fixture2", first_name="Test",
        last_name="", bot=False, premium=False))
    factory = Mock(return_value=client)
    mgr._probe_saved_authorization = AsyncMock()
    original = module.TelegramClient
    module.TelegramClient = factory
    try:
        assert (await mgr.import_hex_session("plain", KEY, 4))["success"] is True
    finally:
        module.TelegramClient = original
    assert factory.call_args.args[1:] == (999, "server-default-hash")
    assert "api_hash_encrypted" not in json.loads((path / "config.json").read_text())["plain"]


@pytest.mark.asyncio
async def test_phone_code_uses_custom_credentials_and_keeps_them_for_2fa(manager):
    mgr, _ = manager
    client = Mock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="h"))
    factory = Mock(return_value=client)
    original = module.TelegramClient
    module.TelegramClient = factory
    try:
        result = await mgr.send_phone_code("phone", "+15550000002", api_id="4242", api_hash=API_HASH)
    finally:
        module.TelegramClient = original
    assert result["success"] is True
    kwargs = factory.call_args.kwargs
    assert (kwargs["api_id"], kwargs["api_hash"]) == (4242, API_HASH)
    pending = mgr.phone_sessions["phone"]
    assert (pending["api_id"], pending["api_hash"]) == (4242, API_HASH)


@pytest.mark.asyncio
async def test_phone_login_saves_custom_credentials(manager):
    mgr, path = manager
    session_string = StringSession(hex_to_string_session(KEY, 2)).save()
    client = Mock()
    client.session = StringSession(session_string)
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=SimpleNamespace(
        id=9, phone="15550000003", username="fixture3", first_name="Test",
        last_name="", bot=False, premium=False))
    state = {"status": "code_sent", "api_id": 4242, "api_hash": API_HASH,
             "proxy_settings": {"mode": "direct"}}
    mgr.phone_sessions["phone"] = state
    await mgr._complete_phone_login("phone", client, state)
    assert state["status"] == "success"
    assert mgr.accounts["phone"]["api_id"] == 4242
    assert mgr.accounts["phone"]["api_hash"] == API_HASH
    raw = (path / "config.json").read_text()
    assert API_HASH not in raw
    assert module.AccountManager().accounts["phone"]["api_hash"] == API_HASH


def test_export_hex_session_roundtrip(manager):
    mgr, _ = manager
    session_string = StringSession(hex_to_string_session(KEY, 3)).save()
    mgr.accounts = {"backup": {"session_string": session_string, "api_id": 4242}}
    exported = mgr.export_hex_session("backup")
    assert exported == {"hex_key": KEY, "dc_id": 3, "api_id": 4242}
    assert mgr.export_hex_session("missing") is None
    mgr.accounts["empty"] = {}
    assert mgr.export_hex_session("empty") is None
