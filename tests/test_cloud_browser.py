"""Cloud Browser: DevTools plumbing and the operator-facing contract.

The security contract matters more than the features here: the stored address and
token, the text typed into a login form and the Cloud Phone code must never come
back out in a response or a log.
"""

import asyncio
import base64
import io
import json
import zipfile

import pytest
import pytest_asyncio
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
async def test_control_keys_cover_a_login_form_without_covering_text():
    """Arrow and delete keys belong to a login flow; printable characters do not."""
    assert cloud_browser.pressable_keys() == [
        "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "Home", "End",
        "PageUp", "PageDown", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown",
    ]
    socket = FakeSocket()
    client = await _client(socket)
    await client.press_key("t1", "ArrowDown")
    await client.close()
    down = [frame for frame in socket.frames if frame["method"] == "Input.dispatchKeyEvent"][0]
    assert down["params"]["windowsVirtualKeyCode"] == 40
    assert down["params"]["key"] == "ArrowDown"


async def _until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the DevTools event pump")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_screencast_acks_each_frame_before_passing_it_on():
    """Chrome stops streaming until the newest frame is acknowledged."""
    socket = FakeSocket()
    client = await _client(socket)
    seen = []
    session_id = await client.start_screencast("t1", lambda data, meta: seen.append((data, meta)))
    assert socket.frames[-1]["method"] == "Page.startScreencast"
    assert socket.frames[-1]["sessionId"] == session_id

    async def _event(session, timestamp):
        await socket._queue.put(json.dumps({
            "method": "Page.screencastFrame",
            "sessionId": session,
            "params": {"data": f"/9j/{timestamp}", "metadata": {"timestamp": timestamp}},
        }))

    await _event(session_id, 1)
    # A frame for another page must not be acked on this session: Chrome would
    # then consider this page's newest frame unacknowledged and stall the stream.
    await _event("someone-elses-page", 2)
    await _event(session_id, 3)
    await _until(lambda: len(seen) == 2)

    assert seen == [("/9j/1", {"timestamp": 1}), ("/9j/3", {"timestamp": 3})]
    acks = [frame for frame in socket.frames if frame["method"] == "Page.screencastFrameAck"]
    assert [frame["params"]["frameMetadata"]["timestamp"] for frame in acks] == [1, 3]
    assert all(frame["sessionId"] == session_id for frame in acks)
    await client.close()


@pytest.mark.asyncio
async def test_stopping_the_stream_removes_the_frame_handler():
    socket = FakeSocket()
    client = await _client(socket)
    session_id = await client.start_screencast("t1", lambda data, meta: None)
    await client.stop_screencast(session_id)
    assert socket.frames[-1]["method"] == "Page.stopScreencast"
    assert socket.frames[-1]["sessionId"] == session_id
    assert client._listeners["Page.screencastFrame"] == []
    assert client._screencasts == {}
    await client.close()


@pytest.mark.asyncio
async def test_click_presses_and_releases_at_the_same_point():
    socket = FakeSocket()
    client = await _client(socket)
    await client.click_mouse("t1", 120.5, 340.0)
    events = [frame["params"] for frame in socket.frames if frame["method"] == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in events] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert all(event["x"] == 120.5 and event["y"] == 340.0 for event in events)
    assert events[0]["button"] == "none" and events[0]["buttons"] == 0
    assert events[1]["button"] == "left" and events[1]["buttons"] == 1 and events[1]["clickCount"] == 1
    assert events[2]["buttons"] == 0
    mouse_frames = [frame for frame in socket.frames if frame["method"] == "Input.dispatchMouseEvent"]
    assert {frame["sessionId"] for frame in mouse_frames} == {"session-1"}
    await client.close()


@pytest.mark.asyncio
async def test_right_click_and_wheel_use_their_own_button_state():
    socket = FakeSocket()
    client = await _client(socket)
    await client.click_mouse("t1", 10.0, 20.0, button="right")
    await client.scroll_page("t1", 10.0, 20.0, delta_y=260.0)
    events = [frame["params"] for frame in socket.frames if frame["method"] == "Input.dispatchMouseEvent"]
    pressed = [event for event in events if event["type"] == "mousePressed"][0]
    assert pressed["button"] == "right" and pressed["buttons"] == 8
    wheel = [event for event in events if event["type"] == "mouseWheel"][0]
    assert wheel["deltaY"] == 260.0 and wheel["deltaX"] == 0.0 and wheel["buttons"] == 0
    await client.close()


