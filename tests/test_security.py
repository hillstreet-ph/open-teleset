"""Exercise the HTTP/WS boundary with signed test JWTs and mocked role storage."""

import time
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_teleset import security

USER_ID = "a77c78ee-a9c9-4b94-a792-16f6b287c5c8"
ISSUER = "https://test-project.supabase.co/auth/v1"


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only-service-key")
    monkeypatch.delenv("SUPABASE_JWT_ISSUER", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_AUDIENCE", raising=False)
    security._settings.cache_clear()
    private_key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(security, "_jwks_client", lambda: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key())
    ))
    state = {
        "accounts": [{"user_id": USER_ID, "role": "admin", "approved": True}],
        "assignments": [{"user_id": USER_ID, "project_key": "open-teleset", "role": "admin"}],
        "status": 200,
        "requests": [],
        "operational_calls": 0,
    }

    def provider(request):
        state["requests"].append(request)
        body = state["accounts"] if request.url.path.endswith("account_roles") else state["assignments"]
        return httpx.Response(state["status"], json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(provider)
    ))

    def token(**overrides):
        claims = {
            "iss": ISSUER, "aud": "authenticated", "sub": USER_ID,
            "role": "authenticated", "iat": int(time.time()) - 1,
            "exp": int(time.time()) + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "test"})

    app = FastAPI()
    app.add_middleware(security.SupabaseAuthMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=["https://open-teleset.site"],
        allow_methods=["GET", "POST"], allow_headers=["Authorization"],
    )

    @app.get("/health")
    @app.get("/readyz")
    @app.get("/")
    @app.get("/static/dashboard.html")
    def public():
        return {"public": True}

    @app.get("/api/accounts/test/export-session")
    @app.post("/api/members/add")
    @app.options("/api/members/add")
    def operational(request: Request):
        state["operational_calls"] += 1
        return {"user": request.state.user.id}

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket):
        await websocket.accept()
        while True:
            try:
                await websocket.receive_text()
                await websocket.send_json({"private_status": True})
            except WebSocketDisconnect:
                return

    with TestClient(app) as client:
        yield client, token, state
    security._settings.cache_clear()


@pytest.mark.parametrize("path", ["/", "/health", "/readyz", "/static/dashboard.html"])
def test_public_pages_remain_available_without_auth_configuration(boundary, monkeypatch, path):
    client, _, state = boundary
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY")
    security._settings.cache_clear()
    assert client.get(path).status_code == 200
    assert not state["requests"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/accounts/test/export-session"),
    ("POST", "/api/members/add"),
    ("OPTIONS", "/api/members/add"),
])
def test_anonymous_operational_requests_never_reach_handler(boundary, method, path):
    client, _, state = boundary
    assert client.request(method, path).status_code == 401
    assert state["operational_calls"] == 0


def test_cors_preflight_is_public_without_invoking_operational_handler(boundary):
    client, _, state = boundary
    response = client.options("/api/members/add", headers={
        "Origin": "https://open-teleset.site",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization",
    })
    assert response.status_code == 200
    assert state["operational_calls"] == 0


