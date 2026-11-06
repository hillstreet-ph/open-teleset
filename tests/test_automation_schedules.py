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
from telethon import errors

import scheduler as module


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "SCHEDULE_FILE", str(tmp_path / "schedules.json"))
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
