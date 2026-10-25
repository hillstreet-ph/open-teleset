import pytest

from open_teleset import app_settings


@pytest.mark.asyncio
async def test_env_var_takes_precedence(monkeypatch):
    app_settings._cache.clear()
    monkeypatch.setenv("MY_VAR", "from-env")
    assert await app_settings.get_setting("my_key", "MY_VAR") == "from-env"


@pytest.mark.asyncio
async def test_reads_stored_value_when_env_absent(monkeypatch):
    app_settings._cache.clear()
    monkeypatch.delenv("MY_VAR", raising=False)
    monkeypatch.setattr(app_settings, "_settings",
                        lambda: {"SUPABASE_URL": "https://x", "SUPABASE_SERVICE_ROLE_KEY": "svc"})

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"value": "stored-token"}]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_k):
            return Resp()

    monkeypatch.setattr(app_settings.httpx, "AsyncClient", lambda *a, **k: Client())
    assert await app_settings.get_setting("tgstat_token") == "stored-token"


@pytest.mark.asyncio
async def test_missing_config_returns_empty(monkeypatch):
    app_settings._cache.clear()
    monkeypatch.delenv("MISSING", raising=False)

    def boom():
        raise KeyError("no service config")

    monkeypatch.setattr(app_settings, "_settings", boom)
    assert await app_settings.get_setting("missing_key", "MISSING") == ""
