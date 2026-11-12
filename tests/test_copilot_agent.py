"""The Copilot agent loop chooses tools; the server decides what may happen."""

import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import copilot
import copilot_agent
import dashboard_features


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return tmp_path


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(copilot_agent.router)
    return TestClient(app)


def _provider(monkeypatch, model="test-model"):
    monkeypatch.setattr(copilot_agent, "_provider",
                        lambda operator: ("https://ai.test/v1", "key", model))


def _chat(monkeypatch, replies):
    sent = []
    queue = list(replies)

    def fake(base_url, key, model, messages, tools):
        sent.append({"messages": [dict(item) for item in messages], "tools": tools})
        return queue.pop(0)

    monkeypatch.setattr(copilot_agent, "_chat", fake)
    return sent


def _call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _reply(*calls, content=""):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def test_capabilities_show_tools_skills_and_connectors(client, monkeypatch):
    monkeypatch.setattr(copilot, "list_presets",
                        lambda operator: [{"id": "scrape", "title": "Scrape members",
                                           "group": "Scraper"}])
    monkeypatch.setattr(dashboard_features, "_integrations",
                        lambda: [{"id": "mcp-1", "name": "Notes MCP",
                                  "transport": "http", "auth_mode": "token"}])
    body = client.get("/api/copilot/agent/capabilities").json()

    assert body["success"] is True
    by_id = {tool["id"]: tool for tool in body["tools"]}
    assert by_id["accounts-overview"]["write"] is False
    assert by_id["schedule-create"]["write"] is True
    assert body["skills"][0]["id"] == "scrape"
    assert body["connectors"][0]["name"] == "Notes MCP"
    assert body["limits"]["steps"] == copilot_agent.MAX_STEPS


def test_a_read_tool_runs_inside_the_loop(monkeypatch):
    _provider(monkeypatch)
    ran = []

    def run_internal(operator, draft):
        ran.append(draft["payload"]["command"])
        return {"accounts": 3, "online": 2}

    monkeypatch.setattr(copilot, "_run_internal", run_internal)
    sent = _chat(monkeypatch, [
        _reply(_call("accounts-overview", {"instruction": "Review my accounts."})),
        _reply(content="Three accounts are connected; two are online."),
    ])

    result = copilot_agent.run_agent("operator-1", "Review my accounts.", "ask")

    assert result["status"] == "done"
    assert result["engine"] == "model"
    assert result["reply"].startswith("Three accounts are connected")
    assert ran == ["accounts-overview"]
    assert result["steps"][0]["status"] == "ok"
    assert sent[1]["messages"][-1]["role"] == "tool"


def test_a_write_tool_stops_for_approval_and_never_runs(monkeypatch):
    _provider(monkeypatch)
    monkeypatch.setattr(copilot, "_run_internal",
                        lambda operator, draft: pytest.fail("a write must not run"))
    _chat(monkeypatch, [
        _reply(_call("schedule-create",
                     {"instruction": "Collect members from @example_group every day at 09:00."})),
    ])

    result = copilot_agent.run_agent(
        "operator-1", "Collect members from @example_group every day at 09:00.", "work")

    assert result["status"] == "awaiting_approval"
    assert result["draft"]["requires_confirmation"] is True
    assert result["draft"]["payload"]["command"] == "schedule-create"
    assert result["steps"][0]["status"] == "pending"


def test_an_unknown_tool_is_reported_and_never_executed(monkeypatch):
    _provider(monkeypatch)
    _chat(monkeypatch, [
        _reply(_call("delete-everything", {})),
        _reply(content="There is no tool for that."),
    ])

    result = copilot_agent.run_agent("operator-1", "Delete everything.", "ask")

    assert result["status"] == "done"
    assert result["steps"][0]["tool"] == "delete-everything"
    assert result["steps"][0]["status"] == "error"


def test_run_skill_opens_a_pending_draft(monkeypatch):
    _provider(monkeypatch)
    preset = {"id": "scrape", "title": "Scrape members", "kind": "command",
              "command": "accounts-overview", "group": "Scraper"}
    monkeypatch.setattr(copilot, "_preset_by_id",
                        lambda operator, preset_id: preset if preset_id == "scrape" else None)
    _chat(monkeypatch, [
        _reply(_call("run_skill", {"skill_id": "scrape", "instruction": "Review my accounts."})),
    ])

    result = copilot_agent.run_agent("operator-1", "Run the scrape skill.", "ask")

    assert result["status"] == "awaiting_approval"
    assert result["draft"]["payload"]["command"] == "accounts-overview"
    assert result["steps"][0]["tool"] == "run_skill"


def test_inspect_connector_is_read_only(monkeypatch):
    _provider(monkeypatch)
    monkeypatch.setattr(dashboard_features, "_integrations",
                        lambda: [{"id": "mcp-1", "name": "Notes MCP", "transport": "http",
                                  "base_url": "https://mcp.test", "auth_mode": "token"}])
    monkeypatch.setattr(dashboard_features, "_public_integration",
                        lambda item, entry=None: {"id": "mcp-1", "name": "Notes MCP",
                                                  "transport": "http"})
    _chat(monkeypatch, [
        _reply(_call("inspect_connector", {"connector_id": "mcp-1"})),
        _reply(content="Notes MCP speaks HTTP."),
    ])

    result = copilot_agent.run_agent("operator-1", "Inspect my connectors.", "ask")

    assert result["status"] == "done"
    assert result["steps"][0]["status"] == "ok"


