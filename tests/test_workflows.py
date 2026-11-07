"""Node workflows: one chain of internal actions, saved per operator and run in order.

The graph is deliberately narrow. These tests pin the operator boundary, the
chain rules, the refusal of anything outside the internal action list, and the
runner's per-step progress, because that is what keeps the canvas from becoming
a general automation surface.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    assert [item["type"] for item in listing["catalog"]] == ["scrape", "wait", "join", "notify"]


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
    ([_node("n1", "join", {"urls": ["@a", "@b", "@c", "@d", "@e", "@f", "@g", "@h", "@i", "@j", "@k"]})], [], "1 and 10"),
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