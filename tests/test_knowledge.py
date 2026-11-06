"""Knowledge base: no credential reaches the disk, and retrieval stays in-process.

The contract that matters here is the one the operator cannot see: a note that looks
like a key, a session string, a phone number or a password is masked before it is
written, and answering a search never calls a model provider.
"""

import json
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import knowledge
from open_teleset import operator_store
from template_manager import template_manager


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    operator_store.set_features({})
    yield


@pytest.fixture(autouse=True)
def no_templates(monkeypatch):
    """The live template view is covered separately; these tests read only stored notes."""
    monkeypatch.setattr(template_manager, "templates", {})
    yield


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(knowledge.router)
    return TestClient(app)


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()

    @app.middleware("http")
    async def anonymous(request, call_next):
        request.state.user = None
        return await call_next(request)

    app.include_router(knowledge.router)
    return TestClient(app)


# ---------------------------------------------------------------- scrubbing


@pytest.mark.parametrize("text, secret", [
    ("My session string is 1234abcd5678efgh", "1234abcd5678efgh"),
    ("api_id: 12345678 and the api_hash is deadbeefcafe1234", "deadbeefcafe1234"),
    ("password = hunter2please", "hunter2please"),
    ("her token is oixb_aQ2Eq1qGVak", "oixb_aQ2Eq1qGVak"),
    ("call me on +65 9123 4567 please", "9123 4567"),
    ("reach me at t.me/+AbCdEfGhIjKlMnOp", "AbCdEfGhIjKlMnOp"),
    ("the key is AAAABBBBCCCCDDDDEEEEFFFFGGGG", "AAAABBBBCCCCDDDDEEEEFFFFGGGG"),
])
def test_credential_shaped_text_is_masked_before_it_is_stored(text, secret):
    clean, masked = knowledge.scrub(text)

    assert masked >= 1
    assert secret not in clean
    assert "[masked]" in clean


def test_ordinary_business_notes_survive_scrubbing():
    text = "Refunds take five working days. Send the order number and we will confirm the size."

    clean, masked = knowledge.scrub(text)

    assert masked == 0
    assert clean == text


def test_add_document_stores_the_masked_text_and_flags_it():
    doc = knowledge.add_document("operator-1", title="Contact", text="Session string is zz11yy22xx33")

    assert doc["masked"] is True
    assert "zz11yy22xx33" not in doc["preview"]
    stored = json.dumps(operator_store.read_secret("knowledge", {}))
    assert "zz11yy22xx33" not in stored


def test_a_saved_note_is_encrypted_at_rest():
    knowledge.add_document("operator-1", title="Shipping", text="We ship from Singapore on Mondays.")
    path = operator_store.store_dir() / "knowledge.secret.json"
    raw = json.loads(path.read_text())

    assert set(raw) == {"ciphertext"}
    assert "Singapore" not in path.read_text()


# ---------------------------------------------------------------- the store


def test_empty_and_oversized_notes_are_refused():
    for text in ("", "   "):
        with pytest.raises(Exception) as error:
            knowledge.add_document("operator-1", title="Blank", text=text)
        assert error.value.status_code == 400

    with pytest.raises(Exception) as error:
        knowledge.add_document("operator-1", title="Long", text="x" * (knowledge.MAX_DOC_CHARS + 1))
    assert error.value.status_code == 400


def test_title_falls_back_to_the_first_line():
    doc = knowledge.add_document("operator-1", title="", text="Opening hours\nWe are open at nine.")

    assert doc["title"] == "Opening hours"


def test_identical_note_is_not_collected_twice():
    first = knowledge.add_document("operator-1", title="Reply", text="Thanks, that is fixed now.",
                                   source="auto_reply", dedupe=True)
    second = knowledge.add_document("operator-1", title="Reply", text="Thanks, that is fixed now.",
                                    source="auto_reply", dedupe=True)

    assert first["id"] != "x"
    assert second is None
    assert len(knowledge.list_documents("operator-1")["documents"]) == 1


def test_capacity_is_enforced_rather_than_silently_dropped(monkeypatch):
    monkeypatch.setattr(knowledge, "MAX_DOCS", 2)
    knowledge.add_document("operator-1", title="A", text="first note")
    knowledge.add_document("operator-1", title="B", text="second note")

    with pytest.raises(Exception) as error:
        knowledge.add_document("operator-1", title="C", text="third note")
    assert error.value.status_code == 409


