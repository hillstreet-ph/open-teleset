"""Node workflows: one chain of internal actions, saved per operator and run in order.

The graph is deliberately narrow. These tests pin the operator boundary, the
chain rules, the refusal of anything outside the internal action list, and the
runner's per-step progress, because that is what keeps the canvas from becoming
a general automation surface.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import scheduler as scheduler_module
import tables
import workflows
from open_teleset import operator_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    workflows._RUNS.clear()
    workflows._RUNNING.clear()
    workflows._TASKS.clear()
    yield


def _client(operator_id="operator-1"):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id=operator_id, role="admin")
        return await call_next(request)

    app.include_router(workflows.router)
    return TestClient(app)


@pytest.fixture
def client():
    return _client()


def _node(node_id, node_type, config, x=40, y=40):
    return {"id": node_id, "type": node_type, "config": config, "x": x, "y": y}


def _chain(*nodes):
    return {"edges": [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def _create(client, name="Weekly sweep", nodes=None, edges=None):
    nodes = nodes or [_node("n1", "scrape", {"target": "@news", "limit": 25})]
    payload = {"name": name, "nodes": nodes,
               "edges": edges if edges is not None else _chain(*nodes)["edges"]}
    response = client.post("/api/workflows", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["workflow"]


# ---------------- validation ----------------


def test_a_single_scrape_node_is_a_valid_chain(client):
    workflow = _create(client, nodes=[_node("n1", "scrape", {"target": "@news", "limit": 25})])
    assert workflow["name"] == "Weekly sweep"
    assert workflow["nodes"][0]["config"] == {"target": "@news", "limit": 25, "filter_bots": True}
    assert workflow["edges"] == []
    listing = client.get("/api/workflows").json()
    assert listing["capacity"]["used"] == 1
    assert [item["type"] for item in listing["catalog"]] == [
        "scrape", "group_info", "table_read", "table_append", "join", "wait", "notify"]


def test_a_chain_keeps_operator_order(client):
    nodes = [
        _node("n1", "scrape", {"target": "@news", "limit": 10}),
        _node("n2", "wait", {"seconds": 30}),
        _node("n3", "notify", {"text": "Scraped {scraped}"}),
    ]
    workflow = _create(client, nodes=nodes)
    assert [edge["from"] for edge in workflow["edges"]] == ["n1", "n2"]
    assert [node["id"] for node in workflows._ordered_nodes(workflow)] == ["n1", "n2", "n3"]


@pytest.mark.parametrize("nodes,edges,message", [
    ([_node("n1", "wait", {"seconds": 5}), _node("n2", "wait", {"seconds": 5})], [], "single starting node"),
    ([_node("n1", "wait", {"seconds": 5}), _node("n2", "wait", {"seconds": 5})],
     [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n1"}], "single starting node"),
    ([_node("n1", "wait", {"seconds": 5}), _node("n2", "wait", {"seconds": 5})],
     [{"from": "n1", "to": "n2"}, {"from": "n1", "to": "n2"}], "once"),
    ([_node("n1", "wait", {"seconds": 5})], [{"from": "n1", "to": "n9"}], "not on the canvas"),
    ([_node("n1", "add_members", {"dest_target": "@x"})], [], "not one of the supported"),
    ([_node("n1", "scrape", {"target": "", "limit": 10})], [], "needs the group"),
    ([_node("n1", "scrape", {"target": "@x", "limit": 5000})], [], "1 to 1000"),
    ([_node("n1", "join", {"urls": ["@a"] * 10001})], [], "1 and 10000"),
    ([_node("n1", "join", {"urls": ["@a"], "delay": 5})], [], "35 or more"),
    ([_node("n1", "wait", {"seconds": 601})], [], "1 to 600"),
    ([_node("n1", "notify", {"text": ""})], [], "between 1 and 1000"),
    ([_node("n1", "note", {"text": "hi"})], [], "not one of the supported"),
])
def test_the_canvas_refuses_broken_or_out_of_scope_graphs(client, nodes, edges, message):
    response = client.post("/api/workflows", json={"name": "Broken", "nodes": nodes, "edges": edges})
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_a_workflow_needs_a_name(client):
    response = client.post("/api/workflows", json={"name": "  ", "nodes": [_node("n1", "wait", {"seconds": 5})]})
    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_total_wait_is_bounded(client):
    nodes = [_node(f"n{i}", "wait", {"seconds": 600}) for i in range(4)]
    response = client.post("/api/workflows", json={"name": "Slow", "nodes": nodes, "edges": _chain(*nodes)["edges"]})
    assert response.status_code == 400
    assert "1800 seconds" in response.json()["detail"]


# ---------------- storage and ownership ----------------


def test_workflows_are_encrypted_and_scoped_to_the_operator(client, tmp_path):
    _create(client)
    stored = (tmp_path / "workflows.secret.json").read_text()
    assert "Weekly sweep" not in stored
    assert "@news" not in stored

    other = _client("operator-2")
    assert other.get("/api/workflows").json()["workflows"] == []
    assert other.put(f"/api/workflows/{_create(client)['id']}", json={
        "name": "Mine now", "nodes": [_node("n1", "wait", {"seconds": 5})]}).status_code == 404


def test_editing_requires_ownership_and_replaces_the_chain(client):
    workflow = _create(client)
    response = client.put(f"/api/workflows/{workflow['id']}", json={
        "name": "Renamed",
        "nodes": [_node("n1", "wait", {"seconds": 15})],
        "edges": [],
    })
    assert response.status_code == 200
    assert response.json()["workflow"]["name"] == "Renamed"
    assert response.json()["workflow"]["nodes"][0]["config"]["seconds"] == 15


def test_deleting_a_workflow_removes_it(client):
    workflow = _create(client)
    assert client.delete(f"/api/workflows/{workflow['id']}").json() == {"success": True}
    assert client.get("/api/workflows").json()["workflows"] == []
    assert client.delete(f"/api/workflows/{workflow['id']}").status_code == 404


def test_anonymous_callers_are_refused():
    app = FastAPI()
    app.include_router(workflows.router)
    assert TestClient(app).get("/api/workflows").status_code == 403


# ---------------- runner ----------------


def test_execute_runs_each_node_in_chain_order_and_fills_tokens():
    workflow = {
        "id": "wf_test", "name": "Run", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True}),
            _node("n2", "notify", {"text": "Scraped {scraped}"}),
        ],
    }
    seen = []

    async def scrape(config, context, account_id):
        context["scraped"] = 7
        return {"scraped": 7}

    async def notify(config, context, account_id):
        seen.append(config["text"].replace("{scraped}", str(context.get("scraped"))))
        return {"sent": True}

    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run, {"scrape": scrape, "notify": notify}))
    assert run["status"] == "succeeded"
    assert [step["status"] for step in run["steps"]] == ["ok", "ok"]
    assert seen == ["Scraped 7"]
    assert run["finished_at"]


def test_a_failing_step_stops_the_run_without_leaking_the_exception():
    workflow = {
        "id": "wf_test", "name": "Run", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True}),
            _node("n2", "wait", {"seconds": 1}),
        ],
    }

    async def explode(config, context, account_id):
        raise RuntimeError("token=very-secret")

    async def should_not_run(config, context, account_id):
        raise AssertionError("the runner must stop after a failed step")

    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run, {"scrape": explode, "wait": should_not_run}))
    assert run["status"] == "failed"
    assert run["steps"][0]["detail"] == {"error": "step_failed"}
    assert run["steps"][1]["status"] == "pending"
    assert "very-secret" not in str(run)


def test_a_cooldown_stop_is_recorded_and_stops_the_chain():
    workflow = {
        "id": "wf_test", "name": "Run", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "join", {"urls": ["@a"], "delay": 35}),
            _node("n2", "wait", {"seconds": 1}),
        ],
    }

    async def join(config, context, account_id):
        return {"joined": 0, "stopped": "telegram_cooldown", "wait_seconds": 120}

    async def should_not_run(config, context, account_id):
        raise AssertionError("a cooldown must stop the chain")

    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run, {"join": join, "wait": should_not_run}))
    assert run["status"] == "stopped"
    assert run["stopped"] == "telegram_cooldown"
    assert run["steps"][1]["status"] == "pending"


def test_running_a_workflow_requires_a_signed_in_account(client, monkeypatch):
    workflow = _create(client, nodes=[_node("n1", "wait", {"seconds": 1})])
    import account_manager as accounts_module

    monkeypatch.setattr(accounts_module.account_manager, "accounts", {}, raising=False)
    response = client.post(f"/api/workflows/{workflow['id']}/run", json={"account_id": "missing"})
    assert response.status_code == 400
    assert "signed-in account" in response.json()["detail"]


def test_a_run_is_reported_with_its_last_result(client, monkeypatch):
    import account_manager as accounts_module

    monkeypatch.setattr(accounts_module.account_manager, "accounts", {"acc": {}}, raising=False)
    workflow = _create(client, nodes=[_node("n1", "wait", {"seconds": 1})])

    async def fake_wait(config, context, account_id):
        return {"waited_seconds": config["seconds"]}

    monkeypatch.setitem(workflows.HANDLERS, "wait", fake_wait)
    response = client.post(f"/api/workflows/{workflow['id']}/run", json={"account_id": "acc"})
    assert response.status_code == 200
    run_id = response.json()["run"]["id"]
    for _ in range(50):
        runs = client.get(f"/api/workflows/{workflow['id']}/runs").json()["runs"]
        if runs and runs[0]["status"] != "running":
            break
        import time
        time.sleep(0.02)
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["steps"][0]["detail"] == {"waited_seconds": 1}
    assert client.get("/api/workflows").json()["workflows"][0]["last_run"]["id"] == run_id


# ---------------- table node ----------------


def _tables_client(operator_id="operator-1"):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id=operator_id, role="admin")
        return await call_next(request)

    app.include_router(tables.router)
    return TestClient(app)


def _signed_in(monkeypatch, *account_ids):
    import account_manager as accounts_module

    monkeypatch.setattr(accounts_module.account_manager, "accounts",
                        {item: {} for item in account_ids}, raising=False)


def test_table_append_needs_real_ids_and_a_scrape_before_it(client):
    broken = client.post("/api/workflows", json={
        "name": "Save", "nodes": [
            _node("n1", "scrape", {"target": "@news"}),
            _node("n2", "table_append", {"table_id": "nope", "column_id": "c_abc123"})],
        "edges": [{"from": "n1", "to": "n2"}]})
    assert broken.status_code == 400
    assert "Choose the data table" in broken.json()["detail"]

    alone = client.post("/api/workflows", json={
        "name": "Save", "nodes": [_node("n1", "table_append",
                                       {"table_id": "t_abc123", "column_id": "c_abc123"})],
        "edges": []})
    assert alone.status_code == 400
    assert "Put a Scrape members node before" in alone.json()["detail"]

    workflow = _create(client, nodes=[
        _node("n1", "scrape", {"target": "@news"}),
        _node("n2", "table_append", {"table_id": "t_abc123", "column_id": "c_abc123"})])
    assert workflow["nodes"][1]["config"] == {"table_id": "t_abc123", "column_id": "c_abc123"}


def test_the_table_node_saves_scraped_members_once_for_the_operator(client):
    tables_client = _tables_client()
    table = tables_client.post("/api/tables", json={"name": "Members"}).json()["table"]
    column_id = table["columns"][0]["id"]
    workflow = {
        "id": "wf_test", "name": "Sweep", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True}),
            _node("n2", "table_append", {"table_id": table["id"], "column_id": column_id}),
        ],
    }

    async def scrape(config, context, account_id):
        context["members"] = ["@alice", "@bob", "@alice"]
        context["scraped"] = 3
        return {"scraped": 3}

    handlers = {"scrape": scrape, "table_append": workflows.HANDLERS["table_append"]}
    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run, handlers, operator="operator-1"))
    assert run["status"] == "succeeded"
    assert run["steps"][1]["detail"] == {"appended": 2, "skipped": 1, "total": 2}
    stored = tables_client.get(f"/api/tables/{table['id']}").json()["table"]
    assert [row["cells"][column_id] for row in stored["rows"]] == ["@alice", "@bob"]

    again = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", again, handlers, operator="operator-1"))
    assert again["steps"][1]["detail"] == {"appended": 0, "skipped": 3, "total": 2}


def test_the_table_node_without_an_operator_fails_closed():
    workflow = {
        "id": "wf_test", "name": "Sweep", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True}),
            _node("n2", "table_append", {"table_id": "t_abc123", "column_id": "c_abc123"}),
        ],
    }

    async def scrape(config, context, account_id):
        context["members"] = ["@alice"]
        return {"scraped": 1}

    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run,
                                  {"scrape": scrape, "table_append": workflows.HANDLERS["table_append"]},
                                  operator=""))
    assert run["status"] == "failed"
    assert run["steps"][1]["detail"] == {"error": "operator_required"}


# ---------------- scrape action ----------------


@pytest.mark.asyncio
async def test_a_scrape_node_degrades_to_recent_posters_without_admin_rights(monkeypatch):
    from telethon import errors, functions, types

    class NonAdminClient:
        async def get_entity(self, target):
            return types.Channel(id=9, title="Hidden Members", photo=types.ChatPhotoEmpty(),
                                 date=None, megagroup=False, broadcast=False)

        async def get_participants(self, entity, limit):
            raise errors.ChatAdminRequiredError(request=None)

        async def __call__(self, request):
            assert isinstance(request, functions.channels.GetFullChannelRequest)
            return SimpleNamespace(full_chat=SimpleNamespace(participants_count=1500))

        def iter_messages(self, entity, limit):
            async def history():
                yield SimpleNamespace(sender=types.User(id=31, first_name="Recent", username="recent_wf"))

            return history()

    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=NonAdminClient()))
    context = {}
    result = await workflows._scrape_members(
        {"target": "@hidden_members", "limit": 25, "filter_bots": True}, context, "acc")

    assert result["source"] == "recent_posters"
    assert result["limited"] is True
    assert result["member_count"] == 1500
    assert context["members"] == ["@recent_wf"]


# ---------------- multiple accounts ----------------


def test_accounts_run_sequentially_with_a_gap_and_fresh_contexts(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(workflows.asyncio, "sleep", sleep)
    order = []

    async def scrape(config, context, account_id):
        order.append(account_id)
        context.setdefault("members", []).append(f"@{account_id}")
        return {"scraped": len(context["members"])}

    async def append(config, context, account_id):
        return {"appended": len(context.get("members") or []), "skipped": 0, "total": 1}

    workflow = {
        "id": "wf_test", "name": "Run", "edges": [{"from": "n1", "to": "n2"}],
        "nodes": [
            _node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True}),
            _node("n2", "table_append", {"table_id": "t_abc123", "column_id": "c_abc123"}),
        ],
    }
    run = workflows._new_run(workflow, ["acc-one", "acc-two"])
    assert [step["account_id"] for step in run["steps"]] == ["acc-one", "acc-one", "acc-two", "acc-two"]
    asyncio.run(workflows.execute(workflow, ["acc-one", "acc-two"], run,
                                  {"scrape": scrape, "table_append": append}))
    assert order == ["acc-one", "acc-two"]
    assert run["status"] == "succeeded"
    assert [step["detail"]["appended"] for step in run["steps"] if step["type"] == "table_append"] == [1, 1]
    sleep.assert_awaited_once_with(workflows.ACCOUNT_GAP_SECONDS)


def test_a_failed_account_stops_the_accounts_after_it(monkeypatch):
    monkeypatch.setattr(workflows.asyncio, "sleep", AsyncMock())

    async def explode(config, context, account_id):
        raise RuntimeError("token=very-secret")

    workflow = {"id": "wf_test", "name": "Run", "edges": [],
                "nodes": [_node("n1", "scrape", {"target": "@news", "limit": 10, "filter_bots": True})]}
    run = workflows._new_run(workflow, ["acc-one", "acc-two"])
    asyncio.run(workflows.execute(workflow, ["acc-one", "acc-two"], run, {"scrape": explode}))
    assert run["status"] == "failed"
    assert [step["status"] for step in run["steps"]] == ["failed", "pending"]
    assert "very-secret" not in str(run)


@pytest.mark.asyncio
async def test_start_run_dedupes_accounts_and_caps_the_run(monkeypatch):
    monkeypatch.setattr(workflows, "ACCOUNT_GAP_SECONDS", 0)
    workflow = {"id": "wf_cap01", "name": "Cap", "edges": [],
                "nodes": [_node("n1", "wait", {"seconds": 1})]}

    async def fake_wait(config, context, account_id):
        return {"waited_seconds": config["seconds"]}

    with pytest.raises(HTTPException) as empty:
        workflows.start_run(workflow, [])
    assert empty.value.status_code == 400
    assert "at least one" in empty.value.detail

    with pytest.raises(HTTPException) as too_many:
        workflows.start_run(workflow, [f"a{i}" for i in range(6)])
    assert too_many.value.status_code == 400
    assert "at most 5" in too_many.value.detail

    run = workflows.start_run(workflow, ["a", "a", " b ", "b"], handlers={"wait": fake_wait})
    assert run["account_ids"] == ["a", "b"]
    assert len(run["steps"]) == 2

    with pytest.raises(HTTPException) as busy:
        workflows.start_run(workflow, ["a"], handlers={"wait": fake_wait})
    assert busy.value.status_code == 409

    await workflows._TASKS["wf_cap01"]
    assert "wf_cap01" not in workflows._RUNNING
    assert workflows._history("wf_cap01")[0]["status"] == "succeeded"


def test_the_run_route_accepts_an_account_rotation(client, monkeypatch):
    _signed_in(monkeypatch, "a", "b")
    monkeypatch.setattr(workflows, "ACCOUNT_GAP_SECONDS", 0)

    async def fake_wait(config, context, account_id):
        return {"waited_seconds": config["seconds"]}

    monkeypatch.setitem(workflows.HANDLERS, "wait", fake_wait)
    workflow = _create(client, nodes=[_node("n1", "wait", {"seconds": 1})])
    url = f"/api/workflows/{workflow['id']}/run"

    assert client.post(url, json={}).status_code == 400
    ghost = client.post(url, json={"account_ids": ["a", "ghost"]})
    assert ghost.status_code == 400
    assert "signed-in account" in ghost.json()["detail"]

    response = client.post(url, json={"account_ids": ["a", "b"]})
    assert response.status_code == 200, response.text
    run_id = response.json()["run"]["id"]
    assert response.json()["run"]["account_ids"] == ["a", "b"]
    for _ in range(50):
        runs = client.get(f"/api/workflows/{workflow['id']}/runs").json()["runs"]
        if runs and runs[0]["status"] != "running":
            break
        import time
        time.sleep(0.02)
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "succeeded"
    assert [step["account_id"] for step in runs[0]["steps"]] == ["a", "b"]

    legacy = client.post(url, json={"account_id": "a"})
    assert legacy.status_code == 200
    assert legacy.json()["run"]["account_ids"] == ["a"]


# ---------------- templates ----------------


def test_templates_are_drafts_served_as_copies(client):
    response = client.get("/api/workflows/templates")
    assert response.status_code == 200
    templates = response.json()["templates"]
    assert [item["id"] for item in templates] == [
        "member_table", "member_digest", "sweep_save_digest", "join_rollout", "table_rollout"]

    listing = client.get("/api/workflows").json()
    assert listing["capacity"]["accounts"] == 5
    assert [item["id"] for item in listing["templates"]] == [item["id"] for item in templates]
    assert "table_append" in [item["type"] for item in listing["catalog"]]

    templates[0]["nodes"][0]["config"]["target"] = "@mutated"
    again = client.get("/api/workflows/templates").json()["templates"]
    assert again[0]["nodes"][0]["config"]["target"] == ""


# ---------------- schedules ----------------


@pytest.fixture
def isolated_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", str(tmp_path / "accounts" / "schedules.json"))
    fresh = scheduler_module.TaskScheduler()
    monkeypatch.setattr(scheduler_module, "task_scheduler", fresh)
    return fresh


def test_schedule_validation_refuses_bad_repeat_time_and_accounts(client, isolated_scheduler, monkeypatch):
    _signed_in(monkeypatch, "acc", *[f"a{i}" for i in range(6)])
    workflow = _create(client, nodes=[_node("n1", "wait", {"seconds": 1})])
    url = f"/api/workflows/{workflow['id']}/schedule"
    base = {"account_ids": ["acc"], "repeat": "weekly", "hour": 9, "minute": 30, "weekday": 1}
    for overrides, message in [
        ({"repeat": "hourly"}, "once, daily, weekly or on workdays"),
        ({"hour": 24}, "not on the clock"),
        ({"minute": 60}, "not on the clock"),
        ({"weekday": 7}, "not on the clock"),
        ({"account_ids": []}, "signed-in accounts this schedule runs"),
        ({"account_ids": ["ghost"]}, "signed-in accounts this schedule runs"),
        ({"account_ids": [f"a{i}" for i in range(6)]}, "at most 5"),
        ({"repeat": "once", "date": "2020-01-01"}, "still ahead of today"),
        ({"repeat": "once", "date": "tomorrow"}, "YYYY-MM-DD"),
    ]:
        response = client.post(url, json={**base, **overrides})
        assert response.status_code == 400, overrides
        assert message in response.json()["detail"]
    assert isolated_scheduler.schedules == {}


@pytest.mark.parametrize("repeat,extra,cron", [
    ("daily", {}, "30 9 * * *"),
    ("weekly", {"weekday": 1}, "30 9 * * 2"),
    ("workday", {}, "30 9 * * 1-5"),
])
def test_scheduling_persists_a_workflow_run(client, isolated_scheduler, monkeypatch, repeat, extra, cron):
    _signed_in(monkeypatch, "acc")
    workflow = _create(client, name="Weekly sweep", nodes=[_node("n1", "wait", {"seconds": 1})])
    response = client.post(f"/api/workflows/{workflow['id']}/schedule",
                           json={"account_ids": ["acc"], "repeat": repeat,
                                 "hour": 9, "minute": 30, **extra})
    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert schedule["schedule_id"].startswith("wfsch_")
    assert schedule["action"] == "run_workflow"
    assert schedule["workflow_id"] == workflow["id"]
    assert schedule["operator_id"] == "operator-1"
    assert schedule["accounts"] == ["acc"]
    assert schedule["cron"] == cron
    assert schedule["repeat"] == repeat

    listing = client.get("/api/workflows").json()
    assert [item["schedule_id"] for item in listing["schedules"]] == [schedule["schedule_id"]]


def test_a_one_off_schedule_keeps_its_date(client, isolated_scheduler, monkeypatch):
    _signed_in(monkeypatch, "acc")
    workflow = _create(client, nodes=[_node("n1", "wait", {"seconds": 1})])
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    response = client.post(f"/api/workflows/{workflow['id']}/schedule",
                           json={"account_ids": ["acc"], "repeat": "once",
                                 "hour": 8, "minute": 15, "date": future})
    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    day = datetime.strptime(future, "%Y-%m-%d")
    assert schedule["cron"] == f"15 8 {day.day} {day.month} *"
    assert schedule["execute_time"]["day"] == day.day
    assert schedule["execute_time"]["hour"] == 8