def test_the_guard_refuses_account_creation_before_any_model_call(monkeypatch):
    monkeypatch.setattr(copilot_agent, "_provider",
                        lambda operator: pytest.fail("the guard runs first"))

    result = copilot_agent.run_agent("operator-1", "Create 20 Telegram accounts.", "work")

    assert result["status"] == "refused"
    assert result["engine"] == "guard"
    assert "create telegram accounts" in result["reply"].lower()


def test_without_a_provider_the_rules_drafter_answers(monkeypatch):
    monkeypatch.setattr(copilot_agent, "_provider", lambda operator: None)
    monkeypatch.setattr(copilot, "_run_internal",
                        lambda operator, draft: {"accounts": 2, "online": 1})

    result = copilot_agent.run_agent("operator-1", "Review my accounts.", "ask")

    assert result["engine"] == "rules"
    assert result["status"] == "done"
    assert result["draft"]["payload"]["command"] == "accounts-overview"
    assert result["steps_used"] == 0


def test_steps_are_bounded(monkeypatch):
    _provider(monkeypatch)
    monkeypatch.setattr(copilot_agent, "MAX_STEPS", 2)
    monkeypatch.setattr(copilot, "_run_internal", lambda operator, draft: {"accounts": 1})
    _chat(monkeypatch, [
        _reply(_call("accounts-overview", {"instruction": "Review my accounts."})),
        _reply(_call("accounts-overview", {"instruction": "Again."})),
    ])

    result = copilot_agent.run_agent("operator-1", "Keep looking.", "ask")

    assert result["status"] == "done"
    assert result["steps_used"] == 2
    assert "2 steps" in result["reply"]


def test_calls_per_step_are_capped(monkeypatch):
    _provider(monkeypatch)
    monkeypatch.setattr(copilot_agent, "MAX_CALLS_PER_STEP", 1)
    ran = []
    monkeypatch.setattr(copilot, "_run_internal",
                        lambda operator, draft: ran.append(1) or {"accounts": 1})
    _chat(monkeypatch, [
        _reply(_call("accounts-overview", {"instruction": "One."}, "call-1"),
               _call("proxies-overview", {"instruction": "Two."}, "call-2")),
        _reply(content="Done."),
    ])

    result = copilot_agent.run_agent("operator-1", "Look twice.", "ask")

    assert len(ran) == 1
    assert result["steps_used"] == 1


def test_the_time_budget_stops_the_loop(monkeypatch):
    _provider(monkeypatch)
    monkeypatch.setattr(copilot_agent, "STEP_BUDGET_SECONDS", -1)

    result = copilot_agent.run_agent("operator-1", "Review my accounts.", "ask")

    assert result["status"] == "done"
    assert "time limit" in result["reply"]
    assert result["steps_used"] == 0


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._response


def _transport(monkeypatch, **kwargs):
    monkeypatch.setattr(copilot_agent.httpx, "Client", lambda **_: _Client(**kwargs))


def test_a_rejected_key_becomes_a_readable_400(monkeypatch):
    _transport(monkeypatch, response=_Response(401, {}))
    with pytest.raises(Exception) as caught:
        copilot_agent._chat("https://ai.test/v1", "key", "model", [], [])
    assert getattr(caught.value, "status_code", None) == 400
    assert "rejected" in caught.value.detail.lower()


def test_an_unreachable_provider_becomes_a_502(monkeypatch):
    _transport(monkeypatch, error=httpx.ConnectError("boom"))
    with pytest.raises(Exception) as caught:
        copilot_agent._chat("https://ai.test/v1", "key", "model", [], [])
    assert getattr(caught.value, "status_code", None) == 502
    assert "unreachable" in caught.value.detail.lower()


def test_a_malformed_provider_reply_becomes_a_502(monkeypatch):
    _transport(monkeypatch, response=_Response(200, ValueError("not json")))
    with pytest.raises(Exception) as caught:
        copilot_agent._chat("https://ai.test/v1", "key", "model", [], [])
    assert getattr(caught.value, "status_code", None) == 502


def test_the_endpoint_runs_the_agent(client, monkeypatch):
    monkeypatch.setattr(copilot_agent, "_provider", lambda operator: None)
    monkeypatch.setattr(copilot, "_run_internal",
                        lambda operator, draft: {"accounts": 1, "online": 1})
    response = client.post("/api/copilot/agent",
                           json={"prompt": "Review my accounts.", "mode": "ask"})

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "rules"
    assert body["draft"]["payload"]["command"] == "accounts-overview"


def test_the_endpoint_rejects_an_oversized_prompt(client):
    response = client.post("/api/copilot/agent",
                           json={"prompt": "x" * (copilot_agent.MAX_PROMPT_CHARS + 1)})
    assert response.status_code == 422
