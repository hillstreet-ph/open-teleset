"""Workflow nodes tell the truth and cover the self-actions an operator can chain.

The canvas used to stop at five node types, count a successful join as a
failure, and report a failed step as the bare word "step_failed" with no way
for the operator to tell a disconnected account apart from a Telegram limit.
These tests pin the completed catalog, honest join counts, safe failure
reasons, the table read/write pair, and joining from a table.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tables
import workflows


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    workflows._RUNS.clear()
    workflows._RUNNING.clear()
    workflows._TASKS.clear()
    yield


def _client(operator_id="operator-1", router=None):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id=operator_id, role="admin")
        return await call_next(request)

    app.include_router(router or workflows.router)
    return TestClient(app)


@pytest.fixture
def client():
    return _client()


def _node(node_id, node_type, config, x=40, y=40):
    return {"id": node_id, "type": node_type, "config": config, "x": x, "y": y}


def _create(client, nodes, edges=None):
    payload = {"name": "Nodes", "nodes": nodes,
               "edges": edges if edges is not None else
               [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    response = client.post("/api/workflows", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["workflow"]


def _table(rows=("@alice", "@bob")):
    client = _client(router=tables.router)
    table = client.post("/api/tables", json={"name": "Links"}).json()["table"]
    column_id = table["columns"][0]["id"]
    for value in rows:
        assert client.post(f"/api/tables/{table['id']}/rows",
                           json={"cells": {column_id: value}}).status_code == 200
    return table, column_id


# ---------------- the completed catalog ----------------


def test_the_catalog_covers_every_chained_self_action(client):
    catalog = client.get("/api/workflows").json()["catalog"]
    assert [item["type"] for item in catalog] == [
        "scrape", "group_info", "table_read", "table_append", "join", "wait", "notify"]
    kinds = {(item["type"], field["key"]): field["kind"]
             for item in catalog for field in item["fields"]}
    assert kinds[("group_info", "target")] == "text"
    assert kinds[("table_read", "table_id")] == "table"
    assert kinds[("table_read", "column_id")] == "column"
    assert kinds[("join", "from_table")] == "checkbox"


def test_inspect_and_read_nodes_round_trip_through_validation(client):
    table, column_id = _table()
    workflow = _create(client, [
        _node("n1", "group_info", {"target": "@news"}),
        _node("n2", "table_read", {"table_id": table["id"], "column_id": column_id}),
        _node("n3", "notify", {"text": "{table} links ready"}),
    ])
    assert [item["type"] for item in workflow["nodes"]] == ["group_info", "table_read", "notify"]
    assert workflow["nodes"][0]["config"] == {"target": "@news"}
    assert workflow["nodes"][1]["config"] == {"table_id": table["id"], "column_id": column_id}


def test_a_table_read_needs_real_ids(client):
    response = client.post("/api/workflows", json={
        "name": "Read", "nodes": [_node("n1", "table_read",
                                       {"table_id": "nope", "column_id": "c_abc123"})],
        "edges": []})
    assert response.status_code == 400
    assert "Choose the data table" in response.json()["detail"]


# ---------------- join from a table ----------------


def test_join_from_a_table_needs_a_table_read_before_it(client):
    response = client.post("/api/workflows", json={
        "name": "Join", "nodes": [
            _node("n1", "wait", {"seconds": 5}),
            _node("n2", "join", {"urls": [], "delay": 35, "from_table": True})],
        "edges": [{"from": "n1", "to": "n2"}]})
    assert response.status_code == 400
    assert "Read a table" in response.json()["detail"]

    table, column_id = _table()
    workflow = _create(client, [
        _node("n1", "table_read", {"table_id": table["id"], "column_id": column_id}),
        _node("n2", "join", {"urls": [], "delay": 35, "from_table": True}),
    ])
    assert workflow["nodes"][1]["config"]["from_table"] is True
    assert workflow["nodes"][1]["config"]["urls"] == []


def test_a_manual_join_still_needs_links(client):
    response = client.post("/api/workflows", json={
        "name": "Join", "nodes": [_node("n1", "join", {"urls": [], "delay": 35})],
        "edges": []})
    assert response.status_code == 400
    assert "1 and 10000" in response.json()["detail"]


@pytest.mark.asyncio
async def test_join_counts_successes_and_reports_safe_failures(monkeypatch):
    from telethon import functions

    slept = AsyncMock()
    monkeypatch.setattr(workflows.asyncio, "sleep", slept)

    class JoinClient:
        def __init__(self):
            self.requests = []

        async def get_entity(self, name):
            return SimpleNamespace(id=7, username=str(name).lstrip("@"))

        async def __call__(self, request):
            self.requests.append(type(request).__name__)
            return SimpleNamespace()

    fake = JoinClient()
    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=fake))
    context = {}
    result = await workflows._join_groups(
        {"urls": ["@alpha", "https://t.me/+abc123", "not a link"], "delay": 35,
         "from_table": False}, context, "acc")

    assert result == {"joined": 2, "skipped": 0, "failed": 1, "reasons": {"invalid_link": 1}}
    assert context["joined"] == 2
    assert "JoinChannelRequest" in fake.requests
    assert "ImportChatInviteRequest" in fake.requests
    slept.assert_awaited()


@pytest.mark.asyncio
async def test_join_reads_its_links_from_a_table(monkeypatch):
    monkeypatch.setattr(workflows.asyncio, "sleep", AsyncMock())

    class JoinClient:
        async def get_entity(self, name):
            return SimpleNamespace(id=7, username=str(name))

        async def __call__(self, request):
            return SimpleNamespace()

    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=JoinClient()))
    context = {"table_values": ["@fromtable"]}
    result = await workflows._join_groups(
        {"urls": [], "delay": 35, "from_table": True}, context, "acc")
    assert result["joined"] == 1


@pytest.mark.asyncio
async def test_join_reports_a_cooldown_without_counting_it_as_a_failure(monkeypatch):
    from telethon import errors

    monkeypatch.setattr(workflows.asyncio, "sleep", AsyncMock())

    class FloodingClient:
        async def get_entity(self, name):
            return SimpleNamespace(id=7)

        async def __call__(self, request):
            raise errors.FloodWaitError(request=request, capture=120)

    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=FloodingClient()))
    context = {}
    result = await workflows._join_groups(
        {"urls": ["@alpha"], "delay": 35, "from_table": False}, context, "acc")
    assert result["stopped"] == "telegram_cooldown"
    assert result["wait_seconds"] == 120
    assert result["failed"] == 0


# ---------------- safe failure reasons ----------------


def _run_with(handler):
    workflow = {"id": "wf_test", "name": "Run", "edges": [],
                "nodes": [_node("n1", "wait", {"seconds": 1})]}
    run = workflows._new_run(workflow)
    asyncio.run(workflows.execute(workflow, "acc", run, {"wait": handler}))
    return run["steps"][0]["detail"]


def test_a_disconnected_account_is_named_not_swallowed():
    async def blocked(config, context, account_id):
        raise RuntimeError("account_not_connected")

    assert _run_with(blocked) == {"error": "account_not_connected"}


def test_a_timeout_is_named_not_swallowed():
    async def slow(config, context, account_id):
        raise asyncio.TimeoutError()

    assert _run_with(slow) == {"error": "timed_out"}


def test_a_telegram_error_is_named_by_class_never_by_text():
    from telethon import errors

    async def limited(config, context, account_id):
        raise errors.PeerFloodError(request=None)

    assert _run_with(limited) == {"error": "telegram_peer_flood"}


def test_an_unknown_failure_never_leaks_its_text():
    async def explode(config, context, account_id):
        raise RuntimeError("token=very-secret")

    assert _run_with(explode) == {"error": "step_failed"}


# ---------------- the read and inspect handlers ----------------


@pytest.mark.asyncio
async def test_the_table_read_node_loads_its_column_for_later_nodes():
    table, column_id = _table(("@alice", "@bob", "@alice"))
    context = {"_operator": "operator-1"}
    detail = await workflows._read_table(
        {"table_id": table["id"], "column_id": column_id}, context, "acc")
    assert detail == {"rows": 3, "values": 3}
    assert context["table_values"] == ["@alice", "@bob", "@alice"]
    assert context["table"] == 3


@pytest.mark.asyncio
async def test_the_table_read_node_needs_the_operator():
    with pytest.raises(RuntimeError, match="operator_required"):
        await workflows._read_table(
            {"table_id": "t_abc123", "column_id": "c_abc123"}, {}, "acc")


@pytest.mark.asyncio
async def test_the_group_info_node_reports_title_and_members(monkeypatch):
    from telethon import functions

    class InfoClient:
        async def get_entity(self, target):
            return SimpleNamespace(id=42, title="News", username="news",
                                   megagroup=False, broadcast=True)

        async def __call__(self, request):
            assert isinstance(request, functions.channels.GetFullChannelRequest)
            return SimpleNamespace(full_chat=SimpleNamespace(participants_count=321))

    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=InfoClient()))
    context = {}
    detail = await workflows._group_info({"target": "@news"}, context, "acc")
    assert detail == {"title": "News", "members": 321, "kind": "channel"}
    assert context["group"]["title"] == "News"


# ---------------- notify tokens ----------------


@pytest.mark.asyncio
async def test_the_note_node_fills_in_the_table_count(monkeypatch):
    sent = {}

    class NoteClient:
        async def send_message(self, target, text):
            sent["target"] = target
            sent["text"] = text

    monkeypatch.setattr(workflows, "_client", AsyncMock(return_value=NoteClient()))
    await workflows._notify_self(
        {"text": "{scraped} members, {table} table rows, {joined} joins"},
        {"scraped": 4, "table": 3, "joined": 2}, "acc")
    assert sent == {"target": "me", "text": "4 members, 3 table rows, 2 joins"}
