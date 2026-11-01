"""The browser agent: a plan the provider writes and the operator runs, step by step.

What matters here is the shape of the boundary, not the intelligence of the model:
proposing must not touch the browser, a step must be re-located on the live page before
it runs, the text of a type step must come from the operator rather than the model, and
neither the stored browser address nor a typed credential may ride along in a plan, a
prompt or an error.
"""

import asyncio
import json
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import browser_agent
import cloud_browser
import dashboard_features
from open_teleset import operator_store
from open_teleset.cdp_client import CdpClient, CdpError


class _User:
    id = "operator-1"
    role = "admin"


class _State:
    user = _User()


class _Request:
    state = _State()


class _OtherRequest:
    state = _State()
    state.user = type("U", (), {"id": "operator-2", "role": "admin"})()


class _AnonRequest:
    state = _State()
    state.user = None


PAGE_ELEMENTS = [
    {"tag": "input", "type": "text", "name": "phone", "label": "Phone number", "x": 0.4, "y": 0.6},
    {"tag": "input", "type": "password", "name": "pwd", "label": "Password", "x": 0.4, "y": 0.7},
    {"tag": "button", "type": "submit", "name": "", "label": "Sign in", "x": 0.5, "y": 0.8},
]


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({"cloud_browser_enabled": True})
    yield


