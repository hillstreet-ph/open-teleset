"""Durable Auto Transfer: resolve a source roster, then queue one invite job.

Transfer is the same invite RPC and the same consent gate as member add; only
the recipient list comes from a group instead of the form. The job kind keeps
what the operator asked for while the outreach policy still sees member_add.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import outreach_jobs as jobs
import outreach_policy as policy
import telegram_ops
from open_teleset.crypto.cipher import SessionCipher


def participant(uid, username, bot=False):
    return SimpleNamespace(id=uid, username=username, bot=bot,
                           first_name="First", last_name="Last")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", SessionCipher.generate_key())
    monkeypatch.setenv("OUTREACH_RATE_DB", str(tmp_path / "rate.sqlite"))
    evaluator = policy.OutreachPolicy(str(tmp_path / "policy.json"))
    evaluator.path.write_text(json.dumps(
        {"version": 1, "approvals": {}, "consents": [], "suppressions": []}))
    monkeypatch.setattr(jobs, "outreach_policy", evaluator)
    monkeypatch.setattr(policy, "outreach_policy", evaluator)
    monkeypatch.setattr(telegram_ops, "outreach_policy", evaluator)
    monkeypatch.setattr(jobs, "_load_profile", AsyncMock(return_value={"role": "admin"}))
    monkeypatch.setattr(jobs.account_manager, "accounts", {"a": {"user_id": 999},
                                                           "b": {"user_id": 998}})
    storage = jobs.JobStore(tmp_path / "jobs.sqlite")
    monkeypatch.setattr(jobs, "store", storage)
    monkeypatch.setattr(jobs, "require_worker", lambda: None)
    monkeypatch.setattr(jobs.webhooks, "emit_job", lambda *args, **kwargs: None)
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=999)),
        get_entity=AsyncMock(return_value=SimpleNamespace(id=5, title="Source")),
    )
    monkeypatch.setattr(jobs.account_manager, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(jobs, "_resolve_target_entity",
                        AsyncMock(return_value=SimpleNamespace(id=5, title="Source")))
    members = [
        participant(1, "alice"),
        participant(999, "owner_account"),   # the signed-in account itself
        participant(2, "helperbot", bot=True),
        participant(3, "alice"),             # duplicate username
        participant(4, "bobby"),
    ]
    monkeypatch.setattr(jobs, "collect_scrape_members",
                        AsyncMock(return_value=(members, "members", 5)))
    return SimpleNamespace(store=storage, client=client)


def _config(**overrides):
    config = {
        "request_id": "automation_transfer_key_01",
        "source_target": "@source_group",
        "dest_target": "@dest_group",
        "limit": 10,
        "delay": 35,
        "rotation_mode": "single",
        "account_id": "a",
        "consent_confirmed": True,
    }
    config.update(overrides)
    return config


def test_transfer_requires_confirmed_consent(env):
    with pytest.raises(HTTPException) as raised:
        asyncio.run(jobs.enqueue_transfer("operator-1", _config(consent_confirmed=False)))
    assert raised.value.status_code == 400
    assert "consent" in raised.value.detail.lower()


def test_transfer_refuses_identical_source_and_destination(env):
    with pytest.raises(HTTPException) as raised:
        asyncio.run(jobs.enqueue_transfer("operator-1", _config(dest_target="@source_group")))
    assert raised.value.status_code == 400
    assert "different from the source" in raised.value.detail


def test_transfer_queues_one_job_with_the_filtered_roster(env):
    snapshot = asyncio.run(jobs.enqueue_transfer("operator-1", _config(limit=3)))

    assert snapshot["kind"] == "member_transfer"
    assert snapshot["state"] == "queued"
    assert snapshot["total"] == 2
    assert {(item["target"], item["state"]) for item in snapshot["results"]} == {
        ("alice", "pending"), ("bobby", "pending")}

    with env.store.db() as db:
        row = db.execute("SELECT payload FROM jobs").fetchone()
    config = env.store.unpack(row["payload"])
    assert config["dest_target"] == "@dest_group"
    assert "usernames" not in config
    assert config["sender_identities"] == {"a": 999}


def test_transfer_keeps_the_member_add_policy_and_scope():
    assert jobs.policy_kind("member_transfer") == "member_add"
    assert policy.cooldown_scope(jobs.policy_kind("member_transfer")) == \
        policy.cooldown_scope("member_add")


def test_transfer_route_returns_a_durable_snapshot(env, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(jobs.router)
    client = TestClient(app)
    response = client.post("/api/jobs/transfer", json={
        "request_id": "ui_transfer_key_0001",
        "source_target": "@source_group",
        "dest_target": "@dest_group",
        "limit": 10,
        "delay": 35,
        "rotation_mode": "single",
        "account_id": "a",
        "consent_confirmed": True,
    })
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == "member_transfer"
    assert body["total"] == 2
    assert client.post("/api/jobs/transfer", json={
        "request_id": "ui_transfer_key_0002",
        "source_target": "@source_group",
        "dest_target": "@dest_group",
        "account_id": "a",
        "consent_confirmed": False,
    }).status_code == 400
