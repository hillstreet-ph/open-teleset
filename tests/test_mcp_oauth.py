"""OAuth sign-in for a custom MCP server.

The dashboard is a public OAuth client: it discovers the authorization server from the
MCP endpoint, registers itself where the server allows it, and keeps every token in the
encrypted store. The panel is told whether a sign-in exists and when it expires, never
what the token is.
"""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

import dashboard_features as features

ISSUER = "https://auth.example.com"
SERVER = "https://mcp.example.com/mcp"


class _User:
    id = "operator-1"
    role = "admin"


class _State:
    user = _User()


class _Request:
    state = _State()
    base_url = "https://open-teleset.site/"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    yield


class _Idp:
    """A stand-in authorization server that records what the dashboard asked for."""

    def __init__(self, *, registrable=True, expires_in=3600, token_access="tok-access-value"):
        self.calls = []
        self.registrable = registrable
        self.expires_in = expires_in
        self.token_access = token_access
        self.token_bodies = []
        self.refuse_token = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": [ISSUER]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "registration_endpoint": (f"{ISSUER}/register" if self.registrable else None),
                    "scopes_supported": ["mcp:tools", "profile"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register":
            body = json.loads(request.content)
            if not body.get("redirect_uris"):
                return httpx.Response(400, json={"error": "redirect_uris required"})
            return httpx.Response(200, json={"client_id": "client-from-registration"})
        if path == "/token":
            self.token_bodies.append(parse_qs(request.content.decode()))
            if self.refuse_token:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "expired tok-secret-value"}
                )
            return httpx.Response(
                200,
                json={
                    "access_token": self.token_access,
                    "refresh_token": "tok-refresh-value",
                    "expires_in": self.expires_in,
                    "scope": "mcp:tools",
                },
            )
        return httpx.Response(404, json={"error": f"unhandled {path}"})

    @property
    def urls(self):
        return [str(request.url) for request in self.calls]


@pytest.fixture
def idp(monkeypatch):
    server = _Idp()
    monkeypatch.setattr(features, "_OAUTH_TRANSPORT", httpx.MockTransport(server.handler))
    return server


def _secrets():
    return features.read_secret("integration_oauth", {})


async def _save_server(**overrides):
    """Put one custom MCP server on record and return its id."""
    fields = {"name": "Example MCP", "base_url": SERVER, "transport": "http", "auth_mode": "oauth"}
    fields.update(overrides)
    saved = await features.save_integration(features.IntegrationInput(**fields), _Request())
    return saved["integration"]["id"]


async def _signed_in(integration_id):
    """Run the browser part of the flow up to, but not including, the code exchange."""
    start = await features.start_mcp_oauth(integration_id, _Request())
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    await features.finish_mcp_oauth(
        features.OAuthCallback(code="an-auth-code", state=state), _Request()
    )
    return state


def _query(start):
    return parse_qs(urlsplit(start["authorization_url"]).query)


# ------------------------------------------------------------------- discovery


@pytest.mark.asyncio
async def test_signin_uses_the_metadata_the_server_publishes(idp):
    integration_id = await _save_server()
    start = await features.start_mcp_oauth(integration_id, _Request())

    assert urlsplit(start["authorization_url"])._replace(query="").geturl() == f"{ISSUER}/authorize"
    query = _query(start)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-from-registration"]
    assert query["redirect_uri"] == ["https://open-teleset.site/dashboard"]
    # no scopes typed, so none asked for — the server's full list is not a shopping list
    assert "scope" not in query
    assert start["scopes_supported"] == ["mcp:tools", "profile"]
    # the MCP endpoint is asked who its authorization server is, and that server is asked
    # for its endpoints — the dashboard never guesses an OAuth URL
    assert "https://mcp.example.com/.well-known/oauth-protected-resource" in idp.urls
    assert f"{ISSUER}/.well-known/oauth-authorization-server" in idp.urls


@pytest.mark.asyncio
async def test_the_scopes_asked_for_are_the_ones_the_operator_wrote(idp):
    integration_id = await _save_server(scopes="mcp:tools")
    start = await features.start_mcp_oauth(integration_id, _Request())

    assert _query(start)["scope"] == ["mcp:tools"]
    # the panel reads its own wording back, so editing a server does not lose the scopes
    listing = await features.list_integrations(_Request())
    assert listing["integrations"][0]["scopes"] == "mcp:tools"
    assert listing["integrations"][0]["granted_scopes"] == ""


@pytest.mark.asyncio
async def test_the_challenge_is_s256_of_a_verifier_the_browser_never_sees(idp):
    integration_id = await _save_server()
    start = await features.start_mcp_oauth(integration_id, _Request())
    query = _query(start)

    verifier = _secrets()[integration_id]["pending"]["verifier"]
    digest = hashlib.sha256(verifier.encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).decode().rstrip("=")]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [_secrets()[integration_id]["pending"]["state"]]
    assert verifier not in json.dumps(start, default=str)


