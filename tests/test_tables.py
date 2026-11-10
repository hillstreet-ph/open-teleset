"""Data tables: operator-scoped sheets that are encrypted at rest and credential-free.

The contract mirrors the knowledge base: a table belongs to the operator who made
it, nothing readable is written to the plain JSON files, and a cell that looks
like a session string or API key is rejected rather than silently altered.
"""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
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


# ---------------- presets ----------------


def test_the_contacts_preset_builds_a_master_list(client):
    response = client.post("/api/tables", json={"name": "", "preset": "contacts"})
    assert response.status_code == 200, response.text
    table = response.json()["table"]
    assert table["name"] == "Contacts"
    assert [(column["name"], column["type"]) for column in table["columns"]] == [
        ("Username", "text"), ("Name", "text"), ("Status", "text"), ("Notes", "text")]

    named = client.post("/api/tables", json={"name": "VIPs", "preset": "contacts"})
    assert named.json()["table"]["name"] == "VIPs"

    unknown = client.post("/api/tables", json={"preset": "invoices"})
    assert unknown.status_code == 400
    assert "preset" in unknown.json()["detail"]


# ---------------- import ----------------


def _import(client, table_id, **payload):
    return client.post(f"/api/tables/{table_id}/import", json=payload)


def test_csv_import_maps_by_name_and_creates_missing_columns(client):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    score = client.post(f"/api/tables/{table['id']}/columns",
                        json={"name": "Score", "type": "number"}).json()["column"]

    response = _import(client, table["id"], content="name,Score,City\nAlice,42,Paris\n")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert body["mode"] == "append"
    assert body["created_columns"] == ["City"]
    cells = body["table"]["rows"][0]["cells"]
    assert cells[name_col] == "Alice"
    assert cells[score["id"]] == 42
    city = next(column for column in body["table"]["columns"] if column["name"] == "City")
    assert cells[city["id"]] == "Paris"


def test_import_column_map_targets_ids_and_refuses_unknown_targets(client):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    mapped = _import(client, table["id"], content="Handle\nalice\n", column_map={"Handle": name_col})
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["created_columns"] == []
    assert mapped.json()["table"]["rows"][0]["cells"][name_col] == "alice"

    broken = _import(client, table["id"], content="Handle\nbob\n", column_map={"Handle": "c_missing"})
    assert broken.status_code == 400
    assert "not in this table" in broken.json()["detail"]


@pytest.mark.parametrize("content,expected_name,expected_done", [
    ("Name\tDone\nbob\tyes\n", "bob", True),
    ('[{"Name": "carol", "Done": true}]', "carol", True),
    ('{"columns": ["Name", "Done"], "rows": [["dan", false]]}', "dan", False),
    ('{"columns": [{"name": "Name"}, {"name": "Done"}], "rows": [{"Name": "eve", "Done": "on"}]}', "eve", True),
])
def test_import_detects_tsv_and_json_shapes(client, content, expected_name, expected_done):
    table = _create(client)
    name_col = table["columns"][0]["id"]
    done = client.post(f"/api/tables/{table['id']}/columns",
                       json={"name": "Done", "type": "checkbox"}).json()["column"]
    response = _import(client, table["id"], content=content)
    assert response.status_code == 200, response.text
    cells = response.json()["table"]["rows"][0]["cells"]
    assert cells[name_col] == expected_name
    assert cells[done["id"]] is expected_done


@pytest.mark.parametrize("payload,message", [
    ({"format": "xlsx", "content": "Name\na\n"}, "CSV, TSV or JSON"),
    ({"mode": "merge", "content": "Name\na\n"}, "appends"),
    ({"content": "   "}, "Paste or choose"),
    ({"content": "Name,Name\na,b\n"}, "same header"),
    ({"content": "Name\n"}, "header but no rows"),
    ({"content": ",Name\na,b\n"}, "needs a header"),
    ({"format": "json", "content": "{not json"}, "not valid JSON"),
    ({"format": "json", "content": "[1, 2]"}, "object of column values"),
    ({"format": "json", "content": '{"columns": ["Name"], "rows": [["a", "b"]]}'}, "more values than columns"),
    ({"format": "json", "content": '"just a string"'}, "list of rows"),
])
def test_import_refuses_broken_input(client, payload, message):
    table = _create(client)
    response = _import(client, table["id"], **payload)
    assert response.status_code == 400, response.text
    assert message in response.json()["detail"]
    assert client.get(f"/api/tables/{table['id']}").json()["table"]["rows"] == []


def test_import_is_size_capped(client):
    table = _create(client)
    response = _import(client, table["id"], content="Name\n" + "x\n" * 300_001)
    assert response.status_code == 413


def test_import_row_cap_and_replace_mode(client, monkeypatch):
    monkeypatch.setattr(tables, "MAX_ROWS", 1)
    table = _create(client)
    name_col = table["columns"][0]["id"]
    assert _import(client, table["id"], content="Name\nalice\n").status_code == 200

    overflow = _import(client, table["id"], content="Name\nbob\n")
    assert overflow.status_code == 409
    assert "1 rows" in overflow.json()["detail"]

    replaced = _import(client, table["id"], content="Name\ncarol\n", mode="replace")
    assert replaced.status_code == 200, replaced.text
    assert [row["cells"][name_col] for row in replaced.json()["table"]["rows"]] == ["carol"]


