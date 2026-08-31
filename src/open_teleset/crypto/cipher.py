"""Versioned authenticated encryption for Telegram session credentials."""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    """Encrypt and decrypt Telegram StringSession values with key versioning."""

    def __init__(self, key: str | None = None, version: int | None = None) -> None:
        raw_key = key or os.getenv("SESSION_ENCRYPTION_KEY", "")
        if not raw_key:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is required")
        self.version = version or int(os.getenv("SESSION_ENCRYPTION_KEY_VERSION", "1"))
        self._fernet = Fernet(raw_key.encode("ascii"))

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, session: str) -> str:
        if not session:
            raise ValueError("Session cannot be empty")
        token = self._fernet.encrypt(session.encode("utf-8"))
        encoded = base64.urlsafe_b64encode(token).decode("ascii")
        return f"v{self.version}:{encoded}"

    def decrypt(self, encrypted_session: str) -> str:
        prefix, separator, payload = encrypted_session.partition(":")
        if not separator or prefix != f"v{self.version}":
            raise ValueError("Unsupported session encryption version")
        try:
            token = base64.urlsafe_b64decode(payload.encode("ascii"))
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("Invalid encrypted session") from exc
