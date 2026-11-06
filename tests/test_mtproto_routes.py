from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import dashboard
import mtproxy

SECRET = "dd" + "0" * 32


@pytest.mark.asyncio
async def test_catalog_route_probes_every_page_item(monkeypatch):
    monkeypatch.setattr(mtproxy, "fetch_catalog", AsyncMock(return_value={
        "items": [{"host": "8.8.8.8", "port": 443, "secret": SECRET}],
        "sources": [{"id": "radikal", "label": "Radikal", "count": 1, "status": "ok"}],
        "cached": False,
    }))
    probe = AsyncMock(return_value=[{"host": "8.8.8.8", "port": 443, "secret": SECRET,
                                     "reachable": True, "latency_ms": 82}])
    monkeypatch.setattr(mtproxy, "probe_items", probe)
    result = await dashboard.mtproto_proxy_catalog(limit=10, refresh=True)
    assert result["items"][0]["latency_ms"] == 82
    assert result["sources"][0]["status"] == "ok"
    assert probe.await_args.args[0][0]["host"] == "8.8.8.8"


@pytest.mark.asyncio
async def test_verify_route_uses_the_deep_check(monkeypatch):
    verify = AsyncMock(return_value={"success": True, "latency_ms": 210})
    monkeypatch.setattr(mtproxy, "deep_verify", verify)
    result = await dashboard.verify_mtproto_proxy(
        dashboard.MtprotoEndpoint(host="8.8.8.8", port=443, secret=SECRET))
    assert result == {"success": True, "result": {"success": True, "latency_ms": 210}}
    verify.assert_awaited_once_with({"host": "8.8.8.8", "port": 443, "secret": SECRET})


@pytest.mark.asyncio
async def test_save_route_rejects_an_invalid_secret():
    with pytest.raises(HTTPException) as excinfo:
        await dashboard.save_mtproto_proxy(dashboard.SaveMtprotoProxyRequest(
            host="8.8.8.8", port=443, secret="nope"))
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_save_route_can_set_the_global_proxy(monkeypatch):
    added = []
    monkeypatch.setattr(dashboard.proxy_manager, "add_proxy",
                        lambda proxy_id, protocol, **fields: added.append((proxy_id, protocol, fields)) or True)
    globals_set = []
    monkeypatch.setattr(dashboard.proxy_manager, "set_global_proxy",
                        lambda protocol, **fields: globals_set.append((protocol, fields)) or True)
    result = await dashboard.save_mtproto_proxy(dashboard.SaveMtprotoProxyRequest(
        host="8.8.8.8", port=443, secret="0" * 32, mode="global"))
    assert result["mode"] == "global" and result["proxy_id"].startswith("mt-")
    assert added == [(result["proxy_id"], "mtproto",
                      {"host": "8.8.8.8", "port": 443, "secret": "0" * 32})]
    assert globals_set == [("mtproto", {"host": "8.8.8.8", "port": 443, "secret": "0" * 32})]


@pytest.mark.asyncio
async def test_save_route_assigns_to_an_existing_account(monkeypatch):
    monkeypatch.setattr(dashboard.account_manager, "accounts", {"acct-1": {}})
    monkeypatch.setattr(dashboard.proxy_manager, "add_proxy", lambda *a, **k: True)
    assigned = []
    monkeypatch.setattr(dashboard.proxy_manager, "assign_proxy_to_account",
                        lambda account_id, proxy_id: assigned.append((account_id, proxy_id)) or True)
    connection = AsyncMock()
    monkeypatch.setattr(dashboard, "update_account_connection", connection)
    result = await dashboard.save_mtproto_proxy(dashboard.SaveMtprotoProxyRequest(
        host="8.8.8.8", port=443, secret=SECRET, mode="assign", account_id="acct-1"))
    assert result["mode"] == "assign"
    assert assigned == [("acct-1", result["proxy_id"])]
    connection.assert_awaited_once_with("acct-1", {"proxy_mode": "selected",
                                                   "proxy_id": result["proxy_id"]})


@pytest.mark.asyncio
async def test_save_route_refuses_an_unknown_account(monkeypatch):
    monkeypatch.setattr(dashboard.account_manager, "accounts", {})
    with pytest.raises(HTTPException) as excinfo:
        await dashboard.save_mtproto_proxy(dashboard.SaveMtprotoProxyRequest(
            host="8.8.8.8", port=443, secret=SECRET, mode="assign", account_id="missing"))
    assert excinfo.value.status_code == 404
