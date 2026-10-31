"""Cloud Browser: DevTools plumbing and the operator-facing contract.

The security contract matters more than the features here: the stored address and
token, the text typed into a login form and the Cloud Phone code must never come
back out in a response or a log.
"""

import asyncio
import json

import base64
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import cloud_browser
from open_teleset import operator_store
from open_teleset.cdp_client import CdpClient, CdpError


class _User:
    id = "operator-1"
    role = "admin"


class _State:
    user = _User()


class _Request:
    state = _State()


class _AnonRequest:
    state = _State()
    state.user = None


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({"cloud_browser_enabled": True})
    yield


@pytest.fixture(autouse=True)
def quiet_logs(monkeypatch):
    monkeypatch.setattr(cloud_browser.log_manager, "add_log", lambda *a, **k: None)


# ---------------------------------------------------------------- DevTools wire


class FakeSocket:
    """Answers DevTools commands the way a hosted browser would."""

    def __init__(self, handler=None):
        self._handler = handler or self._default
        self._queue = asyncio.Queue()
        self.frames = []
        self.closed = False

    @staticmethod
    def _default(payload):
        method = payload["method"]
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"targetId": "t1", "type": "page", "title": "Telegram", "url": "https://web.telegram.org/"},
                {"targetId": "img1", "type": "image", "title": "", "url": ""},
            ]}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Page.captureScreenshot":
            return {"data": "QUJDRA=="}
        if method == "Browser.getVersion":
            return {"product": "Chrome/130.0", "revision": "@44f2b"}
        return {}

    async def send_str(self, data):
        payload = json.loads(data)
        self.frames.append(payload)
        reply = self._handler(payload)
        if reply is None:
            return
        if "error" in reply:
            frame = {"id": payload["id"], "error": reply["error"]}
        else:
            frame = {"id": payload["id"], "result": reply}
        await self._queue.put(json.dumps(frame))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


async def _client(socket):
    client = CdpClient(socket)

    async def read():
        async for raw in socket:
            client._dispatch(json.loads(raw))

    client._reader = asyncio.create_task(read())
    return client


