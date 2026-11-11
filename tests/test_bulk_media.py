"""BulkSend attachments: an operator uploads a file once, previews it, sends it.

A media upload is an operator-owned file on the same store as everything else,
addressed by an opaque id. The dashboard never sends a path; the send route
resolves ids server-side. Script-capable types are refused, the size is capped,
and another operator can never read or reference the file.
"""

import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bulk_media
import telegram_ops as ops
from open_teleset import operator_store

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    yield


def _client(operator_id):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = (SimpleNamespace(id=operator_id, role="admin")
                              if operator_id else None)
        return await call_next(request)

    app.include_router(bulk_media.router)
    return TestClient(app)


@pytest.fixture
def client():
    return _client("operator-1")


def upload(client, name="photo.png", content=PNG, mime="image/png"):
    return client.post("/api/bulk/media", files={"file": (name, content, mime)})


def test_anonymous_operator_is_refused():
    anon = _client(None)
    response = upload(anon)
    assert response.status_code == 403
    assert anon.get("/api/bulk/media").status_code == 403


def test_upload_returns_an_opaque_owned_media_record(client):
    response = upload(client)
    assert response.status_code == 200
    media = response.json()["media"]
    assert media["kind"] == "image"
    assert media["name"] == "photo.png"
    assert media["size"] == len(PNG)
    assert media["id"] != "photo.png" and "/" not in media["id"]

    listing = client.get("/api/bulk/media").json()["media"]
    assert [item["id"] for item in listing] == [media["id"]]


def test_download_returns_the_bytes_and_is_scoped_to_the_owner(client):
    media = upload(client).json()["media"]

    served = client.get(f"/api/bulk/media/{media['id']}")
    assert served.status_code == 200
    assert served.content == PNG
    assert served.headers["content-type"].startswith("image/png")
    assert served.headers["x-content-type-options"] == "nosniff"

    other = _client("operator-2")
    assert other.get(f"/api/bulk/media/{media['id']}").status_code == 404
    assert other.delete(f"/api/bulk/media/{media['id']}").status_code == 404
    assert other.get("/api/bulk/media").json()["media"] == []


def test_script_capable_and_unknown_types_are_refused(client):
    for name, mime, body in (
        ("page.html", "text/html", b"<script>alert(1)</script>"),
        ("payload.svg", "image/svg+xml", b"<svg onload=alert(1)>"),
        ("run.sh", "application/x-sh", b"#!/bin/sh"),
        ("mystery.bin", "application/octet-stream", b"\x00\x01"),
    ):
        response = upload(client, name=name, content=body, mime=mime)
        assert response.status_code == 400, name
        assert "not allowed" in response.json()["detail"].lower()


def test_a_file_whose_bytes_do_not_match_its_type_is_refused(client):
    response = upload(client, name="fake.png", content=b"<html>not a png</html>")
    assert response.status_code == 400
    assert "does not match" in response.json()["detail"].lower()


def test_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(bulk_media, "MAX_BYTES", 32)
    response = upload(client, content=PNG)
    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()


def test_delete_removes_record_and_file(client, tmp_path):
    media = upload(client).json()["media"]
    stored = list((tmp_path / "media").iterdir())
    assert len(stored) == 1
    path = stored[0]
    assert path.exists()

    assert client.delete(f"/api/bulk/media/{media['id']}").status_code == 200
    assert client.get(f"/api/bulk/media/{media['id']}").status_code == 404
    assert not path.exists()


# ---------- the send request contract ----------


def test_media_only_message_is_allowed_and_caption_limit_is_enforced():
    request = ops.BulkSendRequest(
        account_id="a1", targets=["recipient"], media_ids=["m1"])
    assert request.selected_accounts() == ["a1"]

    with pytest.raises(ValueError):
        ops.BulkSendRequest(account_id="a1", targets=["recipient"],
                            message="x" * 2000, media_ids=["m1"])

    plain = ops.BulkSendRequest(
        account_id="a1", targets=["recipient"], message="x" * 4000)
    assert len(plain.message) == 4000


def test_at_most_ten_attachments_per_send():
    with pytest.raises(ValueError):
        ops.BulkSendRequest(account_id="a1", targets=["recipient"],
                            media_ids=[f"m{i}" for i in range(11)])


def test_send_route_rejects_unknown_media_ids(monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", "/tmp/does-not-matter")
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(ops.telegram_ops_router)
    response = TestClient(app).post("/api/bulk/send-personal", json={
        "account_id": "a1", "targets": ["recipient"], "media_ids": ["missing"]})
    assert response.status_code == 400
    assert "unknown attachment" in response.json()["detail"].lower()
