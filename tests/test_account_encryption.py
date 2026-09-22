import json
import os

import pytest
from cryptography.fernet import Fernet

import account_manager
from open_teleset.crypto import SessionCryptoError
from scripts.encrypt_account_config import convert


@pytest.fixture
def config(tmp_path, monkeypatch):
    from open_teleset.crypto.sessions import _fernet
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    _fernet.cache_clear()
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    monkeypatch.setattr(account_manager, "CONFIG_FILE", str(path))
    return path


def test_encrypted_atomic_write_and_reload(config):
    manager = account_manager.AccountManager()
    manager.accounts = {"test": {"session_string": "synthetic-test-session", "username": "test"}}
    manager._save_config()
    raw = config.read_text()
    assert "synthetic-test-session" not in raw and "session_string" not in raw
    assert os.stat(config).st_mode & 0o777 == 0o600
    restored = account_manager.AccountManager()
    assert restored.accounts["test"]["session_string"] == "synthetic-test-session"


def test_legacy_config_requires_explicit_conversion(config, tmp_path):
    original = json.dumps({"test": {"session_string": "synthetic-test-session"}})
    config.write_text(original)
    with pytest.raises(SessionCryptoError):
        account_manager.AccountManager()
    output = tmp_path / "converted.json"
    convert(config, output)
    assert config.read_text() == original
    assert "synthetic-test-session" not in output.read_text()
    with pytest.raises(FileExistsError):
        convert(config, output)


def test_failed_encryption_preserves_previous_file(config, monkeypatch):
    manager = account_manager.AccountManager()
    manager.accounts = {"test": {"session_string": "synthetic-test-session"}}
    manager._save_config()
    before = config.read_bytes()
    def fail(_):
        raise SessionCryptoError("unavailable")
    monkeypatch.setattr(account_manager, "encrypt_session", fail)
    with pytest.raises(SessionCryptoError):
        manager._save_config()
    assert config.read_bytes() == before
