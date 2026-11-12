"""Campaigns: saved discovery presets, scoped to the operator.

A campaign is the set of finder filters the operator reuses - keywords,
category, country, language, type, member threshold and result limit. It is
stored with the same encrypted, operator-scoped store the notes use. Proxy
credentials are deliberately not part of a campaign: they never reach disk.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import campaigns
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

    app.include_router(campaigns.router)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _client("operator-1", tmp_path, monkeypatch)


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    return _client(None, tmp_path, monkeypatch)


def _payload(**overrides):
    body = {
        "name": "PH casino agents",
        "keywords": "casino agent philippines\nonline casino ph",
        "category": "casino",
        "country": "Philippines",
        "language": "tagalog",
        "group_type": "group",
        "min_members": 500,
        "limit": 300,
    }
    body.update(overrides)
    return body


def test_anonymous_operator_is_refused(anon_client):
    assert anon_client.get("/api/campaigns").status_code == 403
    assert anon_client.post("/api/campaigns", json=_payload()).status_code == 403


def test_create_list_and_read_a_campaign(client):
    created = client.post("/api/campaigns", json=_payload())
    assert created.status_code == 200
    campaign = created.json()["campaign"]
    assert campaign["name"] == "PH casino agents"
    assert campaign["category"] == "casino"
    assert campaign["min_members"] == 500
    assert campaign["group_type"] == "group"

    listed = client.get("/api/campaigns").json()
    assert [entry["id"] for entry in listed["campaigns"]] == [campaign["id"]]
    assert listed["capacity"] == {"used": 1, "limit": campaigns.MAX_CAMPAIGNS}

    read = client.get(f"/api/campaigns/{campaign['id']}")
    assert read.status_code == 200
    assert read.json()["campaign"]["keywords"].startswith("casino agent")


def test_campaigns_are_scoped_to_the_operator(client, tmp_path, monkeypatch):
    created = client.post("/api/campaigns", json=_payload()).json()["campaign"]
    other = _client("operator-2", tmp_path, monkeypatch)
    assert other.get("/api/campaigns").json()["campaigns"] == []
    assert other.get(f"/api/campaigns/{created['id']}").status_code == 404


def test_a_blank_name_is_refused(client):
    assert client.post("/api/campaigns", json=_payload(name="   ")).status_code == 400


def test_hostile_values_are_refused(client):
    assert client.post("/api/campaigns", json=_payload(group_type="channelish")).status_code == 422
    assert client.post("/api/campaigns", json=_payload(min_members=-1)).status_code == 422
    assert client.post("/api/campaigns", json=_payload(limit=20000)).status_code == 422


def test_update_renames_and_changes_the_filters(client):
    created = client.post("/api/campaigns", json=_payload()).json()["campaign"]
    updated = client.put(f"/api/campaigns/{created['id']}",
                         json={"name": "PH casino agents v2", "min_members": 1000})
    assert updated.status_code == 200
    campaign = updated.json()["campaign"]
    assert campaign["name"] == "PH casino agents v2"
    assert campaign["min_members"] == 1000
    assert campaign["keywords"] == created["keywords"]
    assert campaign["created"] == created["created"]


def test_delete_removes_only_that_campaign(client):
    first = client.post("/api/campaigns", json=_payload(name="first")).json()["campaign"]
    second = client.post("/api/campaigns", json=_payload(name="second")).json()["campaign"]
    assert client.delete(f"/api/campaigns/{first['id']}").status_code == 200
    remaining = client.get("/api/campaigns").json()["campaigns"]
    assert [entry["id"] for entry in remaining] == [second["id"]]
    assert client.delete(f"/api/campaigns/{first['id']}").status_code == 404


def test_the_library_is_capped(client, monkeypatch):
    monkeypatch.setattr(campaigns, "MAX_CAMPAIGNS", 2)
    assert client.post("/api/campaigns", json=_payload(name="campaign 1")).status_code == 200
    assert client.post("/api/campaigns", json=_payload(name="campaign 2")).status_code == 200
    assert client.post("/api/campaigns", json=_payload(name="overflow")).status_code == 409
