"""Data tables: operator-scoped sheets that are encrypted at rest and credential-free.

The contract mirrors the knowledge base: a table belongs to the operator who made
it, nothing readable is written to the plain JSON files, and a cell that looks
like a session string or API key is rejected rather than silently altered.
"""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tables
from open_teleset import operator_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


def _client(operator_id="operator-1"):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id=operator_id, role="admin")
        return await call_next(request)

    app.include_router(tables.router)
    return TestClient(app)


@pytest.fixture
def client():
    return _client()


def _create(client, name="Work list"):
    response = client.post("/api/tables", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["table"]


def test_creating_a_table_starts_with_a_name_column(client):
    table = _create(client)
    assert table["name"] == "Work list"
    assert [c["name"] for c in table["columns"]] == ["Name"]
    assert table["columns"][0]["type"] == "text"
    assert table["rows"] == []

    listing = client.get("/api/tables").json()
    assert listing["capacity"]["used"] == 1
    assert listing["tables"][0]["rows"] == 0
    assert client.get(f"/api/tables/{table['id']}").json()["table"]["id"] == table["id"]


def test_columns_are_typed_and_last_column_is_protected(client):
    table = _create(client)
    score = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Score", "type": "number"})
    assert score.status_code == 200
    column = score.json()["column"]
    assert column["type"] == "number"
    notes = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Notes", "type": "text"}).json()["column"]

    bad_type = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Nope", "type": "json"})
    assert bad_type.status_code == 400

    assert client.delete(f"/api/tables/{table['id']}/columns/{column['id']}").status_code == 200
    assert client.delete(f"/api/tables/{table['id']}/columns/{notes['id']}").status_code == 200
    assert client.delete(f"/api/tables/{table['id']}/columns/{table['columns'][0]['id']}").status_code == 409


def test_rows_are_added_updated_and_deleted(client):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    added = client.post(f"/api/tables/{table['id']}/rows", json={"cells": {name_col: "first"}})
    assert added.status_code == 200, added.text
    row = added.json()["row"]
    assert row["cells"][name_col] == "first"

    updated = client.patch(f"/api/tables/{table['id']}/rows/{row['id']}", json={"cells": {name_col: "second"}})
    assert updated.status_code == 200
    assert updated.json()["row"]["cells"][name_col] == "second"

    deleted = client.delete(f"/api/tables/{table['id']}/rows/{row['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["table"]["rows"] == []


def test_each_column_type_validates_and_normalises(client):
    table = _create(client)
    number = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Score", "type": "number"}).json()["column"]
    day = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Due", "type": "date"}).json()["column"]
    done = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Done", "type": "checkbox"}).json()["column"]
    name_col = table["columns"][0]["id"]

    row = client.post(f"/api/tables/{table['id']}/rows", json={"cells": {
        name_col: "first", number["id"]: "42", day["id"]: "2026-11-07", done["id"]: True,
    }}).json()["row"]
    assert row["cells"][number["id"]] == 42
    assert row["cells"][day["id"]] == "2026-11-07"
    assert row["cells"][done["id"]] is True

    for column_id, value in [(number["id"], "many"), (day["id"], "tomorrow"), (done["id"], "yes")]:
        failure = client.post(f"/api/tables/{table['id']}/rows", json={"cells": {column_id: value}})
        assert failure.status_code == 400, value


def test_secret_shaped_cells_are_rejected_not_masked(client):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    for value in ("A" * 512, "1" + "B" * 399):
        response = client.post(f"/api/tables/{table['id']}/rows", json={"cells": {name_col: value}})
        assert response.status_code == 400
        assert "credential" in response.json()["detail"].lower()
    assert client.get(f"/api/tables/{table['id']}").json()["table"]["rows"] == []


def test_renaming_column_deletes_its_cells_and_changing_type_rechecks_values(client):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    text = client.post(f"/api/tables/{table['id']}/columns", json={"name": "Extra", "type": "text"}).json()["column"]
    client.post(f"/api/tables/{table['id']}/rows", json={"cells": {name_col: "row", text["id"]: "not a number"}})

    rejected = client.patch(f"/api/tables/{table['id']}/columns/{text['id']}", json={"type": "number"})
    assert rejected.status_code == 409

    renamed = client.patch(f"/api/tables/{table['id']}/columns/{text['id']}", json={"name": "Notes"})
    assert renamed.status_code == 200
    assert renamed.json()["column"]["name"] == "Notes"

    removed = client.delete(f"/api/tables/{table['id']}/columns/{text['id']}")
    assert removed.status_code == 200
    assert all(text["id"] not in row["cells"] for row in removed.json()["table"]["rows"])


def test_tables_are_isolated_per_operator(client):
    table = _create(client)
    other = _client("operator-2")
    assert other.get("/api/tables").json()["tables"] == []
    assert other.get(f"/api/tables/{table['id']}").status_code == 404


def test_table_cap_and_answers_404_for_unknown_ids(client, monkeypatch):
    monkeypatch.setattr(tables, "MAX_TABLES", 2)
    _create(client, "one")
    _create(client, "two")
    assert client.post("/api/tables", json={"name": "three"}).status_code == 409
    assert client.get("/api/tables/t_missing").status_code == 404
    assert client.delete("/api/tables/t_missing").status_code == 404


def test_anonymous_and_unconfigured_stores_are_refused(client, monkeypatch, tmp_path):
    anon = FastAPI()

    @anon.middleware("http")
    async def no_identity(request, call_next):
        request.state.user = None
        return await call_next(request)

    anon.include_router(tables.router)
    assert TestClient(anon).get("/api/tables").status_code == 403

    permissive = _client()
    monkeypatch.setattr(tables, "secret_key_available", lambda: False)
    assert permissive.get("/api/tables").status_code == 500


def test_stored_bytes_never_contain_cell_text(client, tmp_path):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    client.post(f"/api/tables/{table['id']}/rows", json={"cells": {name_col: "quarterly supplier list"}})
    raw = (tmp_path / "tables.secret.json").read_text()
    assert "quarterly supplier list" not in raw
    payload = json.loads(raw)
    assert set(payload) == {"ciphertext"}
    reloaded = operator_store.read_secret("tables", {})
    assert reloaded["operator-1"]["tables"][0]["rows"][0]["cells"][name_col] == "quarterly supplier list"


def test_rename_and_delete_table(client):
    table = _create(client)
    renamed = client.patch(f"/api/tables/{table['id']}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["table"]["name"] == "Renamed"
    assert client.delete(f"/api/tables/{table['id']}").status_code == 200
    assert client.get(f"/api/tables/{table['id']}").status_code == 404