def test_approved_project_admin_can_access_operational_api(boundary):
    client, token, state = boundary
    response = client.get("/api/accounts/test/export-session", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 200
    assert response.json() == {"user": USER_ID}
    assert len(state["requests"]) == 2
    account, project = state["requests"]
    assert account.headers["Accept-Profile"] == "operations_shared"
    assert project.url.params["project_key"] == "eq.open-teleset"
    assert account.url.params["user_id"] == f"eq.{USER_ID}"


@pytest.mark.parametrize("role", ["viewer", "operator", "user", "unknown"])
def test_non_admin_cannot_export_sessions(boundary, role):
    client, token, state = boundary
    state["accounts"][0]["role"] = role
    assert client.get("/api/accounts/test/export-session", headers={"Authorization": f"Bearer {token()}"}).status_code == 403
    assert state["operational_calls"] == 0


@pytest.mark.parametrize("change", ["unapproved", "missing_account", "missing_assignment", "wrong_project", "wrong_user", "project_user"])
def test_unapproved_or_unassigned_administrators_fail_closed(boundary, change):
    client, token, state = boundary
    if change == "unapproved":
        state["accounts"][0]["approved"] = False
    elif change == "missing_account":
        state["accounts"] = []
    elif change == "missing_assignment":
        state["assignments"] = []
    elif change == "wrong_project":
        state["assignments"][0]["project_key"] = "other-project"
    elif change == "wrong_user":
        state["assignments"][0]["user_id"] = "different-user"
    elif change == "project_user":
        state["assignments"][0]["role"] = "user"
    response = client.post("/api/members/add", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 403
    assert state["operational_calls"] == 0


@pytest.mark.parametrize("overrides", [
    {"exp": 1}, {"iss": "https://wrong-project.supabase.co/auth/v1"},
    {"aud": "service_role"}, {"role": "service_role"}, {"sub": "not-a-uuid"},
    {"iat": int(time.time()) + 600},
])
def test_invalid_signed_tokens_never_query_roles(boundary, overrides):
    client, token, state = boundary
    response = client.post("/api/members/add", headers={"Authorization": f"Bearer {token(**overrides)}"})
    assert response.status_code == 401
    assert not state["requests"]
    assert state["operational_calls"] == 0


def test_unsigned_token_is_denied(boundary):
    client, _, state = boundary
    forged = jwt.encode({"sub": USER_ID, "role": "admin"}, key="", algorithm="none")
    assert client.get("/api/accounts/test/export-session", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    assert not state["requests"]


def test_provider_failure_is_generic_and_fail_closed(boundary):
    client, token, state = boundary
    state["status"] = 500
    response = client.post("/api/members/add", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 401
    assert "test-only-service-key" not in response.text
    assert "test-project" not in response.text
    assert state["operational_calls"] == 0


def test_mismatched_runtime_issuer_is_denied(boundary, monkeypatch):
    client, token, state = boundary
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://other.supabase.co/auth/v1")
    security._settings.cache_clear()
    assert client.post("/api/members/add", headers={"Authorization": f"Bearer {token()}"}).status_code == 401
    assert not state["requests"]


def test_missing_runtime_credentials_deny_operational_requests(boundary, monkeypatch):
    client, token, state = boundary
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY")
    security._settings.cache_clear()
    response = client.post("/api/members/add", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 401
    assert state["operational_calls"] == 0
    assert not state["requests"]


def test_websocket_requires_authentication_before_accept(boundary):
    client, _, _ = boundary
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?token=ignored"):
            pass
    assert exc.value.code == 1008


def test_websocket_checks_revocation_before_sending_private_data(boundary):
    client, token, state = boundary
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token()}"}) as ws:
        ws.send_text("status")
        assert ws.receive_json() == {"private_status": True}
        state["accounts"][0]["approved"] = False
        ws.send_text("status")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_websocket_closes_when_access_token_expires(boundary, monkeypatch):
    client, token, _ = boundary
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token()}"}) as ws:
        ws.send_text("status")
        assert ws.receive_json() == {"private_status": True}

        def expired(_token):
            raise jwt.ExpiredSignatureError("expired")

        monkeypatch.setattr(security, "_decode_access_token", expired)
        ws.send_text("status")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_browser_websocket_uses_fixed_protocol_without_echoing_token(boundary):
    client, token, _ = boundary
    offered = [security.WS_PROTOCOL, security.WS_TOKEN_PREFIX + token()]
    with client.websocket_connect(
        "/ws", subprotocols=offered, headers={"Origin": "https://open-teleset.site"}
    ) as ws:
        assert ws.accepted_subprotocol == security.WS_PROTOCOL
        ws.send_text("status")
        assert ws.receive_json() == {"private_status": True}


@pytest.mark.parametrize("origin", [None, "https://attacker.example", "null"])
def test_browser_websocket_rejects_untrusted_origins(boundary, origin):
    client, token, state = boundary
    headers = {"Origin": origin} if origin else {}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws", subprotocols=[security.WS_PROTOCOL, security.WS_TOKEN_PREFIX + token()],
            headers=headers,
        ):
            pass
    assert not state["requests"]


@pytest.mark.parametrize("protocols", [
    ["bearer.invalid"], [security.WS_PROTOCOL],
    [security.WS_PROTOCOL, "bearer.first", "bearer.second"],
    ["other.protocol", "bearer.invalid"],
])
def test_browser_websocket_rejects_missing_or_ambiguous_protocols(boundary, protocols):
    client, _, state = boundary
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws", subprotocols=protocols, headers={"Origin": "https://open-teleset.site"}
        ):
            pass
    assert not state["requests"]


def test_browser_websocket_revalidates_project_access_before_update(boundary):
    client, token, state = boundary
    with client.websocket_connect(
        "/ws", subprotocols=[security.WS_PROTOCOL, security.WS_TOKEN_PREFIX + token()],
        headers={"Origin": "https://open-teleset.site"},
    ) as ws:
        ws.send_text("status")
        assert ws.receive_json() == {"private_status": True}
        state["assignments"] = []
        ws.send_text("status")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_dashboard_sends_current_tokens_and_cleans_up_browser_sessions():
    """Run the actual inline dashboard JavaScript with a mocked browser/provider."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the browser JavaScript test")
    dashboard = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"
    result = subprocess.run([node, "-e", r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
let options, requestHandler, responseError, authCallback, denyAccess = false;
let passwordError = null, refreshError = null, passwordUpdates = 0, sessionRefreshes = 0;
let operationResult = {success: false, sent: 1, added: 1, failed: 1, total: 2};
let session = { access_token: 'first.test.token', user: { id: 'test-user', email: 'test@example.com' } };
const sockets = [], calls = [], deferred = [];
const api = {
    defaults: { headers: { common: {} } },
    interceptors: {
        request: { use(fn) { requestHandler = fn; } },
        response: { use(ok, error) { responseError = error; return 0; }, eject() {} },
    },
    async get(url) {
        calls.push(await requestHandler({url, headers: {}}));
        if (denyAccess) throw { response: { status: 403 } };
        return { data: { id: 'test-user', role: 'admin' } };
    },
    async post(url, data) {
        calls.push({...await requestHandler({url, headers: {}}), data});
        return {data: operationResult};
    },
};
class Socket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; this.closed = false; sockets.push(this); }
    close() { this.closed = true; }
}
const provider = {
    auth: {
        async getSession() { return {data: {session}}; },
        onAuthStateChange(fn) { authCallback = fn; return {data: {subscription: {unsubscribe() {}}}}; },
        async signOut() { session = null; return {}; },
        async updateUser(value) {
            assert.equal(value.password, 'new-password-123');
            passwordUpdates++;
            authCallback('USER_UPDATED', session);
            return {error: passwordError};
        },
        async refreshSession() {
            sessionRefreshes++;
            if (refreshError) return {error: refreshError};
            session = {...session, access_token: 'recovery.refreshed.token'};
            authCallback('TOKEN_REFRESHED', session);
            return {data: {session}};
        },
    },
    from(table) {
        assert.equal(table, 'profiles');
        return {select(columns) {
            assert.equal(columns, 'display_name');
            return {eq() { return {async single() { return {data: {display_name: 'Test'}}; }}; }};
        }};
    },
};
const context = {
    window: { OPEN_TELESET_CONFIG: {apiBase: 'https://api.example.com', supabaseUrl: 'https://test.supabase.co', supabaseAnonKey: 'test'}, location: {origin: 'https://open-teleset.site'} },
    supabase: {createClient() {return provider;}},
    axios: {create(config) { assert.equal(config.baseURL, 'https://api.example.com'); return api; }},
    Vue: {createApp(value) { options = value; return {mount() {}}; }},
    URL, WebSocket: Socket, console,
    setTimeout(fn) { deferred.push(fn); return deferred.length; }, clearTimeout() {},
    setInterval() {return 1;}, clearInterval() {},
};
for (const script of scripts) vm.runInNewContext(script[1], context);
const app = options.data();
for (const [name, fn] of Object.entries(options.methods)) app[name] = fn.bind(app);
let dashboardLoads = 0;
app.loadDashboardData = () => { dashboardLoads++; };
(async () => {
    await app.initAuth();
    await new Promise(setImmediate);
    assert.equal(app.accessReady, true);
    assert.equal(app.verifiedRole, 'admin');
    assert.equal(dashboardLoads, 1);
    assert.equal(calls[0].headers.Authorization, 'Bearer first.test.token');
    assert.equal(sockets[0].url, 'wss://api.example.com/ws');
    assert.deepEqual(Array.from(sockets[0].protocols), ['open-teleset.v1', 'bearer.first.test.token']);
    assert.equal(new URL(sockets[0].url).search, '');
    session = {...session, access_token: 'refreshed.test.token'};
    const fresh = await requestHandler({url: '/api/accounts', headers: {}});
    assert.equal(fresh.headers.Authorization, 'Bearer refreshed.test.token');
    await assert.rejects(requestHandler({url: 'https://attacker.example/api/accounts', headers: {}}));
    authCallback('TOKEN_REFRESHED', session);
    while (deferred.length) await deferred.shift()();
    await new Promise(setImmediate);
    assert.equal(sockets[0].closed, true);
    assert.equal(sockets.at(-1).protocols[1], 'bearer.refreshed.test.token');
    await assert.rejects(responseError({response: {status: 403, data: {detail: 'Outreach denied: suppressed'}}}));
    assert.equal(app.accessReady, true);
    assert.equal(app.adder.limit, 10);
    assert.equal(app.newSchedule.interval, 3000);
    Object.assign(app.adder, {accountId: 'account', destTarget: 'group', usernames: '111\n222', approvalId: 'member-approval'});
    await app.runAdder();
    assert.equal(calls.at(-1).data.approval_id, 'member-approval');
    assert.deepEqual(Array.from(calls.at(-1).data.usernames), ['111', '222']);
    assert.equal(calls.at(-1).data.source_target, undefined);
    assert.match(app.adder.resultMsg, /Incomplete or denied/);
    Object.assign(app.bulksend, {accountId: 'account', targets: '111\n222', message: 'Hello', approvalId: 'send-approval'});
    await app.runBulkSend();
    assert.equal(calls.at(-1).data.approval_id, 'send-approval');
    assert.match(app.bulksend.resultMsg, /Incomplete or denied/);
    const beforeInvalid = calls.length;
    app.bulksend.approvalId = '';
    await app.runBulkSend();
    assert.equal(calls.length, beforeInvalid);
    assert.match(app.bulksend.resultMsg, /approval reference/);
    operationResult = {success: true};
    app.loadSchedules = async () => {};
    Object.assign(app.newSchedule, {selectedAccount: 'account', selectedFriends: [111], approvalId: 'schedule-approval'});
    await app.addSchedule();
    assert.equal(calls.at(-1).data.approval_id, 'schedule-approval');
    assert.equal(calls.at(-1).data.interval, 3000);
    const beforeRecovery = calls.length;
    authCallback('PASSWORD_RECOVERY', session);
    assert.equal(app.passwordRecovery, true);
    assert.equal(app.accessReady, false);
    assert.equal(sockets.at(-1).closed, true);
    await app.applySession(session);
    assert.equal(calls.length, beforeRecovery);
    app.newPassword = app.confirmPassword = 'short';
    await app.updatePassword();
    assert.match(app.authError, /at least 8/);
    app.newPassword = 'new-password-123'; app.confirmPassword = 'different-password';
    await app.updatePassword();
    assert.match(app.authError, /do not match/);
    assert.equal(passwordUpdates, 0);
    app.confirmPassword = app.newPassword;
    passwordError = {message: 'Password rejected by provider policy'};
    await app.updatePassword();
    assert.match(app.authError, /provider policy/);
    assert.equal(app.passwordRecovery, true);
    assert.equal(sessionRefreshes, 0);
    passwordError = null;
    denyAccess = true;
    await app.updatePassword();
    assert.equal(sessionRefreshes, 1);
    assert.equal(app.passwordRecovery, false);
    assert.equal(app.newPassword, '');
    assert.equal(app.confirmPassword, '');
    assert.match(app.authSuccess, /Password updated successfully/);
    assert.equal(app.accessReady, false); // Password recovery does not grant administrator access.
    assert.match(app.accessError, /Administrator access is required/);
    assert.equal(calls.at(-1).headers.Authorization, 'Bearer recovery.refreshed.token');
    while (deferred.length) await deferred.shift()();
    await new Promise(setImmediate);
    authCallback('PASSWORD_RECOVERY', session);
    const recoverySession = session;
    session = null;
    app.newPassword = app.confirmPassword = 'new-password-123';
    const beforeExpired = passwordUpdates;
    await app.updatePassword();
    assert.match(app.authError, /reset session has expired/);
    assert.equal(passwordUpdates, beforeExpired);
    session = recoverySession;
    refreshError = {message: 'Refresh rejected'};
    await app.updatePassword();
    assert.equal(app.passwordRecovery, false);
    assert.equal(app.user, null);
    assert.match(app.authSuccess, /Password updated. Sign in/);
    assert.equal(app.authSubmitting, false);
    assert.equal(api.defaults.headers.common.Authorization, undefined);
    session = recoverySession;
    refreshError = null;
    denyAccess = true;
    await app.applySession(session);
    assert.equal(app.accessReady, false);
    assert.match(app.accessError, /Administrator access is required/);
    assert.equal(sockets.at(-1).closed, true);
    await app.signOut();
    assert.equal(app.user, null);
    assert.equal(api.defaults.headers.common.Authorization, undefined);
    await assert.rejects(requestHandler({url: '/api/accounts', headers: {}}));
    console.log('browser auth checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
''', str(dashboard)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "browser auth checks passed" in result.stdout
