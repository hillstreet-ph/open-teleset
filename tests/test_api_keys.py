"""API key generation, hashing, and authentication (no live database)."""
import hashlib
import hmac

import pytest

from open_teleset import api_keys


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    def json(self):
        return self._data


class _Client:
    def __init__(self, router):
        self._router = router

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None, params=None):
        return self._router("get", params)

    async def post(self, url, headers=None, json=None):
        return self._router("post", json)

    async def patch(self, url, headers=None, params=None, json=None):
        return self._router("patch", params)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(
        api_keys,
        "_settings",
        lambda: {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "svc"},
    )


def _use_router(monkeypatch, router):
    monkeypatch.setattr(api_keys.httpx, "AsyncClient", lambda *a, **k: _Client(router))


def test_generate_format():
    token, prefix, key_hash = api_keys._generate()
    assert token.startswith("ott_")
    assert len(prefix) == 16 and all(c in "0123456789abcdef" for c in prefix)
    # Keyed HMAC-SHA256 under the fake service-role pepper, not a bare SHA-256.
    assert key_hash == hmac.new(b"svc", token.encode(), hashlib.sha256).hexdigest()
    assert key_hash != hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.asyncio
async def test_authenticate_valid_key(monkeypatch):
    token, _prefix, key_hash = api_keys._generate()

    def router(method, _arg):
        if method == "get":
            return _Resp([{"id": "kid", "user_id": "uid-1", "key_hash": key_hash, "revoked_at": None}])
        return _Resp([{}])

    _use_router(monkeypatch, router)
    assert await api_keys.authenticate_api_key(token) == "uid-1"


@pytest.mark.asyncio
async def test_authenticate_revoked_key(monkeypatch):
    token, _prefix, key_hash = api_keys._generate()
    _use_router(
        monkeypatch,
        lambda m, a: _Resp([{"id": "k", "user_id": "u", "key_hash": key_hash, "revoked_at": "2026-01-01T00:00:00Z"}]),
    )
    with pytest.raises(PermissionError):
        await api_keys.authenticate_api_key(token)


@pytest.mark.asyncio
async def test_authenticate_wrong_hash(monkeypatch):
    token, _prefix, _key_hash = api_keys._generate()
    _use_router(
        monkeypatch,
        lambda m, a: _Resp([{"id": "k", "user_id": "u", "key_hash": "deadbeef", "revoked_at": None}]),
    )
    with pytest.raises(PermissionError):
        await api_keys.authenticate_api_key(token)


@pytest.mark.asyncio
async def test_authenticate_unknown_prefix(monkeypatch):
    token, _prefix, _key_hash = api_keys._generate()
    _use_router(monkeypatch, lambda m, a: _Resp([]))
    with pytest.raises(PermissionError):
        await api_keys.authenticate_api_key(token)


@pytest.mark.asyncio
async def test_authenticate_malformed_keys():
    for bad in ["notakey", "ott_xyz", "ott_", "ott_zzzzzzzzzzzzzzzz_secret"]:
        with pytest.raises(PermissionError):
            await api_keys.authenticate_api_key(bad)
