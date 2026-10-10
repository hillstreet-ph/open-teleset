"""Exercise real Streamable HTTP JSON-RPC through the existing ASGI auth gate."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from open_teleset import security
from open_teleset.read_only_mcp import create_read_only_mcp


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def authenticate(scope):
        token = security._extract_bearer(scope)
        calls.append(token)
        if token == "denied":
            raise PermissionError("Project administrator required")
        if token not in {"alice", "bob"}:
            raise ValueError("Invalid token")
        return security.AuthenticatedUser(id=token, email=None, role="admin")

    async def readiness():
        return {
            "status": "degraded", "checks": {"service": True, "database": False},
            "reason": "database_unreachable", "secret": "must-not-return",
        }

    monkeypatch.setattr(security, "_authenticate", authenticate)
    server = create_read_only_mcp(
        lambda: [
            {"status": "online", "phone": "private", "session_string": "private"},
            {"status": "offline", "api_hash": "private"},
        ], readiness,
    )
    remote_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(security.SupabaseAuthMiddleware)
    app.mount("/", remote_app)
    with TestClient(app, base_url="https://open-teleset.site") as client:
        yield client, calls


def rpc(client, method, params=None, token="alice", **kwargs):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-03-26",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(kwargs.pop("headers", {}))
    return client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }, headers=headers, **kwargs)


def structured(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_initialize_and_catalog_are_stateless_and_read_only(client):
    http, _ = client
    response = rpc(http, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "mobile-check", "version": "1"},
    })
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    tools = rpc(http, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "authenticated_identity", "runtime_readiness", "account_status_summary",
    }
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
    assert all(not tool["annotations"]["destructiveHint"] for tool in tools)


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("token,status", [(None, 401), ("invalid", 401), ("denied", 403)])
def test_all_rpc_operations_require_current_admin_auth(client, method, token, status):
    http, _ = client
    response = rpc(http, method, {"name": "account_status_summary"}, token=token)
    assert response.status_code == status
    assert "private" not in response.text


def test_each_call_uses_current_identity_and_rechecks_access(client):
    http, calls = client
    for token in ["alice", "bob"]:
        assert structured(rpc(http, "tools/call", {"name": "authenticated_identity"}, token)) == {
            "id": token, "role": "admin", "project": "open-teleset",
        }
    assert rpc(http, "tools/call", {"name": "authenticated_identity"}, "denied").status_code == 403
    assert calls == ["alice", "bob", "denied"]


def test_counts_and_readiness_return_only_safe_fields(client):
    http, _ = client
    assert structured(rpc(http, "tools/call", {"name": "account_status_summary"})) == {
        "total": 2, "online": 1, "offline": 1,
    }
    assert structured(rpc(http, "tools/call", {"name": "runtime_readiness"})) == {
        "status": "degraded", "checks": {"service": True, "database": False},
        "reason": "database_unreachable",
    }


@pytest.mark.parametrize("name", ["send_message", "export_session", "unknown"])
def test_full_stdio_write_and_session_tools_are_unavailable(client, name):
    http, _ = client
    response = rpc(http, "tools/call", {"name": name})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True


@pytest.mark.parametrize("headers,status", [
    ({"Host": "evil.example"}, 421),
    ({"Origin": "https://evil.example"}, 403),
])
def test_transport_rejects_foreign_host_and_origin(client, headers, status):
    http, _ = client
    assert rpc(http, "tools/list", headers=headers).status_code == status


def test_callback_errors_are_redacted(monkeypatch):
    def fail_accounts():
        raise RuntimeError("secret session")

    async def fail_readiness():
        raise RuntimeError("secret database password")

    async def authenticate(scope):
        return security.AuthenticatedUser(id="admin", email=None, role="admin")

    monkeypatch.setattr(security, "_authenticate", authenticate)
    server = create_read_only_mcp(fail_accounts, fail_readiness)
    remote_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(security.SupabaseAuthMiddleware)
    app.mount("/", remote_app)
    with TestClient(app, base_url="https://open-teleset.site") as http:
        for name in ["account_status_summary", "runtime_readiness"]:
            result = rpc(http, "tools/call", {"name": name}).json()["result"]
            assert result["isError"] is True
            assert "secret" not in json.dumps(result)


def test_real_dashboard_starts_manager_and_preserves_routes(tmp_path):
    # Isolate all legacy relative-path managers from real account/session files.
    (tmp_path / "static").mkdir()
    root = Path(__file__).resolve().parents[1]
    script = '''
from fastapi.testclient import TestClient
import dashboard
from open_teleset import security

async def authenticate(scope):
    return security.AuthenticatedUser(id="synthetic-admin", email=None, role="admin")

with TestClient(dashboard.app, base_url="https://open-teleset.site") as http:
    assert http.get("/health").status_code == 200
    assert http.get("/readyz").status_code == 503
    assert http.post("/mcp", json={}).status_code == 401
    security._authenticate = authenticate
    response = http.post("/mcp", headers={
        "Authorization": "Bearer synthetic",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "account_status_summary"}})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"] == {
        "total": 0, "online": 0, "offline": 0,
    }
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "SUPABASE_", "DATABASE_", "SENTRY_", "TELEGRAM_",
    ))}
    env.update(APP_ENV="production", PYTHONPATH=f"{root}:{root / 'src'}")
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