@pytest.mark.asyncio
async def test_connect_forwards_the_access_token_and_cleans_up(monkeypatch):
    import aiohttp

    captured = {}

    class FakeSession:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        async def ws_connect(self, url, headers=None, max_msg_size=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeSocket()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    client = await CdpClient.connect("wss://browser.example/session/abc", {"Authorization": "Bearer tok-1"})
    assert captured["url"] == "wss://browser.example/session/abc"
    assert captured["headers"] == {"Authorization": "Bearer tok-1"}
    assert (await client.version())["product"] == "Chrome/130.0"
    await client.close()
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_unreachable_endpoint_is_reported_without_the_address(monkeypatch):
    import aiohttp

    class BoomSession:
        def __init__(self, timeout=None):
            pass

        async def ws_connect(self, url, headers=None, max_msg_size=None):
            raise aiohttp.ClientError("certificate failed wss://tok-secret@browser.example/x")

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", BoomSession)
    with pytest.raises(CdpError) as caught:
        await CdpClient.connect("wss://tok-secret@browser.example/x")
    assert "tok-secret" not in str(caught.value)
    assert "browser.example" not in str(caught.value)


@pytest.mark.asyncio
async def test_pages_reports_only_open_pages():
    socket = FakeSocket()
    client = await _client(socket)
    pages = await client.pages()
    await client.close()
    assert pages == [{"target_id": "t1", "title": "Telegram", "url": "https://web.telegram.org/"}]


@pytest.mark.asyncio
async def test_screenshot_attaches_to_the_page_first():
    socket = FakeSocket()
    client = await _client(socket)
    image = await client.screenshot("t1")
    await client.close()
    assert image == "QUJDRA=="
    sent = [frame["method"] for frame in socket.frames]
    assert sent == ["Target.attachToTarget", "Page.enable", "Page.captureScreenshot"]
    assert socket.frames[0]["params"]["targetId"] == "t1"
    assert socket.frames[2]["sessionId"] == "session-1"


@pytest.mark.asyncio
async def test_press_key_sends_down_and_up():
    socket = FakeSocket()
    client = await _client(socket)
    await client.press_key("t1", "Enter")
    await client.close()
    keys = [frame for frame in socket.frames if frame["method"] == "Input.dispatchKeyEvent"]
    assert [frame["params"]["type"] for frame in keys] == ["keyDown", "keyUp"]
    assert keys[0]["params"]["windowsVirtualKeyCode"] == 13


@pytest.mark.asyncio
async def test_unknown_key_is_refused_before_the_wire():
    socket = FakeSocket()
    client = await _client(socket)
    with pytest.raises(CdpError):
        await client.press_key("t1", "F13")
    await client.close()


@pytest.mark.asyncio
async def test_refused_command_does_not_leak_the_vendor_message():
    socket = FakeSocket(lambda payload: {"error": {"message": "denied for token hunter2"}} if payload["method"] == "Target.getTargets" else {})
    client = await _client(socket)
    with pytest.raises(CdpError) as caught:
        await client.pages()
    await client.close()
    assert "hunter2" not in str(caught.value)


@pytest.mark.asyncio
async def test_command_that_never_answers_times_out():
    socket = FakeSocket(lambda payload: None)
    client = await _client(socket)
    with pytest.raises(CdpError):
        await client.call("Page.navigate", {"url": "https://x.example"}, timeout=0.05)
    await client.close()


# -------------------------------------------------------------------- operator API


@pytest.mark.asyncio
async def test_anonymous_requests_are_refused():
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.get_config(_AnonRequest())
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_disabled_feature_blocks_every_command():
    operator_store.set_features({"cloud_browser_enabled": False})
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.check_connection(_Request())
    assert caught.value.status_code == 403
    assert "switched off" in caught.value.detail.lower()


@pytest.mark.asyncio
async def test_endpoint_must_be_a_devtools_address():
    for bad in ["https://web.telegram.org", "wss://user:pass@browser.example/x", "not a url", "ws://"]:
        with pytest.raises(HTTPException) as caught:
            await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint=bad), _Request())
        assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_config_never_returns_the_address_or_token():
    saved = await cloud_browser.save_config(
        cloud_browser.ConfigInput(endpoint=" wss://browser.example/session/abc ", token="tok-secret-value"),
        _Request(),
    )
    assert saved["configured"] is True
    assert saved["host"] == "browser.example"
    assert saved["has_token"] is True
    assert "tok-secret-value" not in json.dumps(saved)
    assert "session/abc" not in json.dumps(saved)
    loaded = await cloud_browser.get_config(_Request())
    assert "tok-secret-value" not in json.dumps(loaded)
    assert operator_store.read_secret("cloud_browser", {})["operator-1"]["token"] == "tok-secret-value"


@pytest.mark.asyncio
async def test_blank_token_keeps_the_stored_one():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a", token="tok-first"), _Request())
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/b", token="   "), _Request())
    stored = operator_store.read_secret("cloud_browser", {})["operator-1"]
    assert stored["token"] == "tok-first"
    assert stored["endpoint"] == "wss://browser.example/b"


@pytest.mark.asyncio
async def test_forgetting_the_address_removes_the_secret():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a", token="tok-first"), _Request())
    forgotten = await cloud_browser.save_config(cloud_browser.ConfigInput(clear=True), _Request())
    assert operator_store.read_secret("cloud_browser", {}) == {}
    assert forgotten["configured"] is False
    # The panel spreads this response into its state; a missing switch would
    # render the tab as "Switched off" and lock the operator out of saving again.
    assert forgotten["enabled"] is True
    assert forgotten["encryption_ready"] is True
    assert (await cloud_browser.get_config(_Request()))["configured"] is False


@pytest.mark.asyncio
async def test_missing_encryption_key_refuses_storage():
    import os

    os.environ.pop("SESSION_ENCRYPTION_KEY", None)
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    assert caught.value.status_code == 503


class FakeClient:
    pages_returned = [{"target_id": "t1", "title": "Telegram", "url": "https://web.telegram.org/"}]

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.calls = []

    async def version(self):
        self.calls.append("version")
        return {"product": "Chrome/130.0", "revision": "@44f2b"}

    async def pages(self):
        self.calls.append("pages")
        return self.pages_returned

    async def open_page(self, url):
        self.calls.append(("open", url))
        return {"target_id": "t2"}

    async def close_page(self, target_id):
        self.calls.append(("close", target_id))

    async def navigate(self, target_id, url):
        self.calls.append(("navigate", target_id, url))

    async def screenshot(self, target_id):
        self.calls.append(("screenshot", target_id))
        return "QUJDRA=="

    async def type_text(self, target_id, text):
        self.calls.append(("type", target_id, text))

    async def press_key(self, target_id, key):
        self.calls.append(("key", target_id, key))

    async def close(self):
        self.calls.append("closed")