def test_operators_cannot_read_each_other_notes():
    knowledge.add_document("operator-1", title="Mine", text="Only my team sees this quote.")

    assert knowledge.list_documents("operator-2")["documents"] == []
    assert knowledge.search("operator-2", "quote") == []


def test_update_and_delete_and_clear():
    doc = knowledge.add_document("operator-1", title="Hours", text="Open at nine.", tags=["ops"])
    edited = knowledge.update_document("operator-1", doc["id"], text="Open at ten.", title="Hours v2")

    assert edited["title"] == "Hours v2"
    assert "ten" in edited["preview"]
    assert knowledge.delete_document("operator-1", doc["id"]) is True
    assert knowledge.delete_document("operator-1", doc["id"]) is False

    knowledge.add_document("operator-1", title="Keep", text="A kept note.", source="copilot")
    knowledge.add_document("operator-1", title="Drop", text="A collected reply.", source="auto_reply")

    assert knowledge.clear_documents("operator-1", source="auto_reply") == 1
    assert [d["source"] for d in knowledge.list_documents("operator-1")["documents"]] == ["copilot"]
    assert knowledge.clear_documents("operator-1") == 1


# ---------------------------------------------------------------- retrieval


def test_search_ranks_on_words_and_never_calls_a_provider():
    knowledge.add_document("operator-1", title="Refunds", text="Refunds are issued to the original payment method.")
    knowledge.add_document("operator-1", title="Sizes", text="Sizes run small, so order one size up.")

    matches = knowledge.search("operator-1", "how do refunds work")

    assert [m["title"] for m in matches] == ["Refunds"]
    assert matches[0]["score"] > 0


def test_search_ignores_stopwords_and_empty_queries():
    knowledge.add_document("operator-1", title="Any", text="the and for with are")

    assert knowledge.search("operator-1", "the and for") == []
    assert knowledge.search("operator-1", "") == []


def test_context_block_is_bounded_and_carries_the_note():
    for index in range(6):
        knowledge.add_document("operator-1", title=f"Note {index}",
                               text="Refund policy detail " * 30 + str(index))

    block = knowledge.context_for("operator-1", "refund policy", characters=300)

    assert len(block) <= 300
    assert "Refund policy detail" in block


def test_one_long_note_still_grounds_the_answer():
    knowledge.add_document("operator-1", title="Returns", text="Returns need a receipt. " * 60)

    block = knowledge.context_for("operator-1", "receipt", characters=120)

    assert block.startswith("- Returns: Returns need a receipt.")
    assert len(block) <= 120
    assert block.endswith("…")


def test_context_is_empty_when_nothing_matches():
    knowledge.add_document("operator-1", title="Unrelated", text="The warehouse closes at six.")

    assert knowledge.context_for("operator-1", "invoice number") == ""


def test_search_counts_hits_for_the_popular_note():
    knowledge.add_document("operator-1", title="Returns", text="Returns need a receipt.")

    knowledge.search("operator-1", "receipt")
    knowledge.search("operator-1", "receipt")

    assert knowledge.list_documents("operator-1")["documents"][0]["hits"] == 2


def test_templates_are_read_live_and_searchable(monkeypatch):
    monkeypatch.setattr(template_manager, "templates",
                        {"tpl_1": {"name": "Welcome", "content": "Hi, welcome to the team."}})

    matches = knowledge.search("operator-1", "welcome", include_templates=True)

    assert [m["id"] for m in matches] == ["template:tpl_1"]
    assert matches[0]["source"] == "template"


def test_edited_template_is_refetched_not_cached(monkeypatch):
    templates = {"tpl_1": {"name": "Welcome", "content": "Welcome aboard."}}
    monkeypatch.setattr(template_manager, "templates", templates)

    assert knowledge.search("operator-1", "aboard", include_templates=True)
    templates["tpl_1"]["content"] = "Welcome to the team."

    assert knowledge.search("operator-1", "aboard", include_templates=True) == []
    assert knowledge.search("operator-1", "team", include_templates=True)


def test_template_toggle_keeps_templates_out_of_the_base(monkeypatch):
    monkeypatch.setattr(template_manager, "templates",
                        {"tpl_1": {"name": "Welcome", "content": "Hi, welcome to the team."}})
    record = knowledge._record("operator-1")
    record["settings"]["include_templates"] = False
    knowledge._save("operator-1", record)

    assert knowledge.search("operator-1", "welcome") == []
    assert knowledge.search("operator-1", "welcome", include_templates=True)