@pytest.fixture(autouse=True)
def quiet_logs(monkeypatch):
    monkeypatch.setattr(cloud_browser.log_manager, "add_log", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_plans():
    browser_agent.reset()
    yield
    browser_agent.reset()


@pytest.fixture(autouse=True)
def no_live_sessions():
    cloud_browser._sessions.clear()
    yield
    cloud_browser._sessions.clear()


@pytest.fixture
def provider(monkeypatch):
    """The operator's configured AI provider, with the replies recorded."""
    holder = {"calls": [], "reply": "[]", "error": None}

    async def fake_chat(base_url, key, model, messages, max_tokens=800):
        holder["calls"].append({
            "base_url": base_url, "key": key, "model": model,
            "messages": messages, "max_tokens": max_tokens,
        })
        if holder["error"]:
            raise HTTPException(*holder["error"])
        return holder["reply"]

    monkeypatch.setattr(dashboard_features, "_ai_chat", fake_chat)
    operator_store.write_json("ai_settings", {"operator-1": {
        "provider": "custom", "base_url": "https://ai.example.com/v1", "model": "some-model",
    }})
    operator_store.write_secret("ai_keys", {"operator-1": {"custom": "sk-provider-secret"}})
    return holder


# ------------------------------------------------------------------ DevTools wire


class FakeSocket:
    def __init__(self, handler=None):
        self._handler = handler or self._default
        self._queue = asyncio.Queue()
        self.frames = []
        self.closed = False

    @staticmethod
    def _default(payload):
        method = payload["method"]
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": json.dumps(PAGE_ELEMENTS)}}
        return {}

    async def send_str(self, data):
        payload = json.loads(data)
        self.frames.append(payload)
        reply = self._handler(payload)
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
async def test_evaluate_hands_back_the_pages_own_answer():
    socket = FakeSocket()
    client = await _client(socket)
    try:
        raw = await client.evaluate("t1", "(() => 'x')()")
    finally:
        await client.close()
    assert json.loads(raw)[0]["label"] == "Phone number"
    asked = [frame["method"] for frame in socket.frames]
    assert asked == ["Target.attachToTarget", "Page.enable", "Runtime.evaluate"]


@pytest.mark.asyncio
async def test_the_snapshot_expression_is_read_only():
    """Nothing the page is asked to run may click, type, navigate or submit."""
    forbidden = ["click(", "submit()", ".value =", "location.href =", "document.write", "innerHTML =", "form."]
    for needle in forbidden:
        assert needle not in browser_agent.ELEMENTS_JS, needle
    assert "el.value" not in browser_agent.ELEMENTS_JS
    assert "getBoundingClientRect" in browser_agent.ELEMENTS_JS


@pytest.mark.asyncio
async def test_a_page_that_refuses_to_describe_itself_says_so():
    def refuse(payload):
        if payload["method"] == "Runtime.evaluate":
            return {"exceptionDetails": {"text": "Refused to execute script"}, "result": {}}
        return FakeSocket._default(payload)

    client = await _client(FakeSocket(refuse))
    try:
        with pytest.raises(CdpError):
            await client.evaluate("t1", "(() => 'x')()")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_non_string_answer_is_refused_rather_than_guessing():
    def not_a_string(payload):
        if payload["method"] == "Runtime.evaluate":
            return {"result": {"type": "object", "value": {}}}
        return FakeSocket._default(payload)

    client = await _client(FakeSocket(not_a_string))
    try:
        with pytest.raises(CdpError):
            await client.evaluate("t1", "(() => 'x')()")
    finally:
        await client.close()


# ----------------------------------------------------------------- element parsing


def test_elements_are_numbered_from_one_and_cleaned():
    elements = browser_agent.parse_elements(json.dumps([
        {"tag": "BUTTON", "type": "", "name": "", "label": "  Sign   in  ", "x": 0.5, "y": 0.5},
        {"tag": "a", "type": "", "name": "", "label": "Docs", "x": 0.1, "y": 0.2},
    ]))
    assert [item["index"] for item in elements] == [1, 2]
    assert elements[0]["label"] == "Sign in"
    assert browser_agent.describe(elements) == '1. button "Sign in"\n2. a "Docs"'


def test_junk_from_the_page_is_dropped_not_stored():
    elements = browser_agent.parse_elements(json.dumps([
        {"tag": "button", "label": "ok", "x": 0.5, "y": 0.5},
        {"tag": "button", "label": "offscreen", "x": 1.7, "y": 0.5},
        {"tag": "button", "label": "no position", "y": 0.5},
        "not a control",
        {"label": "no tag", "x": 0.5, "y": 0.5},
    ]))
    assert [item["label"] for item in elements] == ["ok", "no tag"]


def test_an_unreadable_page_answer_is_a_fixed_sentence():
    for raw in ["not json", '{"tag":1}', "null"]:
        with pytest.raises(HTTPException) as caught:
            browser_agent.parse_elements(raw)
        assert caught.value.status_code == 502
        assert caught.value.detail == browser_agent.NO_CONTROLS


# -------------------------------------------------------------------- plan parsing


def _elements():
    return browser_agent.parse_elements(json.dumps(PAGE_ELEMENTS))


def test_a_plan_comes_back_whether_the_model_fences_it_or_not():
    steps = [{"action": "click", "element": 3, "why": "submit the form"}]
    for reply in [json.dumps(steps), f"```json\n{json.dumps(steps)}\n```", json.dumps({"steps": steps})]:
        parsed = browser_agent.parse_steps(reply, _elements(), keys=["Enter"], url_ok=lambda value: value)
        assert parsed[0]["action"] == "click"
        assert parsed[0]["label"] == "Sign in"


def test_an_action_the_browser_cannot_take_is_refused():
    for reply in [
        json.dumps([{"action": "download"}]),
        json.dumps([{"action": "eval", "element": 1}]),
        json.dumps([{"action": ""}]),
        json.dumps(["click the button"]),
        "I cannot help with that.",
        json.dumps("a string"),
    ]:
        with pytest.raises(HTTPException) as caught:
            browser_agent.parse_steps(reply, _elements(), keys=["Enter"], url_ok=lambda value: value)
        assert caught.value.status_code == 400


def test_an_element_the_page_never_reported_is_refused():
    for reply in [
        json.dumps([{"action": "click", "element": 9}]),
        json.dumps([{"action": "click", "element": 0}]),
        json.dumps([{"action": "type", "element": "three"}]),
        json.dumps([{"action": "click"}]),
    ]:
        with pytest.raises(HTTPException) as caught:
            browser_agent.parse_steps(reply, _elements(), keys=["Enter"], url_ok=lambda value: value)
        assert caught.value.detail == browser_agent.UNKNOWN_ELEMENT


def test_a_key_outside_the_boards_allowlist_is_refused():
    with pytest.raises(HTTPException) as caught:
        browser_agent.parse_steps(
            json.dumps([{"action": "key", "key": "F13"}]), _elements(), keys=["Enter"], url_ok=lambda value: value
        )
    assert caught.value.status_code == 400


def test_an_open_step_goes_through_the_page_url_rule():
    checked = []

    def url_ok(value):
        checked.append(value)
        return value

    browser_agent.parse_steps(
        json.dumps([{"action": "open", "url": "https://my.telegram.org"}]), _elements(), keys=[], url_ok=url_ok
    )
    assert checked == ["https://my.telegram.org"]
    # and a refusal from that rule is the operator-facing sentence, not a rewrite
    with pytest.raises(HTTPException):
        browser_agent.parse_steps(
            json.dumps([{"action": "open", "url": "file:///etc/passwd"}]),
            _elements(), keys=[], url_ok=lambda value: (_ for _ in ()).throw(HTTPException(400, "Open http(s) addresses only.")),
        )


def test_a_plan_is_bounded_and_never_empty():
    with pytest.raises(HTTPException) as caught:
        browser_agent.parse_steps("[]", _elements(), keys=[], url_ok=lambda value: value)
    assert caught.value.detail == browser_agent.NO_STEPS
    many = json.dumps([{"action": "key", "key": "Enter"} for _ in range(browser_agent.MAX_STEPS + 1)])
    with pytest.raises(HTTPException) as caught:
        browser_agent.parse_steps(many, _elements(), keys=["Enter"], url_ok=lambda value: value)
    assert caught.value.detail == browser_agent.TOO_MANY_STEPS


def test_the_model_can_not_put_text_in_a_step():
    """A type step's value comes from the operator at approval, never from the reply."""
    parsed = browser_agent.parse_steps(
        json.dumps([{"action": "type", "element": 2, "text": "hunter2", "value": "hunter2", "why": "sign in"}]),
        _elements(), keys=[], url_ok=lambda value: value,
    )
    assert "hunter2" not in json.dumps(parsed)
    assert parsed[0]["label"] == "Password"
    assert parsed[0]["status"] == "todo"


# ---------------------------------------------------------------- control identity


def test_a_control_is_found_again_by_its_name_or_its_label():
    elements = _elements()
    named = {"tag": "input", "type": "text", "name": "phone", "label": "Phone number"}
    labelled = {"tag": "button", "type": "submit", "name": "", "label": "Sign in"}
    assert browser_agent.find_element(elements, named)["index"] == 1
    assert browser_agent.find_element(elements, labelled)["index"] == 3


def test_a_control_that_has_gone_is_not_replaced_by_a_neighbour():
    elements = _elements()
    assert browser_agent.find_element(elements, {"tag": "button", "type": "submit", "name": "", "label": "Pay now"}) is None
    assert browser_agent.find_element([], {"tag": "input", "type": "text", "name": "phone", "label": "Phone number"}) is None


# ------------------------------------------------------------------------ registry


def test_a_plan_belongs_to_the_operator_who_asked_for_it():
    plan = browser_agent.new_plan("operator-1", "t1", "sign in", {"url": "https://x"}, [{"index": 0, "action": "key", "key": "Enter", "label": "Enter", "why": "", "status": "todo", "result": ""}])
    assert browser_agent.get_plan("operator-1", plan.id) is plan
    with pytest.raises(HTTPException) as caught:
        browser_agent.get_plan("operator-2", plan.id)
    assert caught.value.status_code == 404


def test_a_plan_expires(monkeypatch):
    plan = browser_agent.new_plan("operator-1", "t1", "g", {}, [])
    monkeypatch.setattr(browser_agent, "PLAN_TTL_SECONDS", -1)
    with pytest.raises(HTTPException):
        browser_agent.get_plan("operator-1", plan.id)


def test_old_plans_are_pruned_so_the_list_stays_small():
    ids = [browser_agent.new_plan("operator-1", "t1", "g", {}, []).id for _ in range(browser_agent.MAX_PLANS + 2)]
    alive = [pid for pid in ids if pid in browser_agent._plans]
    assert len(alive) == browser_agent.MAX_PLANS
    assert browser_agent.get_plan("operator-1", ids[-1]).id == ids[-1]


def test_another_operators_plans_survive_the_prune():
    theirs = browser_agent.new_plan("operator-2", "t1", "g", {}, [])
    for _ in range(browser_agent.MAX_PLANS + 1):
        browser_agent.new_plan("operator-1", "t1", "g", {}, [])
    assert browser_agent.get_plan("operator-2", theirs.id) is theirs


def test_switching_browser_drops_the_plans_drafted_against_the_old_one():
    plan = browser_agent.new_plan("operator-1", "t1", "g", {}, [])
    browser_agent.forget_operator("operator-1")
    assert plan.id not in browser_agent._plans


# ---------------------------------------------------------------------- endpoints


class FakeClient:
    # class level so one monkeypatch can redraw the page for every client made after it
    pages_returned: ClassVar[list] = [{"target_id": "t1", "title": "Telegram Web", "url": "https://web.telegram.org/a/"}]
    page_elements: ClassVar[list] = PAGE_ELEMENTS

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.calls = []
        self.is_closed = False
        self.elements = list(self.page_elements)

    async def pages(self):
        self.calls.append("pages")
        return self.pages_returned

    async def evaluate(self, target_id, expression):
        self.calls.append(("evaluate", target_id))
        return json.dumps(self.elements)

    async def viewport_size(self, target_id):
        self.calls.append(("viewport", target_id))
        return (1000.0, 500.0)

    async def click_mouse(self, target_id, x, y, button="left", click_count=1):
        self.calls.append(("click", x, y, button, click_count))

    async def type_text(self, target_id, text):
        self.calls.append(("type", target_id, text))

    async def press_key(self, target_id, key):
        self.calls.append(("key", target_id, key))

    async def navigate(self, target_id, url):
        self.calls.append(("navigate", target_id, url))

    async def start_screencast(self, target_id, on_frame, quality=60, max_width=1280, max_height=800):
        self.calls.append(("startcast", target_id))
        return f"page-session-{target_id}"

    async def stop_screencast(self, session_id):
        self.calls.append(("stopcast", session_id))

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
        return client

    monkeypatch.setattr(cloud_browser.CdpClient, "connect", classmethod(lambda cls, url, headers=None, timeout=10.0: fake_connect(url, headers)))
    monkeypatch.setattr(cloud_browser, "feature_enabled", lambda name: True)
    return holder


PLAN_REPLY = json.dumps([
    {"action": "open", "url": "https://my.telegram.org", "why": "start at the sign-in page"},
    {"action": "type", "element": 1, "why": "enter the Cloud Phone number"},
    {"action": "click", "element": 3, "why": "press Sign in"},
])


async def _draft(connected, provider, reply=PLAN_REPLY, goal="sign in to my.telegram.org"):
    provider["reply"] = reply
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://secret-host.example/session", token="tok-secret-value"), _Request())
    result = await cloud_browser.propose_plan(cloud_browser.PlanInput(target_id="t1", goal=goal), _Request())
    return result["plan"]


@pytest.mark.asyncio
async def test_proposing_a_plan_reads_the_page_and_moves_nothing(connected, provider):
    plan = await _draft(connected, provider)
    calls = connected["client"].calls
    assert ("evaluate", "t1") in calls
    assert [call for call in calls if isinstance(call, tuple) and call[0] in ("click", "type", "key", "navigate")] == []
    assert [step["status"] for step in plan["steps"]] == ["todo", "todo", "todo"]


@pytest.mark.asyncio
async def test_the_provider_is_shown_the_goal_and_the_controls_it_may_pick(connected, provider):
    await _draft(connected, provider)
    sent = provider["calls"][0]["messages"]
    user = sent[1]["content"]
    assert "sign in to my.telegram.org" in user
    assert "https://web.telegram.org/a/" in user
    assert '1. input[type=text] name="phone" "Phone number"' in user
    assert "Chrome DevTools" in sent[0]["content"]
    assert provider["calls"][0]["base_url"] == "https://ai.example.com/v1"


@pytest.mark.asyncio
async def test_the_stored_browser_address_never_reaches_the_provider(connected, provider):
    await _draft(connected, provider)
    assert "secret-host.example" not in json.dumps(provider["calls"])
    assert "tok-secret-value" not in json.dumps(provider["calls"])


@pytest.mark.asyncio
async def test_a_plan_is_answered_with_the_steps_and_nothing_else(connected, provider):
    plan = await _draft(connected, provider)
    assert plan["page"] == {"url": "https://web.telegram.org/a/", "title": "Telegram Web"}
    assert [step["action"] for step in plan["steps"]] == ["open", "type", "click"]
    assert plan["steps"][2]["label"] == "Sign in"
    body = json.dumps(plan)
    assert "secret-host.example" not in body and "tok-secret-value" not in body
    for step in plan["steps"]:
        assert set(step) == {"index", "action", "label", "why", "status", "result", "needs_text", "key", "url"}


@pytest.mark.asyncio
async def test_a_plan_needs_a_provider_that_is_actually_configured(connected, provider, monkeypatch):
    operator_store.write_secret("ai_keys", {"operator-1": {}})
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.propose_plan(cloud_browser.PlanInput(target_id="t1", goal="sign in"), _Request())
    assert caught.value.status_code == 503
    assert caught.value.detail == browser_agent.NO_PROVIDER


@pytest.mark.asyncio
async def test_a_refused_plan_is_the_providers_failure_and_not_a_partial_plan(connected, provider):
    for reply, detail in [("[{\"action\":\"fly\"}]", browser_agent.UNKNOWN_ACTION), ("nonsense", browser_agent.UNUSABLE_PLAN)]:
        with pytest.raises(HTTPException) as caught:
            await _draft(connected, provider, reply=reply)
        assert caught.value.status_code == 400
        assert caught.value.detail == detail
    assert browser_agent._plans == {}


@pytest.mark.asyncio
async def test_a_page_that_is_not_open_stops_the_plan_before_the_provider_is_asked(connected, provider):
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.propose_plan(cloud_browser.PlanInput(target_id="gone", goal="sign in"), _Request())
    assert caught.value.status_code == 404
    assert provider["calls"] == []


@pytest.mark.asyncio
async def test_the_second_step_is_refused_until_the_first_one_runs(connected, provider):
    plan = await _draft(connected, provider)
    clients = len(connected["clients"])
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 1, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 409
    # refused before a connection was even opened, so nothing could reach the browser
    assert len(connected["clients"]) == clients

    ran = await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert ("navigate", "t1", "https://my.telegram.org") in connected["clients"][-1].calls
    assert ran["plan"]["steps"][0]["status"] == "done"
    assert ran["plan"]["steps"][0]["result"] == "Opened https://my.telegram.org"
    assert ran["plan"]["steps"][1]["status"] == "todo"


@pytest.mark.asyncio
async def test_a_type_step_waits_for_the_operators_text(connected, provider):
    plan = await _draft(connected, provider)
    await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    clients = len(connected["clients"])
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 1, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 400
    assert len(connected["clients"]) == clients

    ran = await cloud_browser.run_plan_step(plan["id"], 1, cloud_browser.StepRunInput(text="+639170000111"), _Request())
    fresh = connected["clients"][-1]
    assert ("click", 400.0, 300.0, "left", 1) in fresh.calls
    assert ("type", "t1", "+639170000111") in fresh.calls
    assert ran["plan"]["steps"][1]["result"] == "Typed 13 characters"
    assert "+639170000111" not in json.dumps(ran)


@pytest.mark.asyncio
async def test_a_control_that_is_gone_is_not_clicked_where_it_used_to_be(connected, provider, monkeypatch):
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "click", "element": 3, "why": "press Sign in"}]))
    monkeypatch.setattr(FakeClient, "page_elements", [
        {"tag": "input", "type": "text", "name": "search", "label": "Search", "x": 0.2, "y": 0.2},
    ])
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 409
    assert "new plan" in caught.value.detail
    last = connected["clients"][-1]
    assert [call for call in last.calls if isinstance(call, tuple) and call[0] == "click"] == []
    assert plan["steps"][0]["status"] == "todo"