@pytest.mark.asyncio
async def test_a_server_that_offers_no_registration_needs_a_client_id(idp):
    idp.registrable = False
    integration_id = await _save_server()
    with pytest.raises(features.HTTPException) as caught:
        await features.start_mcp_oauth(integration_id, _Request())
    assert caught.value.status_code == 400
    assert "Client ID" in caught.value.detail

    # typed in by hand, the flow goes ahead without registering
    await features.save_integration(
        features.IntegrationInput(
            id=integration_id, name="Example MCP", base_url=SERVER, transport="http",
            auth_mode="oauth", client_id="my-manual-client",
        ),
        _Request(),
    )
    start = await features.start_mcp_oauth(integration_id, _Request())
    assert _query(start)["client_id"] == ["my-manual-client"]
    assert not any(request.url.path == "/register" for request in idp.calls)


@pytest.mark.asyncio
async def test_the_dashboard_registers_itself_as_a_public_client(idp):
    integration_id = await _save_server()
    await features.start_mcp_oauth(integration_id, _Request())

    registration = next(request for request in idp.calls if request.url.path == "/register")
    body = json.loads(registration.content)
    assert body["redirect_uris"] == ["https://open-teleset.site/dashboard"]
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
async def test_metadata_pointing_inside_the_network_is_never_fetched(idp, monkeypatch):
    """Discovery answers are not ours to trust, so they cannot reach a private host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["http://169.254.169.254"]})
        return idp.handler(request)

    monkeypatch.setattr(features, "_OAUTH_TRANSPORT", httpx.MockTransport(handler))
    integration_id = await _save_server()
    with pytest.raises(features.HTTPException) as caught:
        await features.start_mcp_oauth(integration_id, _Request())
    assert caught.value.status_code == 502
    assert not any("169.254.169.254" in url for url in idp.urls)


@pytest.mark.asyncio
async def test_a_server_that_advertises_no_oauth_says_so(monkeypatch):
    monkeypatch.setattr(features, "_OAUTH_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(404)))
    integration_id = await _save_server()
    with pytest.raises(features.HTTPException) as caught:
        await features.start_mcp_oauth(integration_id, _Request())
    assert caught.value.status_code == 502
    assert "OAuth" in caught.value.detail


# -------------------------------------------------------------------- callback


@pytest.mark.asyncio
async def test_the_code_is_exchanged_and_only_the_expiry_comes_back(idp):
    integration_id = await _save_server()
    start = await features.start_mcp_oauth(integration_id, _Request())
    verifier = _secrets()[integration_id]["pending"]["verifier"]

    result = await features.finish_mcp_oauth(
        features.OAuthCallback(code="an-auth-code", state=_query(start)["state"][0]), _Request()
    )

    form = idp.token_bodies[-1]
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["an-auth-code"]
    assert form["code_verifier"] == [verifier]
    assert form["redirect_uri"] == ["https://open-teleset.site/dashboard"]
    assert result["integration"]["id"] == integration_id
    assert result["integration"]["authorized"] is True
    assert result["integration"]["granted_scopes"] == "mcp:tools"
    assert result["integration"]["expires_at"]
    assert "tok-access-value" not in json.dumps(result, default=str)
    assert "tok-refresh-value" not in json.dumps(result, default=str)


@pytest.mark.asyncio
async def test_a_code_lands_on_the_server_whose_state_it_carries(idp):
    first = await _save_server(id="mcp-first", name="First MCP")
    second = await _save_server(id="mcp-second", name="Second MCP")
    start = await features.start_mcp_oauth(second, _Request())

    result = await features.finish_mcp_oauth(
        features.OAuthCallback(code="an-auth-code", state=_query(start)["state"][0]), _Request()
    )

    assert result["integration"]["id"] == second
    listing = await features.list_integrations(_Request())
    by_id = {item["id"]: item for item in listing["integrations"]}
    assert by_id[second]["authorized"] is True
    assert by_id[first]["authorized"] is False


@pytest.mark.asyncio
async def test_a_state_that_is_not_the_one_we_started_is_refused(idp):
    integration_id = await _save_server()
    await features.start_mcp_oauth(integration_id, _Request())

    with pytest.raises(features.HTTPException) as caught:
        await features.finish_mcp_oauth(
            features.OAuthCallback(code="an-auth-code", state="guessed-state"), _Request()
        )
    assert caught.value.status_code == 409
    assert _secrets()[integration_id].get("tokens") is None


@pytest.mark.asyncio
async def test_an_expired_signin_attempt_cannot_be_finished(idp):
    integration_id = await _save_server()
    start = await features.start_mcp_oauth(integration_id, _Request())
    state = _query(start)["state"][0]

    secrets = _secrets()
    secrets[integration_id]["pending"]["at"] = time.time() - features.OAUTH_PENDING_SECONDS - 1
    features.write_secret("integration_oauth", secrets)

    with pytest.raises(features.HTTPException) as caught:
        await features.finish_mcp_oauth(features.OAuthCallback(code="an-auth-code", state=state), _Request())
    assert caught.value.status_code == 409
    assert "expired" in caught.value.detail.lower()


@pytest.mark.asyncio
async def test_one_signin_attempt_is_spent_whether_or_not_it_works(idp):
    integration_id = await _save_server()
    state = await _signed_in(integration_id)

    with pytest.raises(features.HTTPException) as caught:
        await features.finish_mcp_oauth(features.OAuthCallback(code="an-auth-code", state=state), _Request())
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_a_refused_code_exchange_does_not_echo_the_server(idp):
    integration_id = await _save_server()
    start = await features.start_mcp_oauth(integration_id, _Request())
    idp.refuse_token = True

    with pytest.raises(features.HTTPException) as caught:
        await features.finish_mcp_oauth(
            features.OAuthCallback(code="an-auth-code", state=_query(start)["state"][0]), _Request()
        )
    assert caught.value.status_code == 502
    assert "tok-secret-value" not in caught.value.detail
    assert "invalid_grant" not in caught.value.detail


# ---------------------------------------------------------- using and refreshing


@pytest.mark.asyncio
async def test_a_signed_in_server_carries_its_bearer(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)

    headers = await features.integration_auth_headers(integration_id)
    assert headers["Authorization"] == "Bearer tok-access-value"


@pytest.mark.asyncio
async def test_a_token_about_to_expire_is_renewed_before_it_is_used(idp):
    idp.expires_in = 30
    integration_id = await _save_server()
    await _signed_in(integration_id)

    headers = await features.integration_auth_headers(integration_id)
    assert headers["Authorization"].startswith("Bearer ")
    form = idp.token_bodies[-1]
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["tok-refresh-value"]


@pytest.mark.asyncio
async def test_a_server_that_will_not_renew_asks_for_a_new_signin(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)
    secrets = _secrets()
    secrets[integration_id]["tokens"]["refresh_token"] = ""
    features.write_secret("integration_oauth", secrets)

    with pytest.raises(features.HTTPException) as caught:
        await features.refresh_mcp_oauth(integration_id)
    assert caught.value.status_code == 409
    assert "Sign in again" in caught.value.detail


@pytest.mark.asyncio
async def test_refresh_reports_what_it_tried_in_plain_words(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)
    idp.refuse_token = True

    with pytest.raises(features.HTTPException) as caught:
        await features.refresh_mcp_oauth(integration_id)
    assert caught.value.status_code == 502
    assert "Sign in again" in caught.value.detail


@pytest.mark.asyncio
async def test_signing_out_keeps_the_server_and_drops_its_tokens(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)

    result = await features.disconnect_mcp_oauth(integration_id)
    assert result["integration"]["authorized"] is False
    assert "tok-access-value" not in json.dumps(_secrets(), default=str)
    # the registration survives, so signing in again does not register a second client
    assert _secrets()[integration_id]["client_id"] == "client-from-registration"


@pytest.mark.asyncio
async def test_removing_the_server_removes_its_signin(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)

    await features.delete_integration(integration_id)
    assert _secrets() == {}


@pytest.mark.asyncio
async def test_a_token_server_is_not_offered_an_oauth_flow(idp):
    integration_id = await _save_server(auth_mode="token", api_key="k")
    with pytest.raises(features.HTTPException) as caught:
        await features.start_mcp_oauth(integration_id, _Request())
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_the_list_shows_the_login_state_and_the_address_to_whitelist(idp):
    await _save_server()

    listing = await features.list_integrations(_Request())
    assert listing["redirect_uri"] == "https://open-teleset.site/dashboard"
    item = listing["integrations"][0]
    assert item["auth_mode"] == "oauth"
    assert item["authorized"] is False
    assert set(item) == {
        "id", "name", "base_url", "transport", "has_token", "created_at",
        "auth_mode", "authorized", "expires_at", "scopes", "granted_scopes", "has_client",
    }


@pytest.mark.asyncio
async def test_switching_a_server_back_to_a_token_ends_its_signin(idp):
    integration_id = await _save_server()
    await _signed_in(integration_id)

    await features.save_integration(
        features.IntegrationInput(
            id=integration_id, name="Example MCP", base_url=SERVER, transport="http",
            auth_mode="token", api_key="a-static-key",
        ),
        _Request(),
    )
    assert _secrets() == {}
    listing = await features.list_integrations(_Request())
    assert listing["integrations"][0]["auth_mode"] == "token"
