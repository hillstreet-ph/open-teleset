"""The copilot drafts only; it can never create accounts or bypass consent."""

import pytest
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


def test_router_exposes_only_drafting():
    routes = [(route.path, sorted(route.methods)) for route in copilot.router.routes]
    assert routes == [("/api/copilot/draft", ["POST"])]


def test_draft_endpoint_returns_presets_and_a_draft():
    app = FastAPI()
    app.include_router(copilot.router)
    client = TestClient(app)

    response = client.post(
        "/api/copilot/draft",
        json={"instruction": "Create a template named Ping that says: Hello there."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["draft"]["payload"]["name"] == "Ping"
    assert body["presets"]


def test_draft_endpoint_rejects_oversized_instructions():
    app = FastAPI()
    app.include_router(copilot.router)
    client = TestClient(app)

    response = client.post("/api/copilot/draft", json={"instruction": "x" * 2001})

    assert response.status_code == 422