@pytest.mark.asyncio
async def test_a_click_step_runs_against_the_page_as_it_is_now(connected, provider, monkeypatch):
    plan = await _draft(connected, provider)
    await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    await cloud_browser.run_plan_step(plan["id"], 1, cloud_browser.StepRunInput(text="hello"), _Request())
    # the page has redrawn: the button is somewhere else now
    monkeypatch.setattr(FakeClient, "page_elements", [
        {"tag": "input", "type": "text", "name": "phone", "label": "Phone number", "x": 0.4, "y": 0.6},
        {"tag": "button", "type": "submit", "name": "", "label": "Sign in", "x": 0.9, "y": 0.3},
    ])
    ran = await cloud_browser.run_plan_step(plan["id"], 2, cloud_browser.StepRunInput(), _Request())
    assert ("click", 900.0, 150.0, "left", 1) in connected["clients"][-1].calls
    assert ran["plan"]["steps"][2]["result"] == "Clicked Sign in"
    assert ran["plan"]["done"] is True


@pytest.mark.asyncio
async def test_a_finished_plan_has_no_next_step(connected, provider):
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "key", "key": "Enter", "why": "submit"}]))
    ran = await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert ran["plan"]["done"] is True
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_the_live_view_shows_what_the_agent_did(connected, provider):
    """A step rides the open cockpit connection rather than reconnecting behind it."""
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "key", "key": "Tab", "why": "next field"}]))
    response = await cloud_browser.stream_page("t1", _Request())
    await response.body_iterator.aclose()
    live = cloud_browser._sessions["operator-1"]
    await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert ("key", "t1", "Tab") in live.client.calls
    assert live.client.calls.count("closed") == 0


