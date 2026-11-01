"""The copilot drafts only; it can never create accounts or bypass consent."""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import copilot


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
    draft = copilot.build_draft(preset["instruction"])
    assert draft["action"] in {"template", "schedule", "task"}
    assert draft["requires_confirmation"] is True


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


def test_router_exposes_drafting_and_history_only():
    routes = sorted((route.path, tuple(sorted(route.methods))) for route in copilot.router.routes)
    assert routes == [
        ("/api/copilot/draft", ("POST",)),
        ("/api/copilot/history", ("DELETE",)),
        ("/api/copilot/history", ("GET",)),
        ("/api/copilot/history/{turn_id}/outcome", ("POST",)),
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