@pytest.fixture
def connected(monkeypatch):
    holder = {}

    async def fake_connect(url, headers=None, timeout=10.0):
        client = FakeClient(url, headers)
        holder["client"] = client
        holder["url"] = url
        return client

    monkeypatch.setattr(cloud_browser.CdpClient, "connect", classmethod(lambda cls, url, headers=None, timeout=10.0: fake_connect(url, headers)))
    return holder


@pytest.mark.asyncio
async def test_connection_is_proved_and_pages_are_listed(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/session/abc", token="tok-1"), _Request())
    checked = await cloud_browser.check_connection(_Request())
    assert checked["product"] == "Chrome/130.0"
    assert checked["pages"][0]["target_id"] == "t1"
    assert connected["url"] == "wss://browser.example/session/abc"
    assert connected["client"].headers == {"Authorization": "Bearer tok-1"}
    assert connected["client"].calls[-1] == "closed"


@pytest.mark.asyncio
async def test_page_list_is_capped(connected, monkeypatch):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    monkeypatch.setattr(FakeClient, "pages_returned", [{"target_id": f"t{n}", "title": "", "url": ""} for n in range(60)])
    monkeypatch.setattr(cloud_browser, "MAX_PAGES", 5)
    listed = await cloud_browser.list_pages(_Request())
    assert len(listed["pages"]) == 5


@pytest.mark.asyncio
async def test_commands_need_an_address_first():
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.list_pages(_Request())
    assert caught.value.status_code == 400
    assert "Cloud Browser panel" in caught.value.detail


@pytest.mark.asyncio
async def test_page_addresses_are_http_only(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    for bad in ["file:///etc/passwd", "javascript:alert(1)", "ws://browser.example/a"]:
        with pytest.raises(HTTPException) as caught:
            await cloud_browser.open_page(cloud_browser.OpenPageInput(url=bad), _Request())
        assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_screenshot_returns_the_frame(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    shot = await cloud_browser.page_screenshot("t1", _Request())
    assert shot["image"] == "QUJDRA=="


@pytest.mark.asyncio
async def test_typed_text_is_counted_and_never_echoed(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    result = await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+639171234567"), _Request())
    assert result == {"success": True, "characters": 13}
    assert "639171234567" not in json.dumps(result)
    assert connected["client"].calls[0] == ("type", "t1", "+639171234567")


@pytest.mark.asyncio
async def test_unknown_key_is_refused_before_connecting(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.press_key("t1", cloud_browser.KeyInput(key="F13"), _Request())
    assert caught.value.status_code == 400
    assert connected.get("client") is None


@pytest.mark.asyncio
async def test_cloud_phone_code_is_typed_but_never_returned(connected):
    from dashboard_features import save_recovery, RecoveryInput

    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    secret = base64.b32encode(b"12345678901234567890").decode()
    await save_recovery("account-9", RecoveryInput(otp_secret=secret), _Request())

    sent = await cloud_browser.send_cloud_phone_otp("t1", cloud_browser.OtpInput(account_id="account-9"), _Request())
    assert sent["success"] is True
    assert 1 <= sent["seconds_remaining"] <= 30
    assert len(sent) == 2
    typed = [call for call in connected["client"].calls if call[0] == "type"]
    assert len(typed) == 1
    assert len(typed[0][2]) == 6
    assert typed[0][2].isdigit()


@pytest.mark.asyncio
async def test_cloud_phone_record_without_a_secret_is_reported(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.send_cloud_phone_otp("t1", cloud_browser.OtpInput(account_id="missing"), _Request())
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_failure_is_actionable_and_clean(monkeypatch):
    operator_store.write_secret("cloud_browser", {"operator-1": {"endpoint": "wss://browser.example/a", "token": "tok-secret"}})

    async def refuse(url, headers=None, timeout=10.0):
        raise CdpError("Could not reach the browser endpoint. Check the address and that it is running.")

    monkeypatch.setattr(cloud_browser.CdpClient, "connect", classmethod(lambda cls, url, headers=None, timeout=10.0: refuse(url, headers)))
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.list_pages(_Request())
    assert caught.value.status_code == 502
    assert "tok-secret" not in caught.value.detail
    assert "browser.example" not in caught.value.detail
