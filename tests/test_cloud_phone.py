"""Cloud Phone: the WebDriver plumbing and the operator-facing contract.

What matters here is that the panel drives a real device and that nothing the
operator typed - a password, a one-time code - or the provider's own error text
ever comes back out of the API or into a log.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import cloud_phone
from open_teleset import operator_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({"cloud_phone_enabled": True})
    cloud_phone._sessions.clear()
    cloud_phone._locks.clear()
    monkeypatch.setattr(cloud_phone.log_manager, "add_log", lambda *a, **k: None)
    yield
    cloud_phone._sessions.clear()
    cloud_phone._locks.clear()


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(cloud_phone.router)
    return TestClient(app)


@pytest.fixture
def anonymous():
    app = FastAPI()
    app.include_router(cloud_phone.router)
    return TestClient(app)


class FakeGateway:
    """Records every call and answers with the WebDriver shapes the panel reads."""

    def __init__(self):
        self.calls = []
        self.status = httpx.Response(200, json={"value": {"ready": True, "build": "2.5.1"}})
        self.session = httpx.Response(200, json={"value": {"sessionId": "sess-1"}})
        self.screenshot = httpx.Response(200, json={"value": "aGVsbG8="})
        self.ok = httpx.Response(200, json={"value": None})
        self.fail = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"method": request.method, "path": request.url.path,
                           "headers": dict(request.headers),
                           "body": json.loads(request.content or b"{}")})
        if self.fail is not None:
            return self.fail
        if request.url.path.endswith("/status"):
            return self.status
        if request.url.path.endswith("/screenshot"):
            return self.screenshot
        if request.method == "POST" and request.url.path.endswith("/session"):
            return self.session
        return self.ok


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(cloud_phone, "_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def _configure(client, endpoint="https://phone.example.com/wd/hub", token="tok",
               package="com.telegram.messenger"):
    return client.put("/api/cloud-phone/config", json={
        "endpoint": endpoint, "token": token, "device": "Pixel 7",
        "udid": "emulator-5554", "package": package,
    })


# ------------------------------------------------------------------ the switch


def test_the_panel_is_off_until_the_operator_switches_it_on(client):
    operator_store.set_features({"cloud_phone_enabled": False})
    refused = client.post("/api/cloud-phone/connect")
    assert refused.status_code == 403


def test_an_anonymous_caller_is_refused(anonymous):
    assert anonymous.get("/api/cloud-phone/config").status_code == 403


def test_addresses_need_the_encryption_key(client, monkeypatch):
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY")
    refused = client.put("/api/cloud-phone/config", json={"endpoint": "https://phone.example.com"})
    assert refused.status_code == 503


# --------------------------------------------------------------------- config


def test_the_token_never_comes_back_out(client, gateway):
    saved = _configure(client, token="tok-s3cret-value").json()
    assert "tok-s3cret-value" not in json.dumps(saved)
    assert saved["has_token"] is True
    assert saved["configured"] is True
    assert saved["host"] == "phone.example.com"
    assert saved["device"] == "Pixel 7"
    assert saved["session"]["connected"] is False


def test_a_blank_token_keeps_the_stored_one(client, gateway):
    _configure(client)
    saved = client.put("/api/cloud-phone/config",
                       json={"endpoint": "https://phone.example.com/wd/hub"}).json()
    assert saved["has_token"] is True


def test_clearing_forgets_the_record(client, gateway):
    _configure(client)
    cleared = client.put("/api/cloud-phone/config", json={"clear": True}).json()
    assert cleared["configured"] is False
    assert cleared["has_token"] is False


@pytest.mark.parametrize("endpoint", [
    "http://phone.example.com/wd/hub",
    "https://127.0.0.1/wd/hub",
    "https://user:secret@phone.example.com/wd/hub",
])
def test_only_a_public_https_address_is_accepted(client, gateway, endpoint):
    refused = _configure(client, endpoint=endpoint)
    assert refused.status_code == 400
    assert "secret" not in refused.text


def test_a_bad_package_name_is_refused(client, gateway):
    assert _configure(client, package="not a package!").status_code == 400


# -------------------------------------------------------------------- session


def test_connect_opens_one_session_and_reuses_it(client, gateway):
    _configure(client)
    first = client.post("/api/cloud-phone/connect").json()
    second = client.post("/api/cloud-phone/connect").json()
    assert first["session"]["connected"] is True
    assert second["reused"] is True
    creates = [c for c in gateway.calls if c["method"] == "POST" and c["path"].endswith("/session")]
    assert len(creates) == 1
    capabilities = creates[0]["body"]["capabilities"]["alwaysMatch"]
    assert capabilities["platformName"] == "Android"
    assert capabilities["appium:deviceName"] == "Pixel 7"
    assert capabilities["appium:udid"] == "emulator-5554"
    assert capabilities["appium:noReset"] is True


def test_the_stored_token_travels_as_a_bearer(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/check")
    assert gateway.calls[-1]["headers"].get("authorization") == "Bearer tok"


def test_connecting_without_a_session_id_is_an_error(client, gateway):
    _configure(client)
    gateway.session = httpx.Response(200, json={"value": {}})
    failed = client.post("/api/cloud-phone/connect")
    assert failed.status_code == 502
    assert "session" in failed.json()["detail"].lower()


def test_a_refused_gateway_does_not_echo_its_own_text(client, gateway):
    _configure(client)
    gateway.fail = httpx.Response(500, json={"value": {
        "error": "unknown error",
        "message": "could not find https://phone.example.com/secret-page?token=abc",
    }})
    failed = client.post("/api/cloud-phone/check")
    assert failed.status_code == 502
    assert "secret-page" not in failed.text
    assert "token=abc" not in failed.text
    assert "unknown error" in failed.json()["detail"]


def test_a_gateway_that_does_not_answer_is_a_bad_gateway(client, monkeypatch):
    _configure(client)

    def boom(request):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(cloud_phone, "_TRANSPORT", httpx.MockTransport(boom))
    assert client.post("/api/cloud-phone/check").status_code == 502


def test_changing_the_address_releases_the_open_session(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    _configure(client, endpoint="https://other.example.com/wd/hub")
    deletes = [c for c in gateway.calls if c["method"] == "DELETE"]
    assert deletes and deletes[-1]["path"].endswith("/session/sess-1")
    assert cloud_phone._session("operator-1") is None


def test_disconnect_releases_the_session(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    state = client.post("/api/cloud-phone/disconnect").json()
    assert state["session"]["connected"] is False


def test_screen_returns_the_frame(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    frame = client.get("/api/cloud-phone/screen").json()
    assert frame["image"] == "aGVsbG8="
    assert gateway.calls[-1]["path"].endswith("/session/sess-1/screenshot")


def test_a_control_before_connect_says_so(client, gateway):
    _configure(client)
    refused = client.post("/api/cloud-phone/tap", json={"x": 10, "y": 20})
    assert refused.status_code == 409


# ------------------------------------------------------------------- controls


def test_tap_moves_then_touches_the_two_coordinates(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    assert client.post("/api/cloud-phone/tap", json={"x": 120, "y": 640}).status_code == 200
    assert gateway.calls[-1]["path"].endswith("/session/sess-1/actions")
    actions = gateway.calls[-1]["body"]["actions"][0]["actions"]
    assert actions[0] == {"type": "pointerMove", "duration": 0, "x": 120, "y": 640}
    assert [a["type"] for a in actions[1:]] == ["pointerDown", "pause", "pointerUp"]


def test_swipe_carries_its_duration_and_end_point(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    client.post("/api/cloud-phone/swipe",
                json={"x1": 10, "y1": 700, "x2": 10, "y2": 200, "duration_ms": 450})
    actions = gateway.calls[-1]["body"]["actions"][0]["actions"]
    move = [a for a in actions if a["type"] == "pointerMove"][-1]
    assert move == {"type": "pointerMove", "duration": 450, "x": 10, "y": 200}


@pytest.mark.parametrize("overrides", [
    {"duration_ms": 10},
    {"duration_ms": 5000},
    {"x1": -1},
    {"y2": 20000},
])
def test_swipe_bounds_are_enforced_before_the_device(gateway, overrides):
    body = {"x1": 1, "y1": 1, "x2": 2, "y2": 2}
    body.update(overrides)
    with pytest.raises(ValidationError):
        cloud_phone.SwipeInput(**body)


def test_text_is_typed_one_key_at_a_time_and_never_echoed(client, gateway, monkeypatch):
    recorded = []
    monkeypatch.setattr(cloud_phone.log_manager, "add_log", lambda *a, **k: recorded.append(a))
    _configure(client)
    client.post("/api/cloud-phone/connect")
    result = client.post("/api/cloud-phone/text", json={"text": "s3cret"}).json()
    actions = gateway.calls[-1]["body"]["actions"][0]
    assert actions["type"] == "key"
    assert [a["value"] for a in actions["actions"] if a["type"] == "keyDown"] == list("s3cret")
    assert result == {"success": True, "characters": 6}
    assert all("s3cret" not in str(item) for item in recorded)


def test_an_overlong_text_is_refused_before_the_device(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    refused = client.post("/api/cloud-phone/text",
                          json={"text": "x" * (cloud_phone.MAX_TEXT + 1)})
    assert refused.status_code == 422


@pytest.mark.parametrize("key,keycode", [("home", 3), ("back", 4), ("recents", 187)])
def test_a_button_press_is_an_android_keycode(client, gateway, key, keycode):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    client.post("/api/cloud-phone/key", json={"key": key})
    assert gateway.calls[-1]["path"].endswith("/appium/device/press_keycode")
    assert gateway.calls[-1]["body"] == {"keycode": keycode}


def test_launch_and_stop_use_the_package(client, gateway):
    _configure(client)
    client.post("/api/cloud-phone/connect")
    client.post("/api/cloud-phone/app/launch", json={"package": "com.telegram.messenger"})
    assert gateway.calls[-1]["path"].endswith("/appium/device/activate_app")
    assert gateway.calls[-1]["body"] == {"appId": "com.telegram.messenger"}
    client.post("/api/cloud-phone/app/stop", json={"package": "com.telegram.messenger"})
    assert gateway.calls[-1]["path"].endswith("/appium/device/terminate_app")


@pytest.mark.parametrize("package", ["com", "@bad", "com..x", "com.telegram messenger"])
def test_a_bad_package_is_refused_before_the_device(gateway, package):
    with pytest.raises(ValidationError):
        cloud_phone.AppInput(package=package)