@pytest.mark.asyncio
async def test_viewport_size_comes_from_the_css_metrics():
    """Frames are scaled; clicks are mapped with the CSS viewport, not device pixels."""
    def metrics(payload):
        if payload["method"] == "Page.getLayoutMetrics":
            return {
                "cssVisualViewport": {"width": 900.0, "height": 700.0},
                "visualViewport": {"width": 1800, "height": 1400},
                "devicePixelRatio": 2,
            }
        return FakeSocket._default(payload)

    socket = FakeSocket(metrics)
    client = await _client(socket)
    assert await client.viewport_size("t1") == (900.0, 700.0)
    await client.close()


@pytest.mark.asyncio
async def test_missing_metrics_are_refused_not_guessed():
    socket = FakeSocket()
    client = await _client(socket)
    with pytest.raises(CdpError) as caught:
        await client.viewport_size("t1")
    assert "1280" not in str(caught.value)
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
    assert "session/abc" not in json.dumps(loaded)
    record = operator_store.read_secret("cloud_browser", {})["operator-1"]
    assert record["profiles"][record["active"]]["token"] == "tok-secret-value"


@pytest.mark.asyncio
async def test_blank_token_keeps_the_stored_one():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a", token="tok-first"), _Request())
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/b", token="   "), _Request())
    record = operator_store.read_secret("cloud_browser", {})["operator-1"]
    stored = record["profiles"][record["active"]]
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


# --------------------------------------------------------------- named browsers


def _stored_profiles() -> dict:
    record = operator_store.read_secret("cloud_browser", {})["operator-1"]
    return record["profiles"]


@pytest.mark.asyncio
async def test_a_named_browser_is_listed_by_label_and_host():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://login.example/a"), _Request())
    added = await cloud_browser.add_profile(
        cloud_browser.ProfileInput(label="Cloud Phone console", endpoint="wss://phone.example/b", token="tok-2"), _Request()
    )
    listing = {item["label"]: item for item in added["profiles"]}
    assert set(listing) == {"Cloud Phone console", "login.example"}
    assert listing["Cloud Phone console"]["host"] == "phone.example"
    assert listing["Cloud Phone console"]["has_token"] is True
    assert listing["Cloud Phone console"]["active"] is True
    assert added["active"] == listing["Cloud Phone console"]["id"]
    # The address can carry a session path; the panel sees only the host.
    assert "phone.example/b" not in json.dumps(added)
    assert "tok-2" not in json.dumps(added)


@pytest.mark.asyncio
async def test_an_added_browser_is_the_one_commands_use(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://first.example/a", token="tok-1"), _Request())
    await cloud_browser.add_profile(cloud_browser.ProfileInput(label="second", endpoint="wss://second.example/b", token="tok-2"), _Request())
    await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+63917"), _Request())
    assert connected["url"] == "wss://second.example/b"
    assert connected["client"].headers == {"Authorization": "Bearer tok-2"}


@pytest.mark.asyncio
async def test_switching_browsers_uses_the_other_address(connected):
    await cloud_browser.add_profile(cloud_browser.ProfileInput(label="one", endpoint="wss://one.example/a"), _Request())
    second = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="two", endpoint="wss://two.example/b"), _Request())
    first_id = next(item["id"] for item in second["profiles"] if item["label"] == "one")
    switched = await cloud_browser.use_profile(first_id, _Request())
    assert switched["host"] == "one.example"
    await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+63917"), _Request())
    assert connected["url"] == "wss://one.example/a"


@pytest.mark.asyncio
async def test_switching_browsers_releases_the_live_view(live):
    second = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="other", endpoint="wss://other.example/b"), _Request())
    assert live["client"].is_closed is True
    assert cloud_browser._sessions == {}
    assert second["host"] == "other.example"


@pytest.mark.asyncio
async def test_using_the_browser_already_in_use_keeps_the_live_view(live):
    active = (await cloud_browser.get_config(_Request()))["active"]
    await cloud_browser.use_profile(active, _Request())
    assert live["client"].is_closed is False
    assert cloud_browser._live_state("operator-1")["live"] is True


@pytest.mark.asyncio
async def test_editing_a_browser_keeps_its_stored_token(connected):
    created = await cloud_browser.add_profile(
        cloud_browser.ProfileInput(label="vault", endpoint="wss://vault.example/a", token="tok-old"), _Request()
    )
    profile_id = created["active"]
    await cloud_browser.update_profile(profile_id, cloud_browser.ProfileInput(label="vault", token="   "), _Request())
    assert _stored_profiles()[profile_id]["token"] == "tok-old"
    await cloud_browser.update_profile(profile_id, cloud_browser.ProfileInput(token="tok-new"), _Request())
    assert _stored_profiles()[profile_id]["token"] == "tok-new"
    await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+63917"), _Request())
    assert connected["client"].headers == {"Authorization": "Bearer tok-new"}


