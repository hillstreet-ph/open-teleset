"""Unified automation engine: encrypted definitions, durable runs, triggers.

The engine owns the definition (trigger + ordered steps) and the run ledger.
Actions themselves stay with the component that already owns them, so these
tests replace the handlers and the scheduler clock rather than reaching for
Telegram or the network.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import automation_engine as engine
import scheduler as scheduler_module
from open_teleset import operator_store


class FakeScheduler:
    """Stands in for the shared clock; records what the engine asked for."""

    def __init__(self):
        self.entries = {}

    def list_schedules(self):
        return list(self.entries.values())

    def add_schedule(self, schedule_id, **kwargs):
        self.entries[schedule_id] = {"id": schedule_id,
                                     "next_run": "2026-01-01T00:00:00+00:00", **kwargs}
        return True

    def remove_schedule(self, schedule_id):
        return self.entries.pop(schedule_id, None) is not None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUTOMATION_RUNS_DB", str(tmp_path / "automation_runs.sqlite3"))
    operator_store.set_features({})
    monkeypatch.setattr(engine.log_manager, "add_log", lambda *args, **kwargs: None)
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "task_scheduler", fake)
    return SimpleNamespace(tmp=tmp_path, scheduler=fake)


NOTE_STEP = {"action": "notify", "config": {"text": "hello"}}


def _client(operator_id="operator-1"):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = (SimpleNamespace(id=operator_id, role="admin")
                              if operator_id else None)
        return await call_next(request)

    app.include_router(engine.router)
    return TestClient(app)


def _payload(**overrides):
    body = {"name": "Nightly outreach", "trigger": {"type": "manual"}, "steps": [NOTE_STEP]}
    body.update(overrides)
    return body


def test_anonymous_operator_is_refused():
    client = _client(None)
    assert client.get("/api/automations").status_code == 403
    assert client.post("/api/automations", json=_payload()).status_code == 403


def test_create_list_and_update_an_automation():
    client = _client()
    created = client.post("/api/automations",
                          json=_payload(trigger={"type": "schedule", "cron": "0 9 * * *"},
                                        description="Every morning"))
    assert created.status_code == 201, created.text
    automation = created.json()["automation"]
    assert automation["id"].startswith("auto_")
    assert automation["name"] == "Nightly outreach"
    assert automation["trigger"] == {"type": "schedule", "cron": "0 9 * * *"}
    assert automation["steps"] == [NOTE_STEP]
    assert automation["next_run"] == "2026-01-01T00:00:00+00:00"

    listed = client.get("/api/automations").json()
    assert [entry["id"] for entry in listed["automations"]] == [automation["id"]]
    assert listed["enabled"] is True
    assert any(item["id"] == "member_transfer" for item in listed["catalog"])
    assert all(item["id"] != "factory" for item in listed["catalog"])

    updated = client.put(f"/api/automations/{automation['id']}",
                         json=_payload(name="Renamed", trigger={"type": "manual"}))
    assert updated.status_code == 200, updated.text
    assert updated.json()["automation"]["name"] == "Renamed"
    assert updated.json()["automation"]["created"] == automation["created"]


def test_definitions_are_encrypted_at_rest(isolated):
    client = _client()
    assert client.post("/api/automations", json=_payload(name="Secret campaign name")).status_code == 201
    raw = (isolated.tmp / "automations.secret.json").read_text()
    stored = json.loads(raw)
    assert "ciphertext" in stored
    assert "Secret campaign name" not in raw


def test_automations_are_scoped_to_the_operator():
    first = _client("operator-1")
    automation = first.post("/api/automations", json=_payload()).json()["automation"]

    second = _client("operator-2")
    assert second.get("/api/automations").json()["automations"] == []
    assert second.get(f"/api/automations/{automation['id']}/runs").status_code == 404
    run = first.post(f"/api/automations/{automation['id']}/run", json={}).json()["run"]
    assert second.get(f"/api/automation-runs/{run['run_id']}").status_code == 404


def test_bad_cron_unknown_action_and_missing_consent_are_refused():
    client = _client()
    assert client.post("/api/automations",
                       json=_payload(trigger={"type": "schedule", "cron": "every day"})).status_code == 400
    assert client.post("/api/automations",
                       json=_payload(steps=[{"action": "teleport", "config": {}}])).status_code == 400
    add_without_consent = client.post("/api/automations", json=_payload(steps=[{
        "action": "member_add",
        "config": {"account_id": "a", "dest_target": "@dest", "usernames": ["alice"]},
    }]))
    assert add_without_consent.status_code == 400
    assert "consent" in add_without_consent.json()["detail"].lower()
    assert client.post("/api/automations", json=_payload(steps=[])).status_code == 400


def test_scheduled_automations_ride_the_shared_clock(isolated):
    client = _client()
    automation = client.post("/api/automations",
                             json=_payload(trigger={"type": "schedule", "cron": "*/15 * * * *"})).json()["automation"]
    entry = isolated.scheduler.entries[f"auto_{automation['id']}"]
    assert entry["action"] == "run_automation"
    assert entry["automation_id"] == automation["id"]
    assert entry["operator_id"] == "operator-1"
    assert entry["cron"] == "*/15 * * * *"

    client.put(f"/api/automations/{automation['id']}", json=_payload(trigger={"type": "manual"}))
    assert f"auto_{automation['id']}" not in isolated.scheduler.entries

    client.put(f"/api/automations/{automation['id']}",
               json=_payload(trigger={"type": "schedule", "cron": "0 6 * * *"}))
    assert f"auto_{automation['id']}" in isolated.scheduler.entries
    client.delete(f"/api/automations/{automation['id']}")
    assert f"auto_{automation['id']}" not in isolated.scheduler.entries


def test_a_manual_run_writes_every_step_to_the_ledger():
    client = _client()
    automation = client.post("/api/automations", json=_payload()).json()["automation"]

    started = client.post(f"/api/automations/{automation['id']}/run", json={})
    assert started.status_code == 202, started.text
    run = started.json()["run"]
    assert run["status"] == "completed"
    assert run["trigger"] == "manual"
    assert [step["action"] for step in run["steps"]] == ["notify"]
    assert run["steps"][0]["status"] == "completed"
    assert run["steps"][0]["detail"] == {"queued": True}

    receipts = client.get(f"/api/automations/{automation['id']}/runs").json()["runs"]
    assert receipts[0]["run_id"] == run["run_id"]
    detail = client.get(f"/api/automation-runs/{run['run_id']}").json()
    assert detail["steps"][0]["status"] == "completed"


def test_dispatch_steps_hand_off_with_a_deterministic_request_id(monkeypatch):
    seen = {}

    async def fake_auto_join(operator, config):
        seen.update(config)
        return {"job_id": "job-1", "state": "queued", "total": 2}

    monkeypatch.setitem(engine.HANDLERS, "auto_join", fake_auto_join)
    client = _client()
    automation = client.post("/api/automations", json=_payload(steps=[{
        "action": "auto_join",
        "config": {"account_id": "a", "urls": ["https://t.me/examplechan"], "delay": 38},
    }])).json()["automation"]

    run = client.post(f"/api/automations/{automation['id']}/run", json={}).json()["run"]
    assert run["status"] == "dispatched"
    assert run["steps"][0]["status"] == "dispatched"
    assert run["steps"][0]["detail"]["job_id"] == "job-1"
    assert seen["request_id"] == f"{run['run_id']}-0"
    assert seen["run_id"] == run["run_id"]


def test_a_failing_step_records_the_type_but_never_the_message(monkeypatch):
    async def exploding(operator, config):
        raise RuntimeError("provider token=supersecret")

    monkeypatch.setitem(engine.HANDLERS, "notify", exploding)
    client = _client()
    automation = client.post("/api/automations", json=_payload()).json()["automation"]

    response = client.post(f"/api/automations/{automation['id']}/run", json={})
    assert response.status_code == 202
    run = response.json()["run"]
    assert run["status"] == "failed"
    assert run["reason"] == "step_failed"
    assert run["steps"][0]["detail"] == {"error": "RuntimeError"}
    assert "supersecret" not in response.text


def test_a_disabled_automation_only_runs_when_triggered_by_hand(isolated):
    client = _client()
    automation = client.post("/api/automations", json=_payload(
        enabled=False, trigger={"type": "schedule", "cron": "0 9 * * *"})).json()["automation"]
    assert f"auto_{automation['id']}" not in isolated.scheduler.entries

    with pytest.raises(HTTPException) as raised:
        asyncio.run(engine.run_automation("operator-1", automation["id"], trigger="schedule"))
    assert raised.value.status_code == 409

    manual = client.post(f"/api/automations/{automation['id']}/run", json={})
    assert manual.status_code == 202
    assert manual.json()["run"]["status"] == "completed"


def test_terminal_jobs_fan_out_once_to_matching_event_triggers():
    client = _client()
    client.post("/api/automations", json=_payload(
        name="After a member add",
        trigger={"type": "event", "event": "job.terminal", "kind": "member_add"},
        steps=[{"action": "wait", "config": {"seconds": 0}}],
    ))
    client.post("/api/automations", json=_payload(
        name="Only joins",
        trigger={"type": "event", "event": "job.completed", "kind": "auto_join"},
        steps=[{"action": "wait", "config": {"seconds": 0}}],
    ))

    async def scenario():
        snapshot = {"id": "job-1", "state": "completed"}
        engine.handle_job_terminal("operator-1", "member_add", snapshot)
        engine.handle_job_terminal("operator-1", "member_add", snapshot)
        engine.handle_job_terminal("operator-1", "personal_send", {"id": "job-2", "state": "completed"})
        for _ in range(40):
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    runs = engine._run_rows("operator-1", limit=10)
    assert len(runs) == 1
    assert runs[0]["trigger"] == "event"
    assert runs[0]["trigger_detail"] == {"job_id": "job-1", "kind": "member_add", "state": "completed"}


def test_scheduler_executes_an_automation_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", str(tmp_path / "schedules.json"))
    clock = scheduler_module.TaskScheduler()
    clock.schedules = {}
    monkeypatch.setattr(clock, "_save_schedules", lambda: None)
    calls = {}

    async def fake_run(operator, automation_id, trigger="manual", trigger_key="", trigger_detail=None):
        calls.update(operator=operator, automation_id=automation_id, trigger=trigger)
        return {"run_id": "run_test"}

    monkeypatch.setattr(engine, "run_automation", fake_run)
    schedule = {"id": "auto_abc1234567", "action": "run_automation", "cron": "0 9 * * *",
                "automation_id": "auto_abc1234567", "operator_id": "operator-1"}
    assert asyncio.run(clock._run_automation_schedule(schedule)) is True
    assert calls == {"operator": "operator-1", "automation_id": "auto_abc1234567",
                     "trigger": "schedule"}
    assert schedule["run_count"] == 1
    assert schedule["last_error"] == ""


def test_schedule_requirements_accept_only_well_formed_automation_ids():
    ok, _, _ = scheduler_module.schedule_requirements(
        {"action": "run_automation", "automation_id": "auto_abc1234567"})
    assert ok is None
    with pytest.raises(ValueError):
        scheduler_module.schedule_requirements(
            {"action": "run_automation", "automation_id": "not-an-automation"})
