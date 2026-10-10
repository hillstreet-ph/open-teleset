from unittest.mock import AsyncMock

import pytest
import proxy_manager as module


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROXIES_FILE", str(tmp_path / "proxies.json"))
    return module.ProxyManager()


def test_public_candidates_reject_private_dns_credentials_and_bad_ports(manager):
    lines = ['socks5://8.8.8.8:1080', 'socks5://8.8.8.8:1080', 'socks5://127.0.0.1:1080', 'socks5://169.254.169.254:80', 'socks5://localhost:1080', 'socks5://user:pass@8.8.8.8:1080', 'http://8.8.8.8:80', 'socks5://8.8.8.8:65536', 'socks5://8.8.8.8:0', 'socks5://8.8.8.8:1080/path', 'socks5://192.168.1.2:1080']
    assert manager.free_candidates('\n'.join(lines)) == [{"protocol": "socks5", "host": "8.8.8.8", "port": 1080}]


@pytest.mark.asyncio
async def test_only_verified_proxy_becomes_stable_global_default(manager, monkeypatch):
    candidate = {"protocol": "socks5", "host": "8.8.8.8", "port": 1080}
    monkeypatch.setattr(manager, "fetch_free_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(manager, "test_proxy", AsyncMock(return_value={"success": False}))
    result = await manager.ensure_free_global_proxy()
    assert result["status"] == "no_verified_free_proxy" and manager.global_proxy is None
    manager.test_proxy.return_value = {"success": True}
    assert (await manager.ensure_free_global_proxy())["success"]
    assert manager.global_proxy["free"] and manager.global_proxy["verified_at"]
    assert module.ProxyManager().global_proxy == manager.global_proxy
    manager.fetch_free_candidates.reset_mock()
    assert (await manager.ensure_free_global_proxy())["status"] == "existing_global_preserved"
    assert manager.fetch_free_candidates.await_count == 0


@pytest.mark.asyncio
async def test_removal_disables_default_and_explicit_enable_retries(manager, monkeypatch):
    manager.remove_global_proxy()
    monkeypatch.setattr(manager, "fetch_free_candidates", AsyncMock(side_effect=TimeoutError()))
    assert (await manager.ensure_free_global_proxy())["status"] == "disabled"
    assert manager.fetch_free_candidates.await_count == 0
    assert not module.ProxyManager().free_global_enabled
    assert (await manager.ensure_free_global_proxy(enable=True))["status"] == "free_proxy_source_unavailable"
    assert manager.global_proxy is None


@pytest.mark.asyncio
async def test_manual_global_change_during_probe_wins(manager, monkeypatch):
    monkeypatch.setattr(manager, "fetch_free_candidates", AsyncMock(return_value=[{"protocol": "socks5", "host": "8.8.8.8", "port": 1080}]))
    async def probe(*args, **kwargs):
        manager.set_global_proxy('http', '9.9.9.9', 80)
        return {"success": True}
    monkeypatch.setattr(manager, "test_proxy", probe)
    assert (await manager.ensure_free_global_proxy())["status"] == "configuration_changed"
    assert manager.global_proxy["host"] == '9.9.9.9'