@pytest.mark.asyncio
async def test_an_edit_only_changes_the_named_browser(connected):
    first = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="one", endpoint="wss://one.example/a"), _Request())
    second = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="two", endpoint="wss://two.example/b"), _Request())
    one_id = first["active"]
    assert second["active"] != one_id
    await cloud_browser.update_profile(one_id, cloud_browser.ProfileInput(endpoint="wss://moved.example/c"), _Request())
    # The inactive browser is still the one being driven, so no view is torn down.
    await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+63917"), _Request())
    assert connected["url"] == "wss://two.example/b"
    listing = (await cloud_browser.get_config(_Request()))["profiles"]
    assert {item["label"]: item["host"] for item in listing} == {"one": "moved.example", "two": "two.example"}


@pytest.mark.asyncio
async def test_forgetting_a_browser_keeps_the_others(connected):
    one = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="one", endpoint="wss://one.example/a"), _Request())
    two = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="two", endpoint="wss://two.example/b"), _Request())
    forgotten = await cloud_browser.forget_profile(two["active"], _Request())
    assert [item["label"] for item in forgotten["profiles"]] == ["one"]
    assert forgotten["active"] == one["active"]
    assert forgotten["host"] == "one.example"
    # The last browser gone is the whole vault gone - no empty record keeps a host.
    cleared = await cloud_browser.forget_profile(one["active"], _Request())
    assert operator_store.read_secret("cloud_browser", {}) == {}
    assert cleared["configured"] is False
    assert cleared["profiles"] == []


