"""The internal superpowers: routing, Ask/Work gating and the tool catalogue.

Every test is hermetic. The scheduler, account, proxy, template and log
singletons are replaced with in-memory fakes, so no real
./accounts/schedules.json is read or written.
"""

import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import copilot


class FakeScheduler:
    def __init__(self):
        self.records: dict[str, dict] = {}

    def list_schedules(self):
        return list(self.records.values())

    def get_schedule(self, schedule_id):
        return self.records.get(schedule_id)

    def add_schedule(self, **kwargs):
        schedule_id = kwargs["schedule_id"]
        self.records[schedule_id] = {
            "id": schedule_id,
            "schedule_id": schedule_id,
            "name": kwargs.get("name"),
            "cron": kwargs.get("cron"),
            "action": kwargs.get("action"),
            "target": kwargs.get("target"),
            "enabled": kwargs.get("enabled", True),
            "next_run": "2026-11-08T09:00:00",
            "last_run": None,
        }
        return True

    def toggle_schedule(self, schedule_id):
        record = self.records.get(schedule_id)
        if not record:
            return False
        record["enabled"] = not bool(record.get("enabled"))
        return True


class FakeLogs:
    def __init__(self):
        self.limits: list[int] = []

    def get_logs(self, limit=10):
        self.limits.append(limit)
        return [{"timestamp": f"2026-11-07T09:0{index}:00", "action": "scrape",
                 "level": "info", "detail": f"line {index}"} for index in range(limit)]


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())

    scheduler = FakeScheduler()
    logs = FakeLogs()
    monkeypatch.setitem(sys.modules, "scheduler", SimpleNamespace(task_scheduler=scheduler))
    monkeypatch.setitem(sys.modules, "log_manager", SimpleNamespace(log_manager=logs))
    monkeypatch.setitem(sys.modules, "account_manager", SimpleNamespace(account_manager=SimpleNamespace(
        list_accounts=lambda: [
            {"username": "alice", "status": "online"},
            {"first_name": "Bob", "last_name": "Ng", "status": "offline",
             "connection_error": "timeout"},
        ],
    )))
    monkeypatch.setitem(sys.modules, "proxy_manager", SimpleNamespace(proxy_manager=SimpleNamespace(
        list_proxies=lambda: {"proxies": [{"id": "p1"}], "global": {},
                              "auto_failover_enabled": True, "maintenance": False},
    )))
    monkeypatch.setitem(sys.modules, "template_manager", SimpleNamespace(template_manager=SimpleNamespace(
        get_stats=lambda: {"total": 2, "by_category": {"support": 1, "sales": 1}},
        list_templates=lambda: [
            {"name": "Welcome", "category": "support", "use_count": 3},
        ],
    )))

    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(copilot.router)
    return SimpleNamespace(client=TestClient(app), scheduler=scheduler, logs=logs)


def _draft(bench, instruction, mode="ask"):
    response = bench.client.post("/api/copilot/draft",
                                 json={"instruction": instruction, "mode": mode})
    assert response.status_code == 200
    return response.json()["draft"]


def _seed_toggle(bench, name="Member scrape @example_group",
                 schedule_id="copilot_abc123", enabled=True):
    bench.scheduler.records[schedule_id] = {
        "id": schedule_id, "schedule_id": schedule_id, "name": name,
        "action": "scrape_members", "target": "@example_group",
        "enabled": enabled, "next_run": "2026-11-08T09:00:00",
    }
    return bench.scheduler.records[schedule_id]


def test_tool_catalogue_groups_every_internal_command(bench):
    response = bench.client.get("/api/copilot/tools")

    assert response.status_code == 200
    groups = {group["group"]: group["tools"] for group in response.json()["tools"]}
    assert list(groups) == ["Workspace", "Automation", "Knowledge", "Replies"]
    tools = [tool for entries in groups.values() for tool in entries]
    assert {tool["id"] for tool in tools} == set(copilot.INTERNAL_COMMANDS)
    assert {tool["id"] for tool in tools if tool["write"]} == {"schedule-create", "schedule-toggle"}
    assert all(tool["title"] and tool["summary"] and tool["example"] for tool in tools)


@pytest.mark.parametrize("command,example", list(copilot.INTERNAL_EXAMPLES.items()))
def test_each_catalogue_example_routes_to_its_own_tool(bench, command, example):
    draft = copilot.build_draft(example)

    assert draft["action"] == "run"
    assert draft["payload"]["command"] == command


def test_ask_mode_holds_a_schedule_write_for_the_run_button(bench):
    draft = _draft(bench, "Collect members from @example_group every day at 09:00, limit 200.",
                   mode="ask")

    assert draft["payload"]["command"] == "schedule-create"
    assert draft["requires_confirmation"] is True
    assert draft["pending"] is True
    assert "result" not in draft
    assert draft["mode"] == "ask"
    assert bench.scheduler.records == {}
    assert any("already belong" in warning for warning in draft["warnings"])


def test_work_mode_creates_the_cycle_without_a_second_confirmation(bench):
    draft = _draft(bench, "Collect members from @example_group every day at 09:00, limit 200.",
                   mode="work")

    assert draft["mode"] == "work"
    assert draft.get("pending") is not True
    assert "Created the daily member scrape of @example_group at 09:00, limit 200" \
        in draft["result"]["summary"]
    (record,) = bench.scheduler.records.values()
    assert record["action"] == "scrape_members"
    assert record["target"] == "@example_group"
    assert record["cron"] == "0 9 * * *"
    assert record["enabled"] is True