def test_import_coerces_checkboxes_and_slash_dates(client):
    table = _create(client)
    done = client.post(f"/api/tables/{table['id']}/columns",
                       json={"name": "Done", "type": "checkbox"}).json()["column"]
    due = client.post(f"/api/tables/{table['id']}/columns",
                      json={"name": "Due", "type": "date"}).json()["column"]
    response = _import(client, table["id"], content="Name,Done,Due\nalice,yes,2026/11/7\n")
    assert response.status_code == 200, response.text
    cells = response.json()["table"]["rows"][0]["cells"]
    assert cells[done["id"]] is True
    assert cells[due["id"]] == "2026-11-07"

    bad = _import(client, table["id"], content="Name,Done\nbob,maybe\n")
    assert bad.status_code == 400
    assert "Row 2, column 'Done'" in bad.json()["detail"]


def test_import_refuses_secret_shaped_cells(client):
    table = _create(client)
    response = _import(client, table["id"], content="Name\n" + "A" * 512 + "\n")
    assert response.status_code == 400
    assert "credential" in response.json()["detail"].lower()
    assert client.get(f"/api/tables/{table['id']}").json()["table"]["rows"] == []


# ---------------- export ----------------


def test_export_csv_and_json_round_trip(client):
    table = _create(client, "Suppliers")
    name_col = table["columns"][0]["id"]
    done = client.post(f"/api/tables/{table['id']}/columns",
                       json={"name": "Done", "type": "checkbox"}).json()["column"]
    score = client.post(f"/api/tables/{table['id']}/columns",
                        json={"name": "Score", "type": "number"}).json()["column"]
    client.post(f"/api/tables/{table['id']}/rows",
                json={"cells": {name_col: "Acme", done["id"]: True, score["id"]: 7}})
    client.post(f"/api/tables/{table['id']}/rows",
                json={"cells": {name_col: "Beta", done["id"]: False}})

    csv_response = client.get(f"/api/tables/{table['id']}/export", params={"format": "csv"})
    assert csv_response.status_code == 200
    body = csv_response.json()
    assert body["filename"] == "suppliers.csv"
    assert body["rows"] == 2
    assert body["content"].splitlines() == ["Name,Done,Score", "Acme,true,7", "Beta,false,"]

    json_response = client.get(f"/api/tables/{table['id']}/export", params={"format": "json"})
    assert json_response.status_code == 200
    body = json_response.json()
    assert body["filename"] == "suppliers.json"
    payload = json.loads(body["content"])
    assert payload["name"] == "Suppliers"
    assert payload["columns"] == [{"name": "Name", "type": "text"},
                                  {"name": "Done", "type": "checkbox"},
                                  {"name": "Score", "type": "number"}]
    assert payload["rows"] == [{"Name": "Acme", "Done": True, "Score": 7},
                               {"Name": "Beta", "Done": False, "Score": None}]

    assert client.get(f"/api/tables/{table['id']}/export",
                      params={"format": "xlsx"}).status_code == 400

    fresh = _create(client, "Copy")
    reimported = _import(client, fresh["id"], content=body["content"], format="json")
    assert reimported.status_code == 200, reimported.text
    assert reimported.json()["imported"] == 2
    assert reimported.json()["created_columns"] == ["Done", "Score"]


# ---------------- workflow append helper ----------------


def test_append_to_table_dedupes_case_insensitively_and_skips_blanks(client):
    table = _create(client)
    column_id = table["columns"][0]["id"]
    first = tables.append_to_table("operator-1", table["id"], column_id,
                                   ["@Alice", " @alice ", "", "  ", "@bob"])
    assert first == {"appended": 2, "skipped": 1, "total": 2}
    second = tables.append_to_table("operator-1", table["id"], column_id, ["@ALICE", "@carol"])
    assert second == {"appended": 1, "skipped": 1, "total": 3}
    stored = client.get(f"/api/tables/{table['id']}").json()["table"]
    assert [row["cells"][column_id] for row in stored["rows"]] == ["@Alice", "@bob", "@carol"]


def test_append_to_table_refuses_non_text_columns_and_unknown_tables(client):
    table = _create(client)
    number = client.post(f"/api/tables/{table['id']}/columns",
                         json={"name": "Score", "type": "number"}).json()["column"]
    with pytest.raises(HTTPException) as wrong_type:
        tables.append_to_table("operator-1", table["id"], number["id"], ["1"])
    assert wrong_type.value.status_code == 400
    assert "text column" in wrong_type.value.detail

    with pytest.raises(HTTPException) as missing:
        tables.append_to_table("operator-1", "t_missing", table["columns"][0]["id"], ["a"])
    assert missing.value.status_code == 404


def test_append_to_table_stops_at_the_row_cap(client, monkeypatch):
    monkeypatch.setattr(tables, "MAX_ROWS", 2)
    table = _create(client)
    result = tables.append_to_table("operator-1", table["id"], table["columns"][0]["id"],
                                    ["a", "b", "c"])
    assert result == {"appended": 2, "skipped": 0, "total": 2}
