"""Internal automation: scrape and auto-join validate, persist and execute.

These two actions run entirely with the operator's own account: a scrape only
reads a member list, and a join is a self-action, so neither goes through the
outreach consent gate. Their bounds are still enforced on the way in and again
at execution time, because a stored schedule can be edited on disk.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from telethon import errors

import scheduler as module
import workflows


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "SCHEDULE_FILE", str(tmp_path / "schedules.json"))
    monkeypatch.setenv("OUTREACH_RATE_DB", str(tmp_path / "rate.sqlite"))
    monkeypatch.setattr(module.log_manager, "add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    return module.TaskScheduler()


@pytest.fixture
def client(monkeypatch):
    telethon_client = AsyncMock()
    telethon_client.get_entity = AsyncMock(side_effect=lambda value: SimpleNamespace(id=7, username=str(value)))
    manager = SimpleNamespace(accounts={"acct": {}}, get_client=AsyncMock(return_value=telethon_client))
    monkeypatch.setattr(module, "account_manager", manager)
    return telethon_client


# ---- validation -----------------------------------------------------------

@pytest.mark.parametrize("limit", [1, 200, 1000])
def test_scrape_requirements_accept_bounded_limits(limit):
    assert module.schedule_requirements(
        {"action": "scrape_members", "scrape_target": "@channel", "scrape_limit": limit}
    ) == (None, [], 0)


@pytest.mark.parametrize("schedule", [
    {"action": "scrape_members", "scrape_target": "", "scrape_limit": 10},
    {"action": "scrape_members", "scrape_target": "   ", "scrape_limit": 10},
    {"action": "scrape_members", "scrape_target": "x" * 257, "scrape_limit": 10},
    {"action": "scrape_members", "scrape_target": "@channel", "scrape_limit": 0},
    {"action": "scrape_members", "scrape_target": "@channel", "scrape_limit": 1001},
    {"action": "scrape_members", "scrape_target": "@channel", "scrape_limit": "200"},
    {"action": "scrape_members", "scrape_target": "@channel", "scrape_limit": True},
])
def test_scrape_requirements_reject_out_of_bounds(schedule):
    with pytest.raises(ValueError):
        module.schedule_requirements(schedule)


def test_scrape_requirements_default_the_missing_limit():
    assert module.schedule_requirements(
        {"action": "scrape_members", "scrape_target": "@channel"}
    ) == (None, [], 0)


@pytest.mark.parametrize("urls,delay", [
    (["https://t.me/+AbCdEf123456"], 35),
    (["@publicchannel", "durov"], 60),
    (["t.me/joinchat/XyZ12345"] + [f"@chan{i}" for i in range(9)], 35),
])
def test_join_plan_accepts_up_to_ten_targets(urls, delay):
    parsed, returned_delay = module.join_plan({"join_urls": urls, "join_delay": delay})
    assert len(parsed) == len(urls)
    assert returned_delay == delay


@pytest.mark.parametrize("schedule", [
    {"join_urls": []},
    {"join_urls": ["@channel"] * 11},
    {"join_urls": ["@channel"], "join_delay": 34},
    {"join_urls": ["@channel"], "join_delay": "35"},
    {"join_urls": ["not a link!!"]},
    {"join_urls": "t.me/one"},
])
def test_join_plan_rejects_out_of_bounds(schedule):
    with pytest.raises(ValueError):
        module.join_plan(schedule)


# ---- persistence ----------------------------------------------------------

def test_add_schedule_persists_scrape_fields(store):
    assert store.add_schedule(
        schedule_id="sched_one", name="Weekly members", cron="0 9 * * 1",
        action="scrape_members", target=None, account_ids=["acct"],
        scrape_target="@channel", scrape_limit=500, filter_bots=False,
    )
    saved = json.loads(Path(module.SCHEDULE_FILE).read_text(encoding="utf-8"))
    record = saved["schedules"]["sched_one"]
    assert record["action"] == "scrape_members"
    assert record["scrape_target"] == "@channel"
    assert record["scrape_limit"] == 500
    assert record["filter_bots"] is False


def test_add_schedule_persists_join_fields(store):
    assert store.add_schedule(
        schedule_id="sched_two", name="Join partners", cron="0 9 * * 1",
        action="auto_join", target=None, account_ids=["acct"],
        join_urls=["@partnerone", "https://t.me/+InviteHash12"], join_delay=45,
    )
    saved = json.loads(Path(module.SCHEDULE_FILE).read_text(encoding="utf-8"))
    record = saved["schedules"]["sched_two"]
    assert record["join_urls"] == ["@partnerone", "https://t.me/+InviteHash12"]
    assert record["join_delay"] == 45
    assert record["policy_action"] is None


def test_add_schedule_rejects_invalid_scrape_and_join(store):
    assert not store.add_schedule(
        schedule_id="sched_bad", name="Bad", cron="0 9 * * *",
        action="scrape_members", target=None, account_ids=["acct"],
        scrape_target="", scrape_limit=10,
    )
    assert not store.add_schedule(
        schedule_id="sched_bad2", name="Bad", cron="0 9 * * *",
        action="auto_join", target=None, account_ids=["acct"],
        join_urls=["@channel"] * 11, join_delay=35,
    )
    assert store.schedules == {}


# ---- execution ------------------------------------------------------------

def join_schedule(**overrides):
    schedule = {
        "id": "sched_join", "name": "Join", "cron": "0 9 * * 1", "action": "auto_join",
        "accounts": ["acct"], "join_urls": ["https://t.me/+InviteHash12", "@publicone"],
        "join_delay": 35, "enabled": True,
    }
    schedule.update(overrides)
    return schedule


@pytest.mark.asyncio
async def test_auto_join_joins_invite_and_public(store, client):
    schedule = join_schedule()

    assert await store._execute_schedule(schedule) is True
    assert client.call_count == 2
    assert schedule["last_run"]
    assert module.asyncio.sleep.await_count == 1
    assert module.asyncio.sleep.await_args.args == (35,)


@pytest.mark.asyncio
async def test_auto_join_records_every_target_with_its_time(store, client):
    schedule = join_schedule()

    assert await store._execute_schedule(schedule) is True
    record = schedule["last_join_result"]
    assert record["account"] == "acct" and record["joined"] == 2 and record["failed"] == 0
    assert [row["target"] for row in record["targets"]] == ["https://t.me/+InviteHash12", "@publicone"]
    assert [row["state"] for row in record["targets"]] == ["joined", "joined"]
    # Epoch seconds, so the dashboard can render each row in the operator's time zone.
    assert all(isinstance(row["at"], (int, float)) and row["at"] > 0 for row in record["targets"])
    assert isinstance(record["finished_at"], (int, float))


@pytest.mark.asyncio
async def test_auto_join_stops_on_a_banned_account_and_names_it(store, client):
    client.side_effect = errors.UserDeactivatedBanError(None)
    schedule = join_schedule()

    assert await store._execute_schedule(schedule) is False
    assert client.call_count == 1
    record = schedule["last_join_result"]
    assert record["stopped_reason"] == "account_banned" and record["failed"] == 1
    assert record["targets"][0]["state"] == "failed"
    assert record["targets"][0]["reason"] == "account_banned"
    assert record["targets"][1]["state"] == "pending"
    assert record["targets"][1]["reason"] == "not_attempted"


@pytest.mark.asyncio
async def test_auto_join_counts_existing_membership_as_skipped(store, client):
    client.side_effect = errors.UserAlreadyParticipantError(None)

    assert await store._execute_schedule(join_schedule(join_urls=["https://t.me/+InviteHash12"])) is True
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_auto_join_stops_on_flood_wait(store, client):
    client.side_effect = errors.FloodWaitError(None, 26651)

    assert await store._execute_schedule(join_schedule()) is False
    assert client.call_count == 1
    module.asyncio.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_join_rotates_to_the_next_account_when_one_is_limited(store, monkeypatch):
    flooded = AsyncMock(side_effect=errors.FloodWaitError(None, 60))
    healthy = AsyncMock()
    healthy.get_entity = AsyncMock(side_effect=lambda value: SimpleNamespace(id=7, username=str(value)))
    clients = {"a": flooded, "b": healthy}
    manager = SimpleNamespace(accounts={"a": {}, "b": {}},
                              get_client=AsyncMock(side_effect=lambda account: clients[account]))
    monkeypatch.setattr(module, "account_manager", manager)
    schedule = join_schedule(accounts=["a", "b"])

    assert await store._execute_schedule(schedule) is True
    record = schedule["last_join_result"]
    assert record["joined"] == 2 and record["failed"] == 0
    assert record["stopped_reason"] == ""
    assert flooded.call_count == 1
    assert [row["account"] for row in record["targets"]] == ["b", "b"]
    # The limit stays scoped to joining, so the next schedule can use account a
    # for a send immediately.
    assert module.account_cooldown_remaining("a", "send") == 0
    assert module.account_cooldown_remaining("a", "join") > 0


@pytest.mark.asyncio
async def test_auto_join_waits_without_calling_telegram_while_every_account_cools(store, client):
    import outreach_policy

    outreach_policy.account_cooldown("acct", 120, "join", "flood_wait")
    schedule = join_schedule()

    assert await store._execute_schedule(schedule) is False
    assert client.call_count == 0
    record = schedule["last_join_result"]
    assert record["failed"] == 1 and record["stopped_reason"] == "telegram_cooldown"
    assert record["targets"][0]["reason"] == "telegram_cooldown"
    assert record["targets"][1]["state"] == "pending"


@pytest.mark.asyncio
async def test_auto_join_disables_a_schedule_whose_links_went_stale(store, client):
    schedule = join_schedule(join_urls=["not a link!!"])

    assert await store._execute_schedule(schedule) is False
    assert schedule["enabled"] is False
    assert schedule["last_error"] == "invalid_schedule_bounds_or_action"
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_scrape_filters_bots(store, client):
    client.get_participants = AsyncMock(return_value=[
        SimpleNamespace(id=1, bot=False, username="real", first_name="Real", last_name=""),
        SimpleNamespace(id=2, bot=True, username="bot", first_name="Bot", last_name=""),
    ])
    schedule = {
        "id": "sched_scrape", "name": "Members", "cron": "0 9 * * 1",
        "action": "scrape_members", "accounts": ["acct"],
        "scrape_target": "@channel", "scrape_limit": module.SCRAPE_LIMIT_MAX,
        "filter_bots": True,
    }

    assert await store._execute_schedule(schedule) is True
    client.get_participants.assert_awaited_once()
    assert client.get_participants.await_args.kwargs["limit"] == module.SCRAPE_LIMIT_MAX
    assert [member["username"] for member in schedule["last_scrape_result"]["members"]] == ["real"]


@pytest.mark.asyncio
async def test_scrape_disables_a_schedule_whose_limit_was_edited_up(store, client):
    schedule = {
        "id": "sched_scrape", "name": "Members", "cron": "0 9 * * 1",
        "action": "scrape_members", "accounts": ["acct"],
        "scrape_target": "@channel", "scrape_limit": 5000, "filter_bots": True,
    }

    assert await store._execute_schedule(schedule) is False
    assert schedule["enabled"] is False
    assert schedule["last_error"] == "invalid_schedule_bounds_or_action"
    client.get_participants.assert_not_awaited()


# ---- HTTP pass-through ----------------------------------------------------

@pytest.mark.asyncio
async def test_schedule_route_forwards_scrape_and_join_fields(monkeypatch):
    import dashboard

    captured = {}

    def add_schedule(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(dashboard, "task_scheduler", SimpleNamespace(add_schedule=add_schedule))
    monkeypatch.setattr(dashboard.log_manager, "add_log", lambda *args, **kwargs: None)

    await dashboard.add_schedule({
        "schedule_id": "sched_route", "name": "Route", "action": "auto_join",
        "repeat": "weekly", "account_id": "acct",
        "execute_time": {"day": 9, "month": 11, "hour": 9, "minute": 0},
        "join_urls": ["@partnerone"], "join_delay": 40,
    })
    assert captured["join_urls"] == ["@partnerone"]
    assert captured["join_delay"] == 40

    captured.clear()
    await dashboard.add_schedule({
        "schedule_id": "sched_route2", "name": "Route", "action": "scrape_members",
        "repeat": "daily", "account_id": "acct",
        "execute_time": {"day": 9, "month": 11, "hour": 9, "minute": 0},
        "scrape_target": "@channel", "scrape_limit": 321, "filter_bots": False,
    })
    assert captured["scrape_target"] == "@channel"
    assert captured["scrape_limit"] == 321
    assert captured["filter_bots"] is False


# ---- run_workflow schedules -------------------------------------------------
#
# A workflow schedule only starts an internal run: it never passes the outreach
# consent gate, but it must stay inside the same bounds as a manual run
# (1-5 signed-in accounts, an existing workflow owned by the operator).

@pytest.fixture
def workflow_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path / "operator"))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    workflows._RUNS.clear()
    workflows._RUNNING.clear()
    workflows._TASKS.clear()
    return workflows


def _workflow_schedule(**overrides):
    schedule = {
        "id": "wfsch_run", "schedule_id": "wfsch_run", "name": "Sweep",
        "cron": "0 9 * * 1", "action": "run_workflow", "accounts": ["acct"],
        "workflow_id": "wf_test01", "operator_id": "operator-1", "enabled": True,
        "run_count": 0, "fail_count": 0,
    }
    schedule.update(overrides)
    return schedule


def _seed_workflow(workflow_id="wf_test01"):
    workflows._save("operator-1", [{
        "id": workflow_id, "name": "Sweep",
        "nodes": [{"id": "n1", "type": "wait", "config": {"seconds": 1}, "x": 40, "y": 40}],
        "edges": [],
    }])


def test_workflow_requirements_accept_bounded_account_lists():
    assert module.schedule_requirements(
        {"action": "run_workflow", "workflow_id": "wf_abc123", "account_ids": ["a"]}
    ) == (None, [], 0)
    assert module.schedule_requirements(
        {"action": "run_workflow", "workflow_id": "wf_abc123",
         "accounts": ["a", "b", "c", "d", "e"]}
    ) == (None, [], 0)


@pytest.mark.parametrize("schedule", [
    {"action": "run_workflow", "account_ids": ["a"]},
    {"action": "run_workflow", "workflow_id": "nope", "account_ids": ["a"]},
    {"action": "run_workflow", "workflow_id": "wf_abc123", "account_ids": []},
    {"action": "run_workflow", "workflow_id": "wf_abc123", "account_ids": ["a"] * 6},
    {"action": "run_workflow", "workflow_id": "wf_abc123", "account_ids": "a"},
])
def test_workflow_requirements_reject_broken_schedules(schedule):
    with pytest.raises(ValueError):
        module.schedule_requirements(schedule)


def test_add_schedule_persists_workflow_fields(store):
    assert store.add_schedule(
        schedule_id="wfsch_one", name="Sweep (workflow)", cron="30 9 * * 2",
        action="run_workflow", target="workflow", account_ids=["acct"],
        repeat="weekly", workflow_id="wf_abc123", operator_id="operator-1",
    )
    saved = json.loads(Path(module.SCHEDULE_FILE).read_text(encoding="utf-8"))
    record = saved["schedules"]["wfsch_one"]
    assert record["action"] == "run_workflow"
    assert record["workflow_id"] == "wf_abc123"
    assert record["operator_id"] == "operator-1"
    assert record["accounts"] == ["acct"]
    assert record["policy_action"] is None

    assert not store.add_schedule(
        schedule_id="wfsch_bad", name="Bad", cron="30 9 * * 2",
        action="run_workflow", target="workflow", account_ids=["acct"],
        workflow_id="not-a-workflow", operator_id="operator-1",
    )
    assert "wfsch_bad" not in store.schedules


@pytest.mark.asyncio
async def test_a_workflow_schedule_starts_a_run_for_its_operator(store, client, workflow_store):
    _seed_workflow()
    schedule = _workflow_schedule()

    assert await store._execute_schedule(schedule) is True
    task = workflows._TASKS.get("wf_test01")
    assert task is not None
    await task

    run = workflows._history("wf_test01")[0]
    assert run["status"] == "succeeded"
    assert run["account_ids"] == ["acct"]
    assert run["steps"][0]["detail"] == {"waited_seconds": 1}
    assert schedule["last_run"]
    assert schedule["run_count"] == 1
    assert schedule["fail_count"] == 0


@pytest.mark.asyncio
async def test_a_workflow_schedule_disables_itself_when_the_workflow_is_gone(store, client, workflow_store):
    schedule = _workflow_schedule(workflow_id="wf_gone01")

    assert await store._execute_schedule(schedule) is False
    assert schedule["enabled"] is False
    assert schedule["last_error"] == "workflow_missing"
    assert "wf_gone01" not in workflows._TASKS


@pytest.mark.asyncio
async def test_a_workflow_schedule_only_runs_signed_in_accounts(store, client, workflow_store):
    _seed_workflow()
    schedule = _workflow_schedule(accounts=["acct", "signed-out"])

    assert await store._execute_schedule(schedule) is True
    await workflows._TASKS["wf_test01"]
    assert workflows._history("wf_test01")[0]["account_ids"] == ["acct"]


@pytest.mark.asyncio
async def test_a_workflow_schedule_with_no_signed_in_accounts_fails(store, client, workflow_store):
    _seed_workflow()
    schedule = _workflow_schedule(accounts=["ghost"])

    assert await store._execute_schedule(schedule) is False
    assert schedule["fail_count"] == 1
    assert "wf_test01" not in workflows._TASKS