@pytest.mark.parametrize("mode", ["ask", "work"])
def test_reads_run_in_either_mode(bench, mode):
    draft = _draft(bench, "Review my accounts.", mode=mode)

    assert draft["payload"]["command"] == "accounts-overview"
    assert draft["mode"] == mode
    assert draft["result"]["summary"] == (
        "2 account(s) connected, 1 online, 1 reporting a connection error.")
    assert draft["result"]["items"] == [
        {"label": "@alice", "detail": "online"},
        {"label": "Bob Ng", "detail": "connection error"},
    ]
    # Counts only; no phone numbers or session strings.
    assert "phone" not in str(draft["result"]).lower()


def test_ask_mode_never_switches_an_automation(bench):
    record = _seed_toggle(bench)

    draft = _draft(bench, "Pause the member scrape automation.", mode="ask")

    assert draft["payload"]["command"] == "schedule-toggle"
    assert draft["pending"] is True
    assert "result" not in draft
    assert record["enabled"] is True


def test_work_mode_switches_the_named_automation(bench):
    record = _seed_toggle(bench)

    draft = _draft(bench, "Pause the member scrape automation.", mode="work")

    assert draft["result"]["summary"] == "Member scrape @example_group is now paused."
    assert record["enabled"] is False


def test_work_mode_resumes_an_automation_from_a_paused_state(bench):
    record = _seed_toggle(bench, enabled=False)

    draft = _draft(bench, "Resume the member scrape automation.", mode="work")

    assert draft["result"]["summary"] == "Member scrape @example_group is now on."
    assert record["enabled"] is True


def test_work_mode_reports_a_toggle_with_no_match_without_failing(bench):
    draft = _draft(bench, "Pause the member scrape automation.", mode="work")

    assert draft["result"] == {"error": "No automation matches that name."}


def test_work_mode_lists_both_names_when_a_toggle_is_ambiguous(bench):
    _seed_toggle(bench)
    _seed_toggle(bench, name="Member scrape @second_group", schedule_id="copilot_def456")

    draft = _draft(bench, "Pause the member scrape automation.", mode="work")

    assert draft["result"]["error"].startswith("More than one automation matches:")
    assert all(record["enabled"] for record in bench.scheduler.records.values())


def test_work_mode_refuses_a_schedule_without_a_group(bench):
    draft = _draft(bench, "Schedule to scrape members every day.", mode="work")

    assert draft["payload"]["command"] == "schedule-create"
    assert any("Name the group" in warning for warning in draft["warnings"])
    assert draft["result"] == {
        "error": "Name the group to collect members from, for example @example_group."}
    assert bench.scheduler.records == {}


def test_work_mode_does_not_unlock_a_refused_prompt(bench):
    draft = _draft(bench, "Add the members who agreed to join.", mode="work")

    assert draft["action"] == "unsupported"
    assert "result" not in draft
    assert bench.scheduler.records == {}


def test_logs_tool_honours_the_requested_line_count(bench):
    draft = _draft(bench, "Show the last 5 log lines.", mode="work")

    assert draft["payload"]["command"] == "logs-recent"
    assert bench.logs.limits == [5]
    assert len(draft["result"]["items"]) == 5


def test_work_run_suggests_two_concrete_follow_ups(bench):
    draft = _draft(bench, "Collect members from @example_group every day at 09:00, limit 200.",
                   mode="work")

    assert [step["label"] for step in draft["next"]] == [
        "List your automations", "Give me a health summary"]
    assert all(step["instruction"] for step in draft["next"])


def test_enhance_names_the_group_cadence_and_limit(bench):
    response = bench.client.post("/api/copilot/enhance", json={
        "instruction": "Collect members from @example_group weekly at 14:30, limit 250."})

    body = response.json()
    assert body["instruction"] == (
        "Collect members from @example_group every week at 14:30, limit 250.")
    assert "named the group" in body["added"]
    assert any("Run button" in item for item in body["added"])


def test_enhance_asks_for_the_missing_group(bench):
    response = bench.client.post("/api/copilot/enhance", json={
        "instruction": "Schedule to scrape members every day."})

    body = response.json()
    assert body["instruction"].startswith("Collect members every day at 09:00, limit 200.")
    assert "Name the group" in body["instruction"]
    assert "asked for the group" in body["added"]


def test_enhance_keeps_the_ask_gate_on_a_toggle(bench):
    response = bench.client.post("/api/copilot/enhance", json={
        "instruction": "Pause the member scrape automation."})

    body = response.json()
    assert body["instruction"] == "Pause the member scrape automation."
    assert "named the automation" in body["added"]
    assert any("paused" in item for item in body["added"])
    assert any("Run button" in item for item in body["added"])


def test_enhance_asks_which_automation_to_switch(bench):
    response = bench.client.post("/api/copilot/enhance", json={
        "instruction": "Pause the automation."})

    assert "Name which automation to switch" in response.json()["instruction"]


def test_preset_draft_carries_the_mode_and_keeps_ops_gated(bench):
    response = bench.client.post("/api/copilot/preset-draft", json={
        "preset_id": "automation-status", "mode": "work"})

    draft = response.json()["draft"]
    assert draft["mode"] == "work"
    assert draft["action"] == "ops"
    assert draft["requires_confirmation"] is True
    assert "result" not in draft


def test_preset_command_runs_in_ask_mode(bench):
    response = bench.client.post("/api/copilot/preset-draft", json={
        "preset_id": "knowledge-search",
        "instruction": "Search my knowledge base for the refund policy."})

    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["result"]["query"].startswith("the refund policy")
