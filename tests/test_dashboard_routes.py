"""Exercise direct-host login routes without live accounts or credentials."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_direct_host_login_shell_config_and_auth_boundary(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "static", tmp_path / "static")
    script = '''
import os
from fastapi.testclient import TestClient
import dashboard

with TestClient(dashboard.app, base_url="https://open-teleset.site") as http:
    response = http.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"
    for method in ["GET", "HEAD"]:
        response = http.request(method, "/dashboard")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
    assert 'src="/config.js"' in http.get("/dashboard").text
    assert 'src="/static/csv.js?v=' in http.get("/dashboard").text
    assert http.get('/static/csv.js').status_code == 200
    assert http.post('/api/proxies/free-global').status_code == 401
    for invalid in ["", "sb_secret_synthetic", "eyJ.synthetic.service-role", 'sb_publishable_\\";alert(1)//']:
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = invalid
        response = http.get("/config.js")
        assert response.status_code == 503
        assert invalid not in response.text if invalid else True
    os.environ["SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_synthetic-public-key"
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "synthetic-private-never-publish"
    os.environ["DASHBOARD_PASSWORD"] = "synthetic-legacy-never-publish"
    os.environ["DASHBOARD_USER"] = "admin"
    os.environ["DASHBOARD_LOGIN_EMAIL"] = "synthetic@example.com"
    response = http.get("/config.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert "sb_publishable_synthetic-public-key" in response.text
    assert "__SUPABASE_PUBLISHABLE_KEY__" not in response.text
    assert "synthetic-private-never-publish" not in response.text
    assert "synthetic-legacy-never-publish" not in response.text
    assert 'apiBase: window.location.origin' in response.text
    assert '"admin": "synthetic@example.com"' in response.text
    assert http.head("/config.js").status_code == 200
    for path in ["/api/accounts", "/api/auth/me", "/mcp", "/dashboard-extra", "/config.js/extra"]:
        assert http.get(path).status_code == 401
    for path in ["/", "/dashboard", "/config.js"]:
        assert http.post(path).status_code == 401
    response = http.options("/api/auth/me", headers={
        "Origin": "https://app.open-teleset.site",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.open-teleset.site"
    response = http.options("/api/auth/me", headers={
        "Origin": "https://untrusted.example",
        "Access-Control-Request-Method": "GET",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert http.get("/readyz").status_code == 503
http = TestClient(dashboard.app, base_url="https://app.open-teleset.site")
for method in ["GET", "HEAD"]:
    for path in ["/", "/dashboard"]:
        response = http.request(method, path + "?code=synthetic-code", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "https://open-teleset.site/dashboard?code=synthetic-code"
        assert response.headers["cache-control"] == "no-store"
# Redirects apply only to the public shell; private endpoints retain their gate.
assert http.get("/api/auth/me").status_code == 401
assert http.post("/dashboard").status_code == 401
http = TestClient(dashboard.app, base_url="http://localhost:8080")
assert http.get("/", follow_redirects=False).headers["location"] == "/dashboard"
assert http.get("/dashboard").status_code == 200
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "SUPABASE_", "DATABASE_", "SENTRY_", "TELEGRAM_",
    ))}
    env.update(APP_ENV="production", PYTHONPATH=f"{root}:{root / 'src'}")
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr


def test_status_socket_refuses_callers_the_gate_cannot_verify(tmp_path):
    """The live status channel carries every account, so it closes before accept."""
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "static", tmp_path / "static")
    script = '''
import os
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import dashboard

client = TestClient(dashboard.app, base_url="https://open-teleset.site")
for attempts in (
    {"headers": {}},
    {"headers": {"Authorization": "Bearer eyJ.synthetic.unsigned"}},
    {"subprotocols": ["open-teleset.v1", "bearer.eyJ.synthetic.unsigned"]},
    {"subprotocols": ["open-teleset.v1"]},
):
    try:
        with client.websocket_connect("/ws", **attempts):
            raise AssertionError(f"/ws accepted without verified admin auth: {attempts}")
    except WebSocketDisconnect as closed:
        assert closed.code == 1008
assert dashboard.manager.active_connections == []
assert os.environ.get("SUPABASE_SERVICE_ROLE_KEY") is None
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "SUPABASE_", "DATABASE_", "SENTRY_", "TELEGRAM_",
    ))}
    env.update(APP_ENV="production", PYTHONPATH=f"{root}:{root / 'src'}")
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr


def test_websocket_disconnect_during_accept_is_clean(tmp_path):
    (tmp_path / "static").mkdir()
    root = Path(__file__).resolve().parents[1]
    script = '''
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace
import dashboard

async def run():
    for error in (OSError('closed'), dashboard.WebSocketDisconnect(1006)):
        socket = SimpleNamespace(accept=AsyncMock(side_effect=error))
        await dashboard.websocket_endpoint(socket)
        assert socket not in dashboard.manager.active_connections
        dashboard.manager.disconnect(socket)
asyncio.run(run())
'''
    env = dict(os.environ, PYTHONPATH=f'{root}:{root / "src"}', APP_ENV='development')
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr


def test_account_proxy_routes_and_hex_import(tmp_path):
    (tmp_path / 'static').mkdir()
    root = Path(__file__).resolve().parents[1]
    script = '''
import asyncio
from unittest.mock import AsyncMock, Mock
from starlette.routing import Match
from fastapi import HTTPException
import dashboard as d

async def run():
    d.proxy_manager.proxies = {'one': {'protocol': 'socks5', 'host': '127.0.0.1', 'port': 1080}}
    d.proxy_manager.global_proxy = {'protocol': 'http', 'host': '127.0.0.2', 'port': 8080}
    d.proxy_manager._save_proxies = Mock()
    d.account_manager.accounts = {'a': {'proxy_settings': {'mode': 'direct'}}}
    d.account_manager._save_config = Mock()
    assert d.login_connection({'proxy_mode': 'direct'}, 'a')[0] is None
    assert d.login_connection({'proxy_id': 'one'}, 'a')[1] == {'mode': 'selected', 'proxy_id': 'one'}
    try: d.login_connection({'proxy_mode': 'selected', 'proxy_id': 'absent'}, 'a')
    except HTTPException as e: assert e.status_code == 400
    else: raise AssertionError('missing proxy accepted')
    await d.assign_proxy(d.AssignProxyRequest(account_id='a', proxy_id='one'))
    assert d.account_manager.accounts['a']['proxy_settings'] == {'mode': 'selected', 'proxy_id': 'one'}
    await d.unassign_proxy('one', 'a')
    assert d.account_manager.accounts['a']['proxy_settings'] == {'mode': 'default'}
    scope = {'type': 'http', 'path': '/api/proxies/global', 'method': 'DELETE'}
    route = next(r for r in d.app.routes if r.matches(scope)[0] == Match.FULL)
    assert route.path == '/api/proxies/global'
    d.proxy_manager.test_proxy = AsyncMock(return_value={'success': True})
    await d.test_proxies('global')
    d.proxy_manager.test_proxy.assert_awaited_once_with(d.proxy_manager.global_proxy)
    d.account_manager.import_hex_session = AsyncMock(return_value={'success': True, 'account_id': 'new'})
    route = next(r for r in d.app.routes if getattr(r, 'path', '') == '/api/accounts/add-hex-session')
    result = await route.endpoint({'account_id': 'new', 'hex_key': 'synthetic', 'dc_id': 5, 'proxy_mode': 'direct'})
    assert result['success']
    assert d.account_manager.import_hex_session.await_args.kwargs['proxy_settings'] == {'mode': 'direct'}
asyncio.run(run())
'''
    env = dict(os.environ, PYTHONPATH=f'{root}:{root / "src"}', APP_ENV='development')
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