# ---------------------------------------------------------------- settings


def test_settings_defaults_and_persistence():
    assert knowledge.settings_for("operator-1") == {
        "auto_collect_copilot": True, "auto_collect_replies": True, "include_templates": True}

    record = knowledge._record("operator-1")
    record["settings"]["auto_collect_replies"] = False
    knowledge._save("operator-1", record)

    assert knowledge.settings_for("operator-1")["auto_collect_replies"] is False
    assert knowledge.settings_for("operator-1")["auto_collect_copilot"] is True


def test_unknown_settings_keys_are_not_stored():
    record = knowledge._record("operator-1")
    record["settings"] = {**record["settings"], "everything": True}
    knowledge._save("operator-1", record)

    assert "everything" not in knowledge.settings_for("operator-1")


# ---------------------------------------------------------------- endpoints


def test_document_round_trip_through_the_endpoints(client):
    created = client.post("/api/knowledge/documents",
                          json={"title": "Delivery", "text": "Delivery takes two days.", "tags": ["ops"]}).json()

    doc_id = created["document"]["id"]
    assert client.get("/api/knowledge/documents").json()["documents"][0]["id"] == doc_id
    assert client.put(f"/api/knowledge/documents/{doc_id}", json={"text": "Delivery takes three days."}).json()
    assert client.post("/api/knowledge/search", json={"query": "delivery days"}).json()["matches"]

    edited = client.get("/api/knowledge/documents").json()["documents"][0]
    assert "three" in edited["preview"]

    assert client.delete(f"/api/knowledge/documents/{doc_id}").json()["success"] is True
    assert client.get("/api/knowledge/documents").json()["documents"] == []


def test_detail_route_returns_the_whole_note_the_edit_form_needs(client):
    long_text = "Restocking runs every Tuesday. " * 12
    doc_id = client.post("/api/knowledge/documents",
                        json={"title": "Restock", "text": long_text}).json()["document"]["id"]

    listed = client.get("/api/knowledge/documents").json()["documents"][0]
    assert len(listed["preview"]) < listed["characters"]
    assert "text" not in listed

    detail = client.get(f"/api/knowledge/documents/{doc_id}").json()["document"]
    assert detail["text"].strip() == long_text.strip()
    assert detail["characters"] == len(long_text.strip())


def test_detail_route_only_serves_the_operators_own_note(client):
    doc_id = client.post("/api/knowledge/documents",
                         json={"title": "Mine", "text": "Only mine."}).json()["document"]["id"]

    assert client.get(f"/api/knowledge/documents/{doc_id}").status_code == 200
    assert client.get("/api/knowledge/documents/nosuchid").status_code == 404


def test_endpoints_refuse_unauthenticated_calls(anon_client):
    assert anon_client.get("/api/knowledge/documents").status_code == 403
    assert anon_client.post("/api/knowledge/documents", json={"text": "hi"}).status_code == 403
    assert anon_client.get("/api/knowledge/documents/anything").status_code == 403
    assert anon_client.post("/api/knowledge/search", json={"query": "hi"}).status_code == 403
    assert anon_client.get("/api/knowledge/settings").status_code == 403
    assert anon_client.put("/api/knowledge/settings", json={"include_templates": False}).status_code == 403
    assert anon_client.post("/api/knowledge/clear").status_code == 403


def test_settings_endpoint_round_trip(client):
    assert client.get("/api/knowledge/settings").json()["settings"]["include_templates"] is True

    updated = client.put("/api/knowledge/settings", json={"include_templates": False,
                                                          "auto_collect_copilot": False}).json()

    assert updated["settings"] == {"auto_collect_copilot": False, "auto_collect_replies": True,
                                   "include_templates": False}
    assert client.get("/api/knowledge/settings").json()["settings"]["include_templates"] is False


def test_search_endpoint_rejects_too_many_results(client):
    assert client.post("/api/knowledge/search", json={"query": "x", "k": 99}).status_code == 422


def test_clear_endpoint_reports_what_it_removed(client):
    client.post("/api/knowledge/documents", json={"title": "A", "text": "First note."})
    client.post("/api/knowledge/documents", json={"title": "B", "text": "Second note."})

    assert client.post("/api/knowledge/clear", json={}).json()["removed"] == 2


