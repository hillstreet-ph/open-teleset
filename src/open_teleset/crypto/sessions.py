"""Encrypt / decrypt Telegram session strings at rest."""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class SessionCryptoError(Exception):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SessionCryptoError(
            "SESSION_ENCRYPTION_KEY is not set. "
            'Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise SessionCryptoError(f"Invalid SESSION_ENCRYPTION_KEY: {e}") from e


def encrypt_session(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_session(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise SessionCryptoError("Failed to decrypt session") from e