@pytest.mark.asyncio
async def test_a_provider_refusal_is_reported_as_it_is(connected, provider):
    provider["error"] = (502, "Provider unreachable (ConnectError)")
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/a"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.propose_plan(cloud_browser.PlanInput(target_id="t1", goal="sign in"), _Request())
    assert caught.value.status_code == 502


@pytest.mark.asyncio
async def test_another_operator_cannot_read_or_run_a_plan(connected, provider):
    plan = await _draft(connected, provider)
    for call in (
        lambda: cloud_browser.read_plan(plan["id"], _OtherRequest()),
        lambda: cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _OtherRequest()),
        lambda: cloud_browser.discard_plan(plan["id"], _OtherRequest()),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_plan_needs_an_authenticated_operator(connected):
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.read_plan("nope", _AnonRequest())
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_plans_are_switched_off_with_the_panel(connected, monkeypatch):
    monkeypatch.setattr(cloud_browser, "feature_enabled", lambda name: False)
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.propose_plan(cloud_browser.PlanInput(target_id="t1", goal="sign in"), _Request())
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_discarding_a_plan_takes_it_away(connected, provider):
    plan = await _draft(connected, provider)
    assert plan["id"] in browser_agent._plans
    await cloud_browser.discard_plan(plan["id"], _Request())
    assert plan["id"] not in browser_agent._plans
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.read_plan(plan["id"], _Request())
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_reading_a_plan_back_keeps_the_results(connected, provider):
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "key", "key": "Enter", "why": "go"}]))
    await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    read = await cloud_browser.read_plan(plan["id"], _Request())
    assert read["plan"]["steps"][0]["result"] == "Sent Enter"
    assert read["plan"]["done"] is True


