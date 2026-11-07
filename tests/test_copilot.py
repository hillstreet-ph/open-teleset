"""The copilot drafts only; it can never create accounts or bypass consent."""

import json
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import copilot
import knowledge


def test_template_instruction_extracts_name_and_content():
    draft = copilot.build_draft(
        "Create a template named Welcome that says: Hi {name}, thanks for reaching out."
    )
    assert draft["action"] == "template"
    assert draft["payload"]["name"] == "Welcome"
    assert draft["payload"]["content"] == "Hi {name}, thanks for reaching out."
    assert draft["payload"]["category"] == "Copilot"
    assert draft["warnings"] == []


@pytest.mark.parametrize("preset", copilot.PRESETS, ids=lambda item: item["id"])
def test_every_preset_drafts_a_supported_action(preset):
    draft = copilot._preset_draft(preset)
    assert draft["action"] in {"template", "schedule", "task", "ops", "run"}
    assert draft["requires_confirmation"] is (draft["action"] != "run")


def test_schedule_defaults_and_keeps_the_consent_gates():
    draft = copilot.build_draft(
        "Schedule a daily message at 10:00 saying: Hi {name}, here is today's update."
    )
    payload = draft["payload"]
    assert draft["action"] == "schedule"
    assert payload["repeat"] == "daily"
    assert (payload["hour"], payload["minute"]) == (10, 0)
    assert payload["message"] == "Hi {name}, here is today's update."
    assert payload["approvalId"] == ""
    assert payload["recipients"] == []
    assert any("approval" in warning.lower() for warning in draft["warnings"])
    assert any("recipient" in warning.lower() for warning in draft["warnings"])


def test_schedule_reads_approval_and_recipients_without_touching_the_message():
    draft = copilot.build_draft(
        "Schedule a weekly message at 09:30 approval OPS-42 recipients: @alice_1, @bob_2 "
        "saying: Hi {name}, here is this week's summary."
    )
    payload = draft["payload"]
    assert payload["repeat"] == "weekly"
    assert (payload["hour"], payload["minute"]) == (9, 30)
    assert payload["approvalId"] == "OPS-42"
    assert payload["recipients"] == ["alice_1", "bob_2"]
    assert payload["message"].startswith("Hi {name}")
    # The schedule form enforces the minimum spacing; the draft mirrors it.
    assert payload["interval"] >= 3000


def test_schedule_reads_approval_id_and_reference_prefixes():
    for label in ("approval id: OPS-42", "approval reference: OPS-42"):
        draft = copilot.build_draft(
            f"Schedule a daily message at 10:00 {label} recipients: @alice_1 saying: Hi."
        )
        assert draft["payload"]["approvalId"] == "OPS-42", label


def test_schedule_approval_survives_whitespace_without_backtracking():
    draft = copilot.build_draft(
        "Schedule a daily message at 10:00 approval" + (" " * 500) +
        "OPS-42 saying: Hi {name}, short update."
    )
    payload = draft["payload"]
    assert payload["approvalId"] == "OPS-42"
    assert payload["message"].startswith("Hi {name}")


def test_schedule_caps_recipients_at_the_form_limit():
    recipients = " ".join(f"@user_{index:02d}1" for index in range(25))
    draft = copilot.build_draft(f"Schedule a daily message at 10:00 recipients: {recipients}")
    assert len(draft["payload"]["recipients"]) == 20
    assert any("20 recipients" in warning for warning in draft["warnings"])


def test_task_prefills_bulksend_without_consent():
    draft = copilot.build_draft(
        "Create a task to send: Hi {name}, I will follow up shortly. recipients: @alice_1"
    )
    payload = draft["payload"]
    assert draft["action"] == "task"
    assert payload["message"] == "Hi {name}, I will follow up shortly."
    assert payload["targets"] == ["alice_1"]
    assert payload["consentConfirmed"] is False
    assert any("consent" in warning.lower() for warning in draft["warnings"])