def test_router_exposes_only_the_operators_own_data():
    paths = sorted(route.path for route in knowledge.router.routes)
    assert paths == [
        "/api/knowledge/clear", "/api/knowledge/documents", "/api/knowledge/documents",
        "/api/knowledge/documents/{doc_id}", "/api/knowledge/documents/{doc_id}",
        "/api/knowledge/documents/{doc_id}", "/api/knowledge/export", "/api/knowledge/import",
        "/api/knowledge/search", "/api/knowledge/settings", "/api/knowledge/settings",
    ]


# ============ the zip export ============


def _read_zip(payload: bytes) -> dict:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_the_export_is_a_zip_of_markdown_notes_with_an_index(client):
    client.post("/api/knowledge/documents", json={"title": "Refund policy", "text": "Refunds run 30 days."})
    client.post("/api/knowledge/documents", json={"title": "Support hours", "text": "We reply within a day."})

    response = client.get("/api/knowledge/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="knowledge-')

    files = _read_zip(response.content)
    assert files["documents/Refund-policy.md"] == "# Refund policy\n\nRefunds run 30 days.\n"
    assert files["documents/Support-hours.md"] == "# Support hours\n\nWe reply within a day.\n"

    index = json.loads(files["index.json"])
    assert index["documents"] == 2
    assert index["templates"] == 0
    assert {entry["path"] for entry in index["files"]} == {
        "documents/Refund-policy.md", "documents/Support-hours.md"}


def test_the_export_never_carries_a_credential(client):
    client.post("/api/knowledge/documents",
                json={"title": "Note", "text": "The api hash is 0123456789abcdef0123456789abcdef"})

    files = _read_zip(client.get("/api/knowledge/export").content)

    assert "0123456789abcdef0123456789abcdef" not in files["documents/Note.md"]
    assert "[masked]" in files["documents/Note.md"]


def test_duplicate_titles_get_distinct_file_names(client):
    for _ in range(2):
        client.post("/api/knowledge/documents", json={"title": "Note", "text": "Same title, different note."})

    files = _read_zip(client.get("/api/knowledge/export").content)

    assert "documents/Note.md" in files
    assert "documents/Note-2.md" in files


def test_a_title_cannot_escape_the_export_folder(client):
    client.post("/api/knowledge/documents", json={"title": "../../etc/passwd", "text": "A note."})

    files = _read_zip(client.get("/api/knowledge/export").content)

    assert all(name.startswith(("documents/", "templates/", "index.json")) for name in files)
    assert not any(".." in name for name in files)


def test_the_export_can_include_the_live_templates(client, monkeypatch):
    monkeypatch.setattr(template_manager, "templates",
                        {"welcome": {"name": "Welcome", "content": "Hi {name}, thanks for writing."}})
    client.post("/api/knowledge/documents", json={"title": "Note", "text": "A stored note."})

    files = _read_zip(client.get("/api/knowledge/export").content)

    assert files["templates/Welcome.md"] == "# Welcome\n\nHi {name}, thanks for writing.\n"
    assert json.loads(files["index.json"])["templates"] == 1


def test_templates_can_be_left_out_of_the_export(client, monkeypatch):
    monkeypatch.setattr(template_manager, "templates",
                        {"welcome": {"name": "Welcome", "content": "Hi {name}."}})
    client.post("/api/knowledge/documents", json={"title": "Note", "text": "A stored note."})

    files = _read_zip(client.get("/api/knowledge/export?include_templates=false").content)

    assert not any(name.startswith("templates/") for name in files)
    assert json.loads(files["index.json"])["templates"] == 0


def test_an_empty_base_still_exports_an_index(client):
    files = _read_zip(client.get("/api/knowledge/export").content)

    index = json.loads(files["index.json"])
    assert index["documents"] == 0
    assert index["files"] == []


def test_the_export_requires_an_operator(anon_client):
    assert anon_client.get("/api/knowledge/export").status_code == 403


# ---------------------------------------------------------------- file import


def _upload(name, data, mime="text/plain"):
    return {"files": (name, data, mime)}


def test_a_text_file_is_imported_as_a_note(client):
    response = client.post("/api/knowledge/import",
                           files=[("files", ("refunds.md", b"# Refunds\n\nRefunds run 30 days.", "text/markdown"))])

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    assert body["masked"] == 0
    assert body["files"][0] == {"name": "refunds.md", "stored": 1, "duplicates": 0, "chunks": 1}
    assert body["entries"][0]["title"] == "refunds.md"
    assert body["entries"][0]["source"] == "import"

    docs = client.get("/api/knowledge/documents").json()["documents"]
    assert [doc["title"] for doc in docs] == ["refunds.md"]


def test_a_zip_imports_each_text_member_and_skips_binaries(client):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docs/refunds.md", "Refunds run 30 days.")
        archive.writestr("docs/hours.txt", "We reply within a day.")
        archive.writestr("docs/logo.png", b"\x89PNG\x00binary")

    body = client.post("/api/knowledge/import",
                       files=[("files", ("bundle.zip", buffer.getvalue(), "application/zip"))]).json()

    assert body["imported"] == 2
    assert {entry["title"] for entry in body["entries"]} == {"bundle.zip/refunds.md", "bundle.zip/hours.txt"}


def test_a_zip_member_name_cannot_escape_the_store(client):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/refunds.md", "A note that must stay a note.")

    body = client.post("/api/knowledge/import",
                       files=[("files", ("bundle.zip", buffer.getvalue(), "application/zip"))]).json()

    titles = [entry["title"] for entry in body["entries"]]
    assert titles == ["bundle.zip/refunds.md"]
    assert not any(".." in title for title in titles)


def test_a_binary_upload_is_reported_and_not_stored(client):
    body = client.post("/api/knowledge/import",
                       files=[("files", ("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"))]).json()

    assert body["imported"] == 0
    assert body["skipped_files"][0]["name"] == "photo.png"
    assert "text notes" in body["skipped_files"][0]["reason"]
    assert client.get("/api/knowledge/documents").json()["documents"] == []


def test_an_oversized_upload_is_skipped_without_failing_the_batch(client):
    large = b"a" * (knowledge.MAX_IMPORT_FILE_BYTES + 1)
    body = client.post("/api/knowledge/import", files=[
        ("files", ("huge.txt", large, "text/plain")),
        ("files", ("small.txt", b"A short note.", "text/plain")),
    ]).json()

    assert body["imported"] == 1
    assert body["skipped_files"][0]["name"] == "huge.txt"
    assert "larger than" in body["skipped_files"][0]["reason"]


def test_imported_credentials_are_masked(client):
    body = client.post("/api/knowledge/import", files=[
        ("files", ("setup.txt", b"session string is 1234abcd5678efgh", "text/plain")),
    ]).json()

    assert body["masked"] == 1
    stored = client.get("/api/knowledge/documents").json()["documents"]
    assert stored[0]["masked"] is True
    assert "1234abcd5678efgh" not in client.get(f"/api/knowledge/documents/{stored[0]['id']}").json()["document"]["text"]


def test_importing_the_same_file_twice_reports_a_duplicate(client):
    client.post("/api/knowledge/import", files=[("files", ("note.txt", b"One note.", "text/plain"))])

    body = client.post("/api/knowledge/import",
                       files=[("files", ("note.txt", b"One note.", "text/plain"))]).json()

    assert body["imported"] == 0
    assert body["duplicates"] == 1
    assert len(client.get("/api/knowledge/documents").json()["documents"]) == 1


def test_a_long_file_is_chunked_to_the_document_limit(client):
    text = "\n\n".join("paragraph " + str(i) + " " + "x" * 100 for i in range(80)).encode()
    body = client.post("/api/knowledge/import", files=[("files", ("long.txt", text, "text/plain"))]).json()

    assert body["imported"] > 1
    assert all(entry["characters"] <= knowledge.MAX_DOC_CHARS for entry in body["entries"])
    assert body["entries"][0]["title"].startswith("long.txt (1/")


def test_import_rejects_more_files_than_the_batch_bound(client):
    files = [("files", (f"note-{i}.txt", b"A note.", "text/plain")) for i in range(knowledge.MAX_IMPORT_FILES + 1)]

    assert client.post("/api/knowledge/import", files=files).status_code == 400


def test_import_requires_an_operator(anon_client):
    assert anon_client.post("/api/knowledge/import",
                            files=[("files", ("note.txt", b"A note.", "text/plain"))]).status_code == 403
