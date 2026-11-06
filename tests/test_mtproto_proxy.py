from unittest.mock import AsyncMock

import pytest
import proxy_manager as module


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROXIES_FILE", str(tmp_path / "proxies.json"))
    return module.ProxyManager()


def test_mtproto_secret_is_required_and_normalized(manager):
    assert manager.add_proxy("mt-1", "mtproto", "8.8.8.8", 443) is False
    assert manager.add_proxy("mt-1", "mtproto", "8.8.8.8", 443, secret="bad") is False
    assert manager.add_proxy("mt-1", "mtproto", "8.8.8.8", 443, secret="0" * 32) is True
    assert manager.proxies["mt-1"]["secret"] == "dd" + "0" * 32
    assert manager.add_proxy("socks-1", "socks5", "8.8.8.8", 1080) is True
    assert "secret" not in manager.proxies["socks-1"]


def test_telethon_format_returns_a_secret_tuple_for_mtproto(manager):
    manager.add_proxy("mt-1", "mtproto", "8.8.8.8", 443, secret="ee" + "a" * 32)
    assert manager.to_telethon_format(manager.proxies["mt-1"]) == ("8.8.8.8", 443, "ee" + "a" * 32)
    transport = module.proxy_transport(("8.8.8.8", 443, "0" * 34))
    assert transport["connection"].__name__ == "ConnectionTcpMTProxyRandomizedIntermediate"
    assert module.proxy_transport({"proxy_type": "socks5"}) == {}


def test_invalid_saved_mtproto_secret_is_never_silently_dropped(manager):
    manager.proxies["broken"] = {"protocol": "mtproto", "host": "8.8.8.8", "port": 443, "secret": "bad"}
    with pytest.raises(ValueError, match="MTProto proxy secret"):
        manager.get_proxy("broken")
    with pytest.raises(ValueError, match="MTProto proxy secret"):
        manager.resolve_login_proxy(mode="selected", proxy_id="broken")


def test_global_mtproto_proxy(manager):
    assert manager.set_global_proxy("mtproto", "8.8.8.8", 443, secret="0" * 32) is True
    assert manager.get_global_proxy() == ("8.8.8.8", 443, "dd" + "0" * 32)
    assert manager.set_global_proxy("mtproto", "8.8.8.8", 443) is False


@pytest.mark.asyncio
async def test_mtproto_test_proxy_only_checks_reachability(manager, monkeypatch):
    probe = AsyncMock(return_value=95)
    monkeypatch.setattr(module, "tcp_latency", probe)
    result = await manager.test_proxy({"protocol": "mtproto", "host": "8.8.8.8", "port": 443,
                                       "secret": "dd" + "0" * 32}, timeout=3)
    assert result == {"success": True, "response_time": 95}
    probe.return_value = None
    assert (await manager.test_proxy({"protocol": "mtproto", "host": "8.8.8.8", "port": 443}))["success"] is False
