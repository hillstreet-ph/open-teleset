"""Server-owned API keys for programmatic access to Open-Teleset.

Keys are hashed (SHA-256) before storage and the plaintext is returned only once,
at creation. Authentication still resolves a key to its owner and then through the
shared administrator gate, so a key grants access only while its user remains an
approved administrator and only until the key is revoked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

import httpx

from open_teleset.security import API_KEY_PREFIX, _settings

_SCHEMA = "operations_shared"
_ENDPOINT = "/rest/v1/api_keys"
_TIMEOUT = 5.0
_TOUCH_INTERVAL = 60.0
_last_touch: dict[str, float] = {}


def _service_headers(write: bool = False) -> dict[str, str]:
    settings = _settings()
    headers = {
        "apikey": settings["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {settings['SUPABASE_SERVICE_ROLE_KEY']}",
        "Accept-Profile": _SCHEMA,
    }
    if write:
        headers["Content-Profile"] = _SCHEMA
        headers["Prefer"] = "return=representation"
    return headers


def _pepper() -> bytes:
    """Server-side pepper used as the KDF salt.

    Deriving the stored digest with the service-role secret as the salt means a
    database-only compromise cannot verify or forge keys without also holding this
    server secret. It is available server-side only and is never sent to clients.
    """
    return _settings()["SUPABASE_SERVICE_ROLE_KEY"].encode("utf-8")


_KDF_ITERATIONS = 200_000


def _hash(token: str) -> str:
    """Derive the stored digest of an API token with PBKDF2-HMAC-SHA256.

    The server-side service-role secret is used as the salt/pepper, so a
    database-only compromise cannot verify or forge keys without also holding the
    server secret. A single global pepper (rather than a per-row salt) is
    sufficient here because each token is itself a 256-bit cryptographically
    random value, so tokens never collide and rainbow tables do not apply; the
    key-stretching KDF is defense-in-depth. Verification stays deterministic:
    look up by prefix, recompute, and compare in constant time.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", token.encode("utf-8"), _pepper(), _KDF_ITERATIONS
    ).hex()


def _generate() -> tuple[str, str, str]:
    prefix = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"{API_KEY_PREFIX}{prefix}_{secret}"
    return token, prefix, _hash(token)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "prefix": row.get("key_prefix"),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "revoked_at": row.get("revoked_at"),
    }


async def create_key(user_id: str, name: str | None) -> dict[str, Any]:
    token, prefix, key_hash = _generate()
    settings = _settings()
    clean_name = (name or "").strip()[:80] or "API key"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{settings['SUPABASE_URL']}{_ENDPOINT}",
            headers=_service_headers(write=True),
            json={
                "user_id": user_id,
                "name": clean_name,
                "key_prefix": prefix,
                "key_hash": key_hash,
            },
        )
        response.raise_for_status()
        row = response.json()[0]
    result = _public(row)
    result["key"] = token  # Shown once; never stored in plaintext or returned again.
    return result


async def list_keys(user_id: str) -> list[dict[str, Any]]:
    settings = _settings()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings['SUPABASE_URL']}{_ENDPOINT}",
            headers=_service_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,name,key_prefix,created_at,last_used_at,revoked_at",
                "order": "created_at.desc",
            },
        )
        response.raise_for_status()
        return [_public(row) for row in response.json()]


async def revoke_key(user_id: str, key_id: str) -> bool:
    settings = _settings()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.patch(
            f"{settings['SUPABASE_URL']}{_ENDPOINT}",
            headers=_service_headers(write=True),
            params={"id": f"eq.{key_id}", "user_id": f"eq.{user_id}", "revoked_at": "is.null"},
            json={"revoked_at": "now()"},
        )
        response.raise_for_status()
        return len(response.json()) > 0


async def _touch(key_id: Any) -> None:
    """Record last use, at most once per interval, never blocking authentication."""
    if not key_id:
        return
    now = time.monotonic()
    if now - _last_touch.get(str(key_id), 0.0) < _TOUCH_INTERVAL:
        return
    _last_touch[str(key_id)] = now
    settings = _settings()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.patch(
                f"{settings['SUPABASE_URL']}{_ENDPOINT}",
                headers=_service_headers(write=True),
                params={"id": f"eq.{key_id}"},
                json={"last_used_at": "now()"},
            )
    except httpx.HTTPError:
        pass  # Last-used tracking is best-effort and must never block authentication.


async def authenticate_api_key(token: str) -> str:
    """Return the owner user_id for a valid, non-revoked key, else raise PermissionError."""
    if not token.startswith(API_KEY_PREFIX):
        raise PermissionError("Invalid API key")
    prefix = token[len(API_KEY_PREFIX):].split("_", 1)[0]
    if not prefix or len(prefix) != 16 or not all(c in "0123456789abcdef" for c in prefix):
        raise PermissionError("Invalid API key")
    settings = _settings()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings['SUPABASE_URL']}{_ENDPOINT}",
            headers=_service_headers(),
            params={
                "key_prefix": f"eq.{prefix}",
                "select": "id,user_id,key_hash,revoked_at",
                "limit": "1",
            },
        )
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        raise PermissionError("Invalid API key")
    row = rows[0]
    if row.get("revoked_at") is not None:
        raise PermissionError("Invalid API key")
    if not hmac.compare_digest(str(row.get("key_hash", "")), _hash(token)):
        raise PermissionError("Invalid API key")
    await _touch(row.get("id"))
    return str(row["user_id"])