@pytest.mark.parametrize(
    "instruction",
    [
        "Buy the cheapest Telegram number and automatically register accounts",
        "Auto join casino groups and add members to my channel",
        "Create a task to send a message to scraped users",
        "Add members to this group for me",
        "Generate random names and usernames for new accounts",
    ],
)
def test_account_factory_and_cold_outreach_requests_are_refused(instruction):
    draft = copilot.build_draft(instruction)
    assert draft["action"] == "unsupported"
    assert draft["payload"] == {}
    assert "cannot" in draft["summary"].lower()


def test_empty_instruction_returns_help_with_examples():
    draft = copilot.build_draft("   ")
    assert draft["action"] == "help"
    assert draft["examples"]
    assert draft["requires_confirmation"] is False


def test_router_exposes_drafting_history_and_the_preset_runbook():
    routes = sorted((route.path, tuple(sorted(route.methods))) for route in copilot.router.routes)
    assert routes == [
        ("/api/copilot/attachments", ("DELETE",)),
        ("/api/copilot/attachments", ("GET",)),
        ("/api/copilot/attachments", ("POST",)),
        ("/api/copilot/attachments/{attachment_id}", ("DELETE",)),
        ("/api/copilot/draft", ("POST",)),
        ("/api/copilot/enhance", ("POST",)),
        ("/api/copilot/history", ("DELETE",)),
        ("/api/copilot/history", ("GET",)),
        ("/api/copilot/history/{turn_id}/outcome", ("POST",)),
        ("/api/copilot/preset-draft", ("POST",)),
        ("/api/copilot/presets", ("GET",)),
        ("/api/copilot/presets", ("POST",)),
        ("/api/copilot/presets/reset", ("POST",)),
        ("/api/copilot/presets/{preset_id}", ("DELETE",)),
        ("/api/copilot/presets/{preset_id}", ("PUT",)),
        ("/api/copilot/suggest", ("GET",)),
        ("/api/copilot/tools", ("GET",)),
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A dashboard-less app whose request state carries the signed-in operator."""
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(copilot.router)
    return TestClient(app)


def test_draft_endpoint_returns_presets_and_a_draft(client):
    response = client.post(
        "/api/copilot/draft",
        json={"instruction": "Create a template named Ping that says: Hello there."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["draft"]["payload"]["name"] == "Ping"
    assert body["presets"]
    assert body["turn_id"]


def test_draft_endpoint_rejects_oversized_instructions(client):
    response = client.post("/api/copilot/draft", json={"instruction": "x" * 2001})

    assert response.status_code == 422


def test_draft_never_sends_or_creates_anything(client):
    client.post("/api/copilot/draft",
                json={"instruction": "Create a template named Ping that says: Hello there."})
    before = copilot.build_draft("Create a template named Ping that says: Hello there.")

    assert before["requires_confirmation"] is True
    assert before["action"] == "template"
    assert client.get("/api/copilot/history").json()["turns"][0]["outcome"] == "drafted"


def test_history_keeps_the_thread_in_newest_first_order(client):
    for instruction in (
        "Create a template named First that says: Hello.",
        "Create a template named Second that says: Goodbye.",
    ):
        client.post("/api/copilot/draft", json={"instruction": instruction})

    turns = client.get("/api/copilot/history").json()["turns"]
    assert [turn["text"] for turn in turns][:2] == ["Goodbye.", "Hello."]
    assert all(turn["instruction"] for turn in turns)


def test_history_is_scoped_to_the_signed_in_operator(client, tmp_path, monkeypatch):
    turns = client.get("/api/copilot/history").json()["turns"]
    assert turns == []

    client.post("/api/copilot/draft", json={"instruction": "Create a template named Mine that says: Hi there."})
    path = tmp_path / "copilot_history.secret.json"
    raw = json.loads(path.read_text())
    assert set(raw) == {"ciphertext"}
    assert "Hi there." not in path.read_text()

    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path / "other"))
    assert copilot._turns("operator-2") == []


def test_applying_a_draft_offers_it_to_the_knowledge_base(client, monkeypatch):
    saved = []
    monkeypatch.setattr(
        copilot.knowledge, "add_document",
        lambda operator, **kwargs: saved.append((operator, kwargs)) or {"id": "doc-1"},
    )

    turn = client.post("/api/copilot/draft",
                       json={"instruction": "Create a template named Ping that says: Hello there."}).json()["turn_id"]
    response = client.post(f"/api/copilot/history/{turn}/outcome", json={"outcome": "applied"})

    assert response.json() == {"success": True, "collected": True}
    assert saved[0][0] == "operator-1"
    assert saved[0][1]["text"] == "Hello there."
    assert saved[0][1]["source"] == "copilot"


def test_auto_collect_can_be_switched_off(client, monkeypatch):
    calls = []
    monkeypatch.setattr(copilot.knowledge, "add_document", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(copilot.knowledge, "settings_for", lambda operator: {"auto_collect_copilot": False})

    turn = client.post("/api/copilot/draft",
                       json={"instruction": "Create a template named Ping that says: Hi."}).json()["turn_id"]
    response = client.post(f"/api/copilot/history/{turn}/outcome", json={"outcome": "applied"})

    assert response.json()["collected"] is True
    assert calls == []


def test_discarding_a_draft_collects_nothing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(copilot.knowledge, "add_document",
                        lambda *a, **k: calls.append(a))

    turn = client.post("/api/copilot/draft",
                       json={"instruction": "Create a template named Ping that says: Hello there."}).json()["turn_id"]
    client.post(f"/api/copilot/history/{turn}/outcome", json={"outcome": "discarded"})

    assert calls == []
    assert copilot._turns("operator-1")[0]["outcome"] == "discarded"


def test_only_known_outcomes_are_accepted(client):
    turn = client.post("/api/copilot/draft",
                       json={"instruction": "Create a template named Ping that says: Hi."}).json()["turn_id"]

    assert client.post(f"/api/copilot/history/{turn}/outcome", json={"outcome": "sent"}).status_code == 400
    assert client.post("/api/copilot/history/nope/outcome", json={"outcome": "applied"}).status_code == 404


def test_history_can_be_cleared(client):
    client.post("/api/copilot/draft", json={"instruction": "Create a template named Ping that says: Hi."})

    assert client.delete("/api/copilot/history").json()["cleared"] is True
    assert client.get("/api/copilot/history").json()["turns"] == []


def test_turns_are_bounded(client, monkeypatch):
    monkeypatch.setattr(copilot, "MAX_TURNS", 3)
    for index in range(6):
        client.post("/api/copilot/draft",
                    json={"instruction": f"Create a template named T{index} that says: Hello."})

    assert len(copilot._turns("operator-1")) == 3


def test_draft_still_works_when_history_cannot_be_written(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(copilot, "_write_turns", refuse)

    response = client.post("/api/copilot/draft",
                           json={"instruction": "Create a template named Ping that says: Hi."})

    assert response.status_code == 200
    assert response.json()["turn_id"] == ""


def test_anonymous_requests_are_refused():
    app = FastAPI()

    @app.middleware("http")
    async def anonymous(request, call_next):
        request.state.user = None
        return await call_next(request)

    app.include_router(copilot.router)
    client = TestClient(app)

    assert client.post("/api/copilot/draft", json={"instruction": "hi"}).status_code == 403
    assert client.get("/api/copilot/history").status_code == 403


# ============ the ops tier ============


def test_free_text_opens_an_ops_surface_without_running_anything():
    draft = copilot.build_draft("Collect the members of a group I already have access to.")

    assert draft["action"] == "ops"
    payload = draft["payload"]
    assert payload["surface"] == "scraper"
    assert payload["tab"] == "scraper"
    assert payload["gate"] == "gate-scraper"
    assert payload["fields"]["limit"] == 500
    assert draft["requires_confirmation"] is True


def test_the_ops_draft_carries_the_surface_note_as_a_warning():
    draft = copilot.build_draft("Show the automation status.")

    assert draft["action"] == "ops"
    assert draft["payload"]["surface"] == "automation"
    assert draft["warnings"]


def test_the_copilot_tunes_a_field_only_inside_its_bounds():
    draft = copilot.build_draft("Collect the members with a limit of 900.")
    assert draft["payload"]["fields"]["limit"] == 900

    capped = copilot.build_draft("Collect the members with a limit of 99999.")
    assert capped["payload"]["fields"]["limit"] == 2000


def test_the_member_adder_delay_never_drops_below_the_safety_floor():
    draft = copilot._preset_draft(
        {"kind": "ops", "surface": "adder", "title": "Add members who agreed",
         "instruction": "Add the members who agreed with delay 5."}
    )

    assert draft["action"] == "ops"
    assert draft["payload"]["fields"]["delay"] == 35


def test_the_bulksend_preset_keeps_the_consent_gate_closed():
    draft = copilot._preset_draft(
        {"kind": "ops", "surface": "bulksend", "title": "Prepare an opt-in send",
         "instruction": "Send an opt-in message to the recipients who agreed."}
    )

    assert draft["action"] == "ops"
    assert draft["payload"]["fields"] == {"delay": 45, "perAccountLimit": 8}


def test_the_adder_and_auto_join_surfaces_stay_unreachable_from_free_text():
    # Their prose forms stay refused; only the curated preset opens them.
    assert copilot.build_draft("Add the members who agreed to my group.")["action"] == "unsupported"
    assert copilot.build_draft("Join the groups I list, one at a time.")["action"] == "unsupported"

    adder = copilot._preset_draft(
        {"kind": "ops", "surface": "adder", "title": "Add members",
         "instruction": "Add the members who agreed."}
    )
    autojoin = copilot._preset_draft(
        {"kind": "ops", "surface": "autojoin", "title": "Join groups",
         "instruction": "Join the groups I list."}
    )
    assert adder["payload"]["surface"] == "adder"
    assert autojoin["payload"]["surface"] == "autojoin"


def test_the_account_factory_surface_only_opens_the_operators_own_form():
    assert copilot.build_draft("Register Telegram accounts automatically.")["action"] == "unsupported"

    draft = copilot.build_draft("Run the account factory.")
    assert draft["action"] == "ops"
    assert draft["payload"]["surface"] == "factory"
    assert draft["payload"]["fields"]["maxPrice"] == 0.5
    assert draft["requires_confirmation"] is True


def test_the_ops_preset_route_prefills_and_stops_at_the_gate(client):
    response = client.post("/api/copilot/preset-draft", json={"preset_id": "scraper-members"})

    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["action"] == "ops"
    assert draft["payload"]["gate"] == "gate-scraper"
    assert draft["payload"]["fields"]["limit"] == 500


def test_an_unknown_preset_id_is_refused(client):
    assert client.post("/api/copilot/preset-draft", json={"preset_id": "nope"}).status_code == 404


# ============ the internal tier ============


def test_a_note_is_stored_in_the_operator_own_knowledge_base(client):
    response = client.post(
        "/api/copilot/draft",
        json={"instruction": "Remember: orders leave the warehouse within two working days."},
    )

    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "knowledge-note"
    assert draft["result"]["stored"] is True

    import knowledge
    stored = knowledge.list_documents("operator-1")
    assert any("warehouse" in doc["preview"] for doc in stored["documents"])


def test_a_knowledge_search_ranks_the_operators_own_notes(client):
    client.post("/api/copilot/draft", json={"instruction": "Remember: the refund policy is 30 days."})

    response = client.post("/api/copilot/draft", json={"instruction": "Search my knowledge base for the refund policy."})

    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "knowledge-search"
    assert draft["result"]["count"] >= 1


def test_a_knowledge_search_with_no_match_returns_an_empty_result(client):
    response = client.post("/api/copilot/draft", json={"instruction": "Search my knowledge base for the refund policy."})

    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "knowledge-search"
    assert draft["result"]["count"] == 0


def test_an_internal_command_without_its_needed_text_says_so():
    draft = copilot._internal_draft("knowledge-note", {"text": ""})

    assert draft["warnings"]
    assert "note" in draft["warnings"][0].lower()


def test_the_auto_reply_preview_never_sends(client, monkeypatch):
    import auto_reply

    auto_reply._save("operator-1", {
        **auto_reply._blank(),
        "rules": [{"id": "rule-1", "name": "Pricing", "match": "keyword", "keywords": ["cost"],
                   "reply": "Our price is 10 USD", "enabled": True}],
    })

    response = client.post("/api/copilot/draft",
                           json={"instruction": "Which rule would answer the message: how much does it cost?"})

    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "autoreply-test"
    assert draft["result"]["matched"]["id"] == "rule-1"
    assert draft["result"]["would_send"] is True


def test_the_patterns_command_re_reads_the_notes(client):
    response = client.post("/api/copilot/draft", json={"instruction": "Look for repeated wording in my notes."})

    draft = response.json()["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "patterns-refresh"
    assert "suggestions" in draft["result"]


# ============ the per-operator runbook ============


def test_the_runbook_lists_the_shipped_presets_with_use_counts(client):
    response = client.get("/api/copilot/presets")

    assert response.status_code == 200
    presets = response.json()["presets"]
    assert presets
    assert all(item["built_in"] is True for item in presets)
    assert all(item["uses"] == 0 for item in presets)
    assert {item["id"] for item in presets} >= {"scraper-members", "knowledge-note"}


def test_a_built_in_preset_can_be_edited_by_the_operator(client):
    response = client.put("/api/copilot/presets/scraper-members",
                           json={"title": "Collect my group members",
                                 "description": "Uses my usual limit."})

    assert response.status_code == 200
    presets = {item["id"]: item for item in response.json()["presets"]}
    assert presets["scraper-members"]["title"] == "Collect my group members"
    assert presets["scraper-members"]["modified"] is True
    assert presets["scraper-members"]["instruction"].startswith("Collect the members")


def test_the_operator_can_add_its_own_preset(client):
    response = client.post("/api/copilot/presets", json={
        "group": "Outreach",
        "title": "My weekly check",
        "description": "Runs my usual pace.",
        "instruction": "Collect the members of a group I already have access to.",
    })

    assert response.status_code == 200
    created = response.json()["preset"]
    assert created["id"].startswith("custom-")
    assert created["kind"] == "draft"

    presets = {item["id"]: item for item in response.json()["presets"]}
    assert created["id"] in presets
    assert presets[created["id"]]["built_in"] is False


def test_a_custom_preset_needs_a_title_and_an_instruction(client):
    response = client.post("/api/copilot/presets", json={"title": "Only a title"})

    assert response.status_code == 400


def test_a_preset_can_be_removed_and_restored(client):
    assert client.delete("/api/copilot/presets/scraper-members").status_code == 200
    ids = {item["id"] for item in client.get("/api/copilot/presets").json()["presets"]}
    assert "scraper-members" not in ids

    assert client.post("/api/copilot/presets/reset").status_code == 200
    ids = {item["id"] for item in client.get("/api/copilot/presets").json()["presets"]}
    assert "scraper-members" in ids


def test_removing_an_unknown_preset_is_refused(client):
    assert client.delete("/api/copilot/presets/nope").status_code == 404


def test_an_applied_preset_floats_to_the_top_of_the_runbook(client):
    turn = client.post("/api/copilot/preset-draft",
                       json={"preset_id": "knowledge-note"}).json()["turn_id"]
    client.post(f"/api/copilot/history/{turn}/outcome", json={"outcome": "applied"})

    presets = client.get("/api/copilot/presets").json()["presets"]
    assert presets[0]["id"] == "knowledge-note"
    assert presets[0]["uses"] == 1


def test_the_runbook_is_scoped_to_the_signed_in_operator(client, tmp_path, monkeypatch):
    client.put("/api/copilot/presets/scraper-members", json={"title": "Only mine"})

    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path / "other"))
    assert copilot._preset_record("operator-2")["overrides"] == {}



# ---------------------------------------------------------------- the assistant tray


def _upload(name, data, mime="text/plain"):
    return ("files", (name, data, mime))


def test_attachments_are_listed_read_and_removed(client):
    body = client.post("/api/copilot/attachments", files=[_upload("refunds.md", b"Refunds run 30 days.")]).json()

    assert body["success"] is True
    record = body["added"][0]
    assert record["name"] == "refunds.md"
    assert record["kind"] == "text"
    assert record["readable"] is True
    assert record["characters"] == len("Refunds run 30 days.")
    assert client.get("/api/copilot/attachments").json()["attachments"][0]["id"] == record["id"]

    assert client.delete(f"/api/copilot/attachments/{record['id']}").status_code == 200
    assert client.get("/api/copilot/attachments").json()["attachments"] == []
    assert client.delete(f"/api/copilot/attachments/{record['id']}").status_code == 404


def test_a_binary_attachment_is_kept_but_named_unreadable(client):
    record = client.post("/api/copilot/attachments",
                         files=[_upload("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png")]).json()["added"][0]

    assert record["kind"] == "image"
    assert record["readable"] is False
    assert "photo.png" in record["reason"]


def test_attachments_are_capped_per_prompt(client):
    files = [_upload(f"note-{i}.txt", b"A note.") for i in range(copilot.MAX_ATTACHMENTS + 1)]

    assert client.post("/api/copilot/attachments", files=files).status_code == 400


def test_an_oversized_attachment_is_refused(client):
    oversized = b"a" * (copilot.ATTACHMENT_FILE_BYTES + 1)

    response = client.post("/api/copilot/attachments", files=[_upload("huge.txt", oversized)])

    assert response.status_code == 400
    assert "4 MB" in response.json()["detail"]


def test_a_zip_of_text_attaches_its_readable_members(client):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes/refunds.md", "Refunds run 30 days.")
        archive.writestr("notes/logo.png", b"\x89PNG\x00")

    record = client.post("/api/copilot/attachments",
                         files=[_upload("bundle.zip", buffer.getvalue(), "application/zip")]).json()["added"][0]

    assert record["kind"] == "archive"
    assert record["readable"] is True
    assert record["characters"] == len("Refunds run 30 days.")


def test_attachments_alone_become_a_knowledge_import(client):
    record = client.post("/api/copilot/attachments",
                         files=[_upload("refunds.md", b"Refunds run 30 days.")]).json()["added"][0]

    body = client.post("/api/copilot/draft", json={"instruction": "", "attachment_ids": [record["id"]]}).json()

    draft = body["draft"]
    assert draft["action"] == "run"
    assert draft["payload"]["command"] == "knowledge-import"
    assert draft["result"]["imported"] == 1
    assert draft["next"]
    assert [doc["title"] for doc in
            knowledge.list_documents("operator-1")["documents"]] == ["refunds.md"]


def test_a_binary_only_tray_explains_what_can_be_read(client):
    record = client.post("/api/copilot/attachments",
                         files=[_upload("photo.png", b"\x89PNG\x00", "image/png")]).json()["added"][0]

    draft = client.post("/api/copilot/draft",
                        json={"instruction": "", "attachment_ids": [record["id"]]}).json()["draft"]

    assert draft["action"] == "help"
    assert "cannot be read" in draft["summary"]
    assert any("photo.png" in warning for warning in draft["warnings"])


def test_a_binary_attachment_does_not_block_a_text_draft(client):
    record = client.post("/api/copilot/attachments",
                         files=[_upload("photo.png", b"\x89PNG\x00", "image/png")]).json()["added"][0]

    draft = client.post("/api/copilot/draft", json={
        "instruction": "Create a template named Ping that says: Hello there.",
        "attachment_ids": [record["id"]],
    }).json()["draft"]

    assert draft["action"] == "template"
    assert draft["payload"]["attachments"][0]["name"] == "photo.png"
    assert any("could not be read as text" in warning for warning in draft["warnings"])


def test_the_draft_echoes_ask_or_work(client):
    ask = client.post("/api/copilot/draft", json={"instruction": "Open the logs"}).json()["draft"]
    work = client.post("/api/copilot/draft",
                       json={"instruction": "Open the logs", "mode": "work"}).json()["draft"]

    assert ask["mode"] == "ask"
    assert work["mode"] == "work"


def test_enhance_names_the_fields_it_inferred(client):
    body = client.post("/api/copilot/enhance", json={
        "instruction": "Schedule a daily message at 10:00 saying: Hello"}).json()

    assert body["success"] is True
    assert body["instruction"] == "Schedule a daily message at 10:00 saying: Hello"
    assert "set the send time to 10:00" in body["added"]


def test_enhance_asks_for_what_a_vague_prompt_lacks(client):
    body = client.post("/api/copilot/enhance", json={"instruction": "do the thing"}).json()

    assert "Give the exact message text" in body["instruction"]


def test_enhance_keeps_a_template_prompt_and_asks_for_its_body(client):
    body = client.post("/api/copilot/enhance", json={
        "instruction": "Create a template named Short."}).json()

    assert body["action"] == "template"
    assert body["instruction"].startswith("Create a template named Short.")
    assert "exact message text" in body["instruction"]
    assert any("exact message text" in item for item in body["added"])


def test_enhance_rejects_an_empty_prompt(client):
    assert client.post("/api/copilot/enhance", json={"instruction": "  "}).status_code == 400


def test_suggestions_start_with_the_operators_own_notes(client):
    knowledge.add_document("operator-1", title="Refund policy", text="Refunds run 30 days.")

    body = client.get("/api/copilot/suggest").json()

    assert body["suggestions"][0]["source"] == "knowledge"
    assert "Refund policy" in body["suggestions"][0]["instruction"]
    assert len(body["suggestions"]) <= 6
    assert len({item["instruction"] for item in body["suggestions"]}) == len(body["suggestions"])


def test_suggestions_include_the_last_prompt_the_operator_applied(client):
    first = client.post("/api/copilot/draft", json={
        "instruction": "Create a template named Ping that says: Hello there."}).json()
    client.post(f"/api/copilot/history/{first['turn_id']}/outcome", json={"outcome": "applied"})

    body = client.get("/api/copilot/suggest").json()

    assert any(item["source"] == "history" for item in body["suggestions"])


def test_suggestions_are_capped(client):
    assert len(client.get("/api/copilot/suggest?limit=2").json()["suggestions"]) <= 2


def test_navigation_switches_a_page_without_running_anything():
    draft = copilot.build_draft("Open the logs")

    assert draft["action"] == "navigate"
    assert draft["payload"]["tab"] == "logs"
    assert draft["requires_confirmation"] is False


def test_ban_evasion_prompts_are_refused():
    for prompt in (
        "Add a greeting rotation to avoid flags and ban",
        "Warm up the accounts before using them",
        "Auto rotate accounts when a limit is reached",
        "Use a fingerprint mask so Telegram cannot detect the sessions",
    ):
        draft = copilot.build_draft(prompt)
        assert draft["action"] == "unsupported", prompt


def test_attachments_require_an_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()

    @app.middleware("http")
    async def anonymous(request, call_next):
        request.state.user = None
        return await call_next(request)

    app.include_router(copilot.router)
    anon = TestClient(app)

    assert anon.get("/api/copilot/attachments").status_code == 403
    assert anon.get("/api/copilot/suggest").status_code == 403
    assert anon.post("/api/copilot/enhance", json={"instruction": "Open the logs"}).status_code == 403
