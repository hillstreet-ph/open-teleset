"""Notes: the operator's scratchpad, scoped to them and encrypted on disk.

The dashboard's Notes tab is for text the operator wants to keep for later - a
draft message, a snippet, a reminder. It is not the knowledge base: nothing here
is fed to an AI, and it has its own store. The rules that matter are the same
ones the knowledge base follows: a note belongs to the operator who wrote it, a
credential-shaped value is masked before it is stored, and the file on disk is
encrypted.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import notes
from open_teleset import operator_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({})
    yield


def _client(operator_id, tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = (SimpleNamespace(id=operator_id, role="admin")
                              if operator_id else None)
        return await call_next(request)

    app.include_router(notes.router)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _client("operator-1", tmp_path, monkeypatch)


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    return _client(None, tmp_path, monkeypatch)


def test_anonymous_operator_is_refused(anon_client):
    assert anon_client.get("/api/notes").status_code == 403
    assert anon_client.post("/api/notes", json={"text": "hello"}).status_code == 403


def test_create_list_and_read_a_note(client):
    created = client.post("/api/notes", json={"text": "Call the supplier on Monday"}).json()
    assert created["success"] is True
    note = created["note"]
    assert note["text"] == "Call the supplier on Monday"
    assert note["title"] == "Call the supplier on Monday"

    listing = client.get("/api/notes").json()
    assert [n["id"] for n in listing["notes"]] == [note["id"]]
    assert "text" not in listing["notes"][0] or listing["notes"][0]["text"] == ""

    read = client.get(f"/api/notes/{note['id']}").json()["note"]
    assert read["text"] == "Call the supplier on Monday"


def test_update_keeps_identity_and_bumps_updated(client):
    note = client.post("/api/notes", json={"title": "Draft", "text": "first"}).json()["note"]
    updated = client.put(f"/api/notes/{note['id']}", json={"text": "second"}).json()["note"]
    assert updated["id"] == note["id"]
    assert updated["title"] == "Draft"
    assert updated["text"] == "second"


def test_delete_removes_the_note(client):
    note = client.post("/api/notes", json={"text": "temporary"}).json()["note"]
    assert client.delete(f"/api/notes/{note['id']}").status_code == 200
    assert client.get(f"/api/notes/{note['id']}").status_code == 404
    assert client.delete(f"/api/notes/{note['id']}").status_code == 404


def test_empty_note_is_rejected(client):
    assert client.post("/api/notes", json={"text": "   "}).status_code == 400


def test_credential_shaped_text_is_masked(client):
    note = client.post("/api/notes",
                       json={"text": "password = hunter2please\nsession string: 1234abcd5678efgh"}
                       ).json()["note"]
    assert note["masked"] is True
    assert "hunter2please" not in note["text"]
    assert "1234abcd5678efgh" not in note["text"]
    assert "[masked]" in note["text"]


def test_note_is_encrypted_at_rest(client, tmp_path):
    client.post("/api/notes", json={"text": "plaintext marker zqxjkv"})
    blob = (tmp_path / "notes.secret.json").read_text(encoding="utf-8")
    assert "zqxjkv" not in blob


def test_notes_are_scoped_to_the_operator(tmp_path, monkeypatch):
    first = _client("operator-1", tmp_path, monkeypatch)
    note = first.post("/api/notes", json={"text": "mine"}).json()["note"]

    second = _client("operator-2", tmp_path, monkeypatch)
    assert second.get("/api/notes").json()["notes"] == []
    assert second.get(f"/api/notes/{note['id']}").status_code == 404
    assert second.delete(f"/api/notes/{note['id']}").status_code == 404


def test_newest_note_is_first(client):
    client.post("/api/notes", json={"text": "older"})
    client.post("/api/notes", json={"text": "newer"})
    titles = [n["title"] for n in client.get("/api/notes").json()["notes"]]
    assert titles == ["newer", "older"]
