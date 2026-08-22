import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())

from open_teleset.crypto import SessionCryptoError, decrypt_session, encrypt_session


def test_roundtrip():
    plain = "1BVtsola-test-session"
    assert decrypt_session(encrypt_session(plain)) == plain


def test_empty():
    assert encrypt_session("") == ""
    assert decrypt_session("") == ""


def test_tamper():
    ct = encrypt_session("secret")
    with pytest.raises(SessionCryptoError):
        decrypt_session(ct[:-4] + "xxxx")