@pytest.mark.asyncio
async def test_switching_the_browser_in_use_takes_the_plan_with_it(connected, provider):
    plan = await _draft(connected, provider)
    await cloud_browser.save_config(cloud_browser.ConfigInput(endpoint="wss://browser.example/b"), _Request())
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_steps_failure_is_the_same_two_sentences_as_every_other_cdp_failure(connected, provider, monkeypatch):
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "key", "key": "Enter", "why": "go"}]))

    async def refuse(self, target_id, key):
        raise CdpError("Target closed")

    monkeypatch.setattr(FakeClient, "press_key", refuse)
    with pytest.raises(HTTPException) as caught:
        await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(), _Request())
    assert caught.value.status_code == 502
    assert caught.value.detail == "Target closed"
    assert plan["steps"][0]["status"] == "todo"


@pytest.mark.asyncio
async def test_the_log_records_the_action_and_never_its_text(connected, provider, monkeypatch):
    logged = []
    monkeypatch.setattr(cloud_browser.log_manager, "add_log", lambda *a: logged.append(a))
    plan = await _draft(connected, provider, reply=json.dumps([{"action": "type", "element": 2, "why": "sign in"}]))
    await cloud_browser.run_plan_step(plan["id"], 0, cloud_browser.StepRunInput(text="sup3rs3cret"), _Request())
    body = json.dumps(logged)
    assert "sup3rs3cret" not in body
    assert "Ran step 1 of a browser plan (type)" in body
    read = await cloud_browser.read_plan(plan["id"], _Request())
    assert read["plan"]["steps"][0]["result"] == "Typed 11 characters"
    assert "sup3rs3cret" not in json.dumps(read)
