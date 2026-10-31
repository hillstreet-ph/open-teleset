"""Automation proxy: config gate, token forwarding, cooldown and error mapping."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation

REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def client(monkeypatch):
    automation._runs.clear()
    automation._last_accepted = 0.0
    monkeypatch.setattr(automation.log_manager, "add_log", lambda *args, **kwargs: None)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("N8N_AUTOMATION_WEBHOOK_URL", "http://n8n.test/webhook/telegram-account-factory")
    monkeypatch.setenv("N8N_AUTOMATION_WEBHOOK_TOKEN", "shared-token")
    monkeypatch.setenv("AUTOMATION_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("AUTOMATION_DEFAULT_MAX_PRICE", "0.75")
    monkeypatch.setenv("AUTOMATION_DEFAULT_OTP_ATTEMPTS", "24")
    app = FastAPI()
    app.include_router(automation.router)
    return TestClient(app)


def use_upstream(monkeypatch, handler):
    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(automation.httpx, "AsyncClient", factory)


def test_status_and_run_fail_closed_without_configuration(client, monkeypatch):
    monkeypatch.delenv("N8N_AUTOMATION_WEBHOOK_URL")
    monkeypatch.delenv("N8N_AUTOMATION_WEBHOOK_TOKEN")

    status = client.get("/api/automation/status").json()
    assert status["configured"] is False
    assert "N8N_AUTOMATION_WEBHOOK_URL" in status["problem"]
    assert status["runs"] == []

    response = client.post("/api/automation/run", json={})
    assert response.status_code == 503
    assert "N8N_AUTOMATION_WEBHOOK_URL" in response.json()["detail"]


def test_status_reports_configuration_without_leaking_secrets(client):
    status = client.get("/api/automation/status").json()
    assert status["configured"] is True
    assert status["problem"] is None
    assert status["webhook_host"] == "n8n.test"
    assert "shared-token" not in str(status)
    assert status["cooldown_seconds"] == 600
    assert status["cooldown_remaining"] == 0
    assert status["defaults"] == {"max_price": 0.75, "otp_attempts": 24}


def test_run_forwards_token_and_overrides_then_enforces_cooldown(client, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("x-automation-token")
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"message": "Workflow was started"})

    use_upstream(monkeypatch, handler)

    response = client.post("/api/automation/run", json={"max_price": 0.3, "otp_attempts": 12})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["run_id"].startswith("run_")
    assert body["max_price"] == 0.3
    assert body["otp_attempts"] == 12
    assert captured["url"] == "http://n8n.test/webhook/telegram-account-factory"
    assert captured["token"] == "shared-token"
    assert '"source":"open-teleset"' in captured["body"]
    assert '"max_price":0.3' in captured["body"]
    assert '"requested_by":"dashboard"' in captured["body"]

    status = client.get("/api/automation/status").json()
    assert status["cooldown_remaining"] > 0
    assert status["last_run"]["status"] == "accepted"
    assert status["runs"][0]["run_id"] == body["run_id"]

    blocked = client.post("/api/automation/run", json={})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_bad_token_is_reported_without_leaking_details(client, monkeypatch):
    use_upstream(monkeypatch, lambda request: httpx.Response(403, json={"message": "denied"}))

    response = client.post("/api/automation/run", json={})
    assert response.status_code == 502
    assert "AUTOMATION_WEBHOOK_TOKEN" in response.json()["detail"]
    status = client.get("/api/automation/status").json()
    assert status["last_run"]["status"] == "failed"
    assert status["last_run"]["upstream_status"] == 403
    assert status["cooldown_remaining"] == 0

    # A rejection does not start a paid run, so another attempt is allowed.
    response = client.post("/api/automation/run", json={})
    assert response.status_code == 502


def test_upstream_errors_are_mapped_without_echoing_urls(client, monkeypatch):
    use_upstream(monkeypatch, lambda request: httpx.Response(500, text="internal"))
    response = client.post("/api/automation/run", json={})
    assert response.status_code == 502
    assert "HTTP 500" in response.json()["detail"]
    assert "n8n.test" not in response.json()["detail"]

    def unreachable(request):
        raise httpx.ConnectError("dial failed", request=request)

    use_upstream(monkeypatch, unreachable)
    response = client.post("/api/automation/run", json={})
    assert response.status_code == 502
    assert "published" in response.json()["detail"]
    assert "dial failed" not in response.json()["detail"]


def test_run_rejects_out_of_range_overrides(client):
    assert client.post("/api/automation/run", json={"max_price": 0.001}).status_code == 422
    assert client.post("/api/automation/run", json={"otp_attempts": 500}).status_code == 422