@pytest.mark.asyncio
async def test_forgetting_a_browser_you_are_not_viewing_keeps_the_view(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    added = await cloud_browser.add_profile(cloud_browser.ProfileInput(label="other", endpoint="wss://other.example/b"), _Request())
    other_id = added["active"]
    viewed_id = next(item["id"] for item in added["profiles"] if item["label"] == "browser.example")
    await cloud_browser.use_profile(viewed_id, _Request())
    response = await cloud_browser.stream_page("t1", _Request())
    body = response.body_iterator
    assert (await _next_event(body))["type"] == "ready"
    await cloud_browser.forget_profile(other_id, _Request())
    assert cloud_browser._live_state("operator-1")["live"] is True
    assert connected["client"].is_closed is False
    await body.aclose()


@pytest.mark.asyncio
async def test_the_browser_list_is_capped(monkeypatch):
    monkeypatch.setattr(cloud_browser, "MAX_PROFILES", 3)
    for n in range(3):
        await cloud_browser.add_profile(cloud_browser.ProfileInput(label=f"b{n}", endpoint=f"wss://b{n}.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.add_profile(cloud_browser.ProfileInput(label="fourth", endpoint="wss://b4.example/a"), _Request())
    assert caught.value.status_code == 409
    assert "forget" in caught.value.detail.lower()


@pytest.mark.asyncio
async def test_an_unknown_browser_id_is_reported():
    for call in (
        lambda: cloud_browser.use_profile("browser_missing", _Request()),
        lambda: cloud_browser.update_profile("browser_missing", cloud_browser.ProfileInput(label="x"), _Request()),
        lambda: cloud_browser.forget_profile("browser_missing", _Request()),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_browser_needs_a_devtools_address():
    for bad in ["https://web.telegram.org", "wss://user:pass@browser.example/x", "ws://"]:
        with pytest.raises(HTTPException) as caught:
            await cloud_browser.add_profile(cloud_browser.ProfileInput(label="x", endpoint=bad), _Request())
        assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_a_label_of_only_spaces_falls_back_to_the_host():
    added = await cloud_browser.add_profile(cloud_browser.ProfileInput(label=" \n  ", endpoint="wss://host.example/a"), _Request())
    assert added["profiles"][0]["label"] == "host.example"


@pytest.mark.asyncio
async def test_saving_the_address_can_rename_the_browser_in_use():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://a.example/1", label="Login browser"), _Request())
    saved = await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://a.example/2"), _Request())
    assert [item["label"] for item in saved["profiles"]] == ["Login browser"]
    # A blank name keeps the one on record rather than falling back to the host.
    assert saved["host"] == "a.example"
    assert saved["updated_at"]


@pytest.mark.asyncio
async def test_a_legacy_address_reads_as_one_named_browser():
    operator_store.write_secret("cloud_browser", {"operator-1": {"endpoint": "wss://legacy.example/session/x", "token": "tok-old"}})
    config = await cloud_browser.get_config(_Request())
    assert config["configured"] is True
    assert config["host"] == "legacy.example"
    assert [item["label"] for item in config["profiles"]] == ["legacy.example"]
    assert config["active"] == "default"
    assert config["profiles"][0]["has_token"] is True
    # The next save moves the secret into the profile shape and drops the top-level copy.
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://legacy.example/session/y"), _Request())
    record = operator_store.read_secret("cloud_browser", {})["operator-1"]
    assert "endpoint" not in record and "token" not in record
    assert record["profiles"]["default"]["token"] == "tok-old"


@pytest.mark.asyncio
async def test_a_record_from_another_operator_is_not_readable():
    operator_store.write_secret("cloud_browser", {"operator-2": {"endpoint": "wss://other.example/a", "token": "tok-2"}})
    config = await cloud_browser.get_config(_Request())
    assert config["configured"] is False
    assert config["profiles"] == []


@pytest.mark.asyncio
async def test_the_panel_is_told_how_many_browsers_it_can_keep():
    config = await cloud_browser.get_config(_Request())
    assert config["max_profiles"] == cloud_browser.MAX_PROFILES


class FakeClient:
    pages_returned = [{"target_id": "t1", "title": "Telegram", "url": "https://web.telegram.org/"}]

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.calls = []
        self.is_closed = False
        self.publisher = None

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

    async def start_screencast(self, target_id, on_frame, quality=60, max_width=1280, max_height=800):
        self.calls.append(("startcast", target_id))
        self.publisher = on_frame
        return f"page-session-{target_id}"

    async def stop_screencast(self, session_id):
        self.calls.append(("stopcast", session_id))

    async def viewport_size(self, target_id):
        self.calls.append(("viewport", target_id))
        return (1000.0, 500.0)

    async def move_mouse(self, target_id, x, y):
        self.calls.append(("move", x, y))

    async def click_mouse(self, target_id, x, y, button="left", click_count=1):
        self.calls.append(("click", x, y, button, click_count))

    async def scroll_page(self, target_id, x, y, delta_x=0.0, delta_y=0.0):
        self.calls.append(("scroll", x, y, delta_x, delta_y))

    async def close(self):
        self.calls.append("closed")
        self.is_closed = True


@pytest.fixture
def connected(monkeypatch):
    holder = {"clients": []}

    async def fake_connect(url, headers=None, timeout=10.0):
        client = FakeClient(url, headers)
        holder["clients"].append(client)
        holder["client"] = client
        holder["url"] = url
        return client

    monkeypatch.setattr(cloud_browser.CdpClient, "connect", classmethod(lambda cls, url, headers=None, timeout=10.0: fake_connect(url, headers)))
    return holder


@pytest.fixture(autouse=True)
def no_live_sessions():
    cloud_browser._sessions.clear()
    yield
    for session in list(cloud_browser._sessions.values()):
        if session._idle:
            session._idle.cancel()
    cloud_browser._sessions.clear()


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


# ------------------------------------------------------------------- live view


async def _next_event(body):
    chunk = await body.__anext__()
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    return json.loads(chunk[6:-2])


@pytest_asyncio.fixture
async def live(connected):
    """One operator with the cockpit open, plus the stream that opened it."""
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    response = await cloud_browser.stream_page("t1", _Request())
    body = response.body_iterator
    assert (await _next_event(body))["type"] == "ready"
    holder = {"response": response, "body": body, "client": connected["client"], "url": connected["url"]}
    yield holder
    await body.aclose()


@pytest.mark.asyncio
async def test_frames_reach_the_viewer_and_extra_paints_are_paced(live):
    raw = base64.b64encode(b"jpeg-one").decode()
    live["client"].publisher(raw, {"timestamp": 1_730_000_000})
    event = await _next_event(live["body"])
    assert event == {"type": "frame", "image": raw, "at": "2024-10-27T03:33:20+00:00"}

    # Chrome paints on every change; a login view needs two frames a second, and
    # a viewer that falls behind must not be able to make the queue grow.
    live["client"].publisher(base64.b64encode(b"jpeg-two").decode(), {})
    session = cloud_browser._sessions["operator-1"]
    assert session.frames == 1


@pytest.mark.asyncio
async def test_the_browsers_address_never_reaches_the_viewer(live):
    await cloud_browser.save_config(
        cloud_browser.ConfigInput(endpoint="wss://secret-host.example/session/abc", token="tok-secret-value"), _Request()
    )
    state = cloud_browser._live_state("operator-1")
    assert "secret-host" not in json.dumps(state)
    assert "tok-secret-value" not in json.dumps(state)
    assert set(state) == {"live", "watchers", "frames", "recording", "reel_frames", "reel_bytes"}


@pytest.mark.asyncio
async def test_a_view_nobody_watches_is_released(live, monkeypatch):
    monkeypatch.setattr(cloud_browser, "LIVE_IDLE_SECONDS", 0.05)
    await live["body"].aclose()
    await _until(lambda: not cloud_browser._sessions)
    assert live["client"].is_closed is True
    assert ("stopcast", "page-session-t1") in live["client"].calls


@pytest.mark.asyncio
async def test_commands_ride_the_open_cockpit_connection(live):
    """While the view is live the panel must not reconnect for every click."""
    await cloud_browser.type_into_page("t1", cloud_browser.TypeInput(text="+63917"), _Request())
    assert ("type", "t1", "+63917") in live["client"].calls
    assert "closed" not in live["client"].calls
    assert cloud_browser._live_state("operator-1")["live"] is True


@pytest.mark.asyncio
async def test_closing_the_view_releases_the_connection(live):
    closed = await cloud_browser.close_live_view(_Request())
    assert closed["live"] is False
    assert live["client"].is_closed is True
    assert cloud_browser._sessions == {}


@pytest.mark.asyncio
async def test_the_page_under_the_view_uses_the_same_session(live):
    """A second watcher of the same page shares the connection, not the browser."""
    response = await cloud_browser.stream_page("t1", _Request())
    body = response.body_iterator
    assert (await _next_event(body))["type"] == "ready"
    assert cloud_browser._live_state("operator-1")["watchers"] == 2
    assert live["url"] == "wss://browser.example/a"
    await body.aclose()


@pytest.mark.asyncio
async def test_a_second_watcher_beyond_the_limit_is_refused(live, monkeypatch):
    """One view is shared, but the frame stream must not fan out without bound."""
    monkeypatch.setattr(cloud_browser, "MAX_SUBSCRIBERS", 1)
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.stream_page("t1", _Request())
    assert caught.value.status_code == 429


@pytest.mark.asyncio
async def test_switching_pages_tears_the_previous_stream_down(live):
    first = live["client"]
    response = await cloud_browser.stream_page("t2", _Request())
    await _next_event(response.body_iterator)
    assert first.is_closed is True
    assert cloud_browser._sessions["operator-1"].target_id == "t2"
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_saving_a_new_address_drops_the_live_connection(live):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/b"), _Request())
    assert live["client"].is_closed is True
    assert cloud_browser._sessions == {}


@pytest.mark.asyncio
async def test_closing_the_viewed_page_stops_its_stream(live):
    await cloud_browser.close_page("t1", _Request())
    assert live["client"].is_closed is True
    assert cloud_browser._sessions == {}


@pytest.mark.asyncio
async def test_a_page_that_is_not_viewed_keeps_the_stream_open(live):
    await cloud_browser.close_page("other", _Request())
    assert live["client"].is_closed is False
    assert cloud_browser._sessions["operator-1"].target_id == "t1"


# --------------------------------------------------------------------- mouse


@pytest.mark.asyncio
async def test_clicks_are_mapped_onto_the_browsers_own_viewport(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    result = await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(x=0.5, y=0.25), _Request())
    assert ("click", 500.0, 125.0, "left", 1) in connected["client"].calls
    assert result["viewport"] == {"width": 1000.0, "height": 500.0}


@pytest.mark.asyncio
async def test_double_right_and_wheel_each_reach_the_browser(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(x=1, y=1, action="double"), _Request())
    await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(x=0, y=0, action="right"), _Request())
    await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(x=0.5, y=0.5, action="scroll", delta_y=-300), _Request())
    await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(x=0.5, y=0.5, action="move"), _Request())
    calls = [call for client in connected["clients"] for call in client.calls]
    assert ("click", 1000.0, 500.0, "left", 2) in calls
    assert ("click", 0.0, 0.0, "right", 1) in calls
    assert ("scroll", 500.0, 250.0, 0.0, -300.0) in calls
    assert ("move", 500.0, 250.0) in calls


@pytest.mark.asyncio
async def test_a_click_outside_the_page_is_refused_before_connecting(connected):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    for bad in [{"x": 1.4, "y": 0.5}, {"x": 0.5, "y": -0.2}, {"x": 0.5, "y": 0.5, "action": "drag"}]:
        with pytest.raises((HTTPException, ValueError)):
            await cloud_browser.control_mouse("t1", cloud_browser.MouseInput(**bad), _Request())
    assert connected.get("client") is None or connected["client"].calls == []


# --------------------------------------------------------------------- reel


@pytest.mark.asyncio
async def test_recording_needs_the_live_view():
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.set_reel("t1", cloud_browser.ReelInput(recording=True), _Request())
    assert caught.value.status_code == 409
    assert "live view" in caught.value.detail


@pytest.mark.asyncio
async def test_a_recording_downloads_as_frames(live, monkeypatch):
    monkeypatch.setattr(cloud_browser, "FRAME_INTERVAL", 0.0)
    started = await cloud_browser.set_reel("t1", cloud_browser.ReelInput(recording=True), _Request())
    assert started["recording"] is True
    for index in range(3):
        live["client"].publisher(base64.b64encode(f"frame-{index}".encode()).decode(), {})
    reel = await cloud_browser.download_reel("t1", _Request())
    assert reel.media_type == "application/zip"
    assert reel.headers["content-disposition"] == 'attachment; filename="cloud-browser-t1.zip"'
    with zipfile.ZipFile(io.BytesIO(reel.body)) as bundle:
        assert bundle.namelist() == ["frame-00001.jpg", "frame-00002.jpg", "frame-00003.jpg"]
        assert bundle.read("frame-00002.jpg") == b"frame-1"


@pytest.mark.asyncio
async def test_the_reel_keeps_the_newest_frames_and_forgets_the_oldest(live, monkeypatch):
    monkeypatch.setattr(cloud_browser, "FRAME_INTERVAL", 0.0)
    monkeypatch.setattr(cloud_browser, "REEL_MAX_FRAMES", 2)
    await cloud_browser.set_reel("t1", cloud_browser.ReelInput(recording=True), _Request())
    for index in range(4):
        live["client"].publisher(base64.b64encode(f"frame-{index}".encode()).decode(), {})
    reel = await cloud_browser.download_reel("t1", _Request())
    with zipfile.ZipFile(io.BytesIO(reel.body)) as bundle:
        assert bundle.read("frame-00001.jpg") == b"frame-2"
        assert bundle.read("frame-00002.jpg") == b"frame-3"


@pytest.mark.asyncio
async def test_stopping_a_recording_and_clearing_it(live, monkeypatch):
    monkeypatch.setattr(cloud_browser, "FRAME_INTERVAL", 0.0)
    await cloud_browser.set_reel("t1", cloud_browser.ReelInput(recording=True), _Request())
    live["client"].publisher(base64.b64encode(b"frame").decode(), {})
    stopped = await cloud_browser.set_reel("t1", cloud_browser.ReelInput(recording=False), _Request())
    assert stopped["recording"] is False
    assert stopped["reel_frames"] == 1
    live["client"].publisher(base64.b64encode(b"more").decode(), {})
    assert cloud_browser._sessions["operator-1"].reel_bytes == len(b"frame")
    discarded = await cloud_browser.discard_reel("t1", _Request())
    assert discarded["reel_frames"] == 0
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.download_reel("t1", _Request())
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_recording_the_wrong_page_is_refused(live):
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.set_reel("t2", cloud_browser.ReelInput(recording=True), _Request())
    assert caught.value.status_code == 409
    assert "not the one being viewed" in caught.value.detail


@pytest.mark.asyncio
async def test_a_page_id_cannot_escape_the_download_name(live, monkeypatch):
    monkeypatch.setattr(cloud_browser, "FRAME_INTERVAL", 0.0)
    session = cloud_browser._sessions["operator-1"]
    session.target_id = 'evil"; filename="x.zip'
    session.reel.append(b"frame")
    session.reel_bytes = 5
    reel = await cloud_browser.download_reel('evil"; filename="x.zip', _Request())
    assert reel.headers["content-disposition"] == 'attachment; filename="cloud-browser-evilfilenamexzip.zip"'


@pytest.mark.asyncio
async def test_the_panel_is_told_which_keys_actually_work(connected):
    config = await cloud_browser.get_config(_Request())
    assert config["keys"] == cloud_browser.pressable_keys()
    assert "Enter" in config["keys"] and "F13" not in config["keys"]

