"""Small, atomic JSON store for operator-owned dashboard settings.

Only non-secret values live in the JSON files. Secrets (AI keys, bot tokens,
recovery credentials) are encrypted with the same versioned SessionCipher used
for Telegram sessions and stored separately, so a backup of the JSON files
never exposes them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from open_teleset.crypto.cipher import SessionCipher

_LOCK = threading.RLock()


def store_dir() -> Path:
    return Path(os.getenv("OPERATOR_STORE_DIR", "./accounts"))


def _path(name: str) -> Path:
    return store_dir() / f"{name}.json"


def read_json(name: str, default):
    with _LOCK:
        try:
            with _path(name).open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError):
            return default
    return value if isinstance(value, type(default)) else default


def write_json(name: str, value) -> None:
    with _LOCK:
        path = _path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        temporary.replace(path)


def secret_key_available() -> bool:
    return bool(os.getenv("SESSION_ENCRYPTION_KEY", "").strip())


def _cipher() -> SessionCipher:
    return SessionCipher()


def read_secret(name: str, default):
    encrypted = read_json(f"{name}.secret", {})
    if not isinstance(encrypted, dict) or not encrypted.get("ciphertext"):
        return default
    try:
        return json.loads(_cipher().decrypt(encrypted["ciphertext"]))
    except (ValueError, RuntimeError, TypeError):
        return default


def write_secret(name: str, value) -> None:
    encrypted = {"ciphertext": _cipher().encrypt(json.dumps(value))}
    write_json(f"{name}.secret", encrypted)


def delete_secret(name: str) -> None:
    with _LOCK:
        try:
            _path(f"{name}.secret").unlink()
        except OSError:
            pass


DEFAULT_FEATURES = {
    "automation_enabled": True,
    "factory_enabled": True,
    # Ships dark: it only does something once the operator points it at a hosted browser.
    "cloud_browser_enabled": False,
    # Ships dark: it sends messages on a real account, so it stays off until switched on.
    "auto_reply_enabled": False,
}


def get_features() -> dict:
    features = read_json("features", {})
    return {**DEFAULT_FEATURES, **{k: bool(v) for k, v in features.items() if k in DEFAULT_FEATURES}}


def set_features(updates: dict) -> dict:
    features = get_features()
    for key in DEFAULT_FEATURES:
        if key in updates and isinstance(updates[key], bool):
            features[key] = updates[key]
    write_json("features", features)
    return features


def feature_enabled(name: str) -> bool:
    return get_features().get(f"{name}_enabled", True)
