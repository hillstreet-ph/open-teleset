import sqlite3
import time

import pytest
from telethon import errors

import outreach_policy as policy


@pytest.fixture
def rate_db(tmp_path, monkeypatch):
    path = tmp_path / "rate.sqlite3"
    monkeypatch.setenv("OUTREACH_RATE_DB", str(path))
    return path


def test_a_join_limit_never_blocks_sending(rate_db):
    policy.account_cooldown("a", 60, "join", "flood_wait")
    assert policy.account_cooldown_remaining("a", "join") > 0
    assert policy.account_cooldown_remaining("a", "send") == 0
    # A bare read reports the longest wait across every scope.
    assert policy.account_cooldown_remaining("a") > 0


def test_a_hard_account_limit_blocks_every_action(rate_db):
    policy.account_cooldown("a", 60, "account", "account_banned")
    assert policy.account_cooldown_remaining("a", "send") > 0
    assert policy.account_cooldown_remaining("a", "join") > 0


def test_a_success_clears_only_the_action_it_proved(rate_db):
    policy.account_cooldown("a", 60, "send", "flood_wait")
    policy.account_cooldown("a", 60, "join", "flood_wait")
    policy.clear_account_cooldowns("a", "send")
    assert policy.account_cooldown_remaining("a", "send") == 0
    assert policy.account_cooldown_remaining("a", "join") > 0


def test_a_live_connection_clears_only_the_hard_scope(rate_db):
    policy.account_cooldown("a", 60, "account", "account_frozen")
    policy.account_cooldown("a", 60, "join", "flood_wait")
    policy.clear_account_cooldowns("a", "account")
    assert policy.account_cooldown_remaining("a", "join") > 0
    policy.clear_account_cooldowns("a", None)
    assert policy.account_cooldown_remaining("a", "join") == 0


def test_flood_wait_is_recorded_against_the_action_that_hit_it(rate_db):
    policy.record_provider_restriction("a", errors.FloodWaitError(None, 37), "auto_join")
    assert 30 < policy.account_cooldown_remaining("a", "join") <= 37
    assert policy.account_cooldown_remaining("a", "send") == 0


def test_channel_limit_only_blocks_joining(rate_db):
    policy.record_provider_restriction("a", errors.ChannelsTooMuchError(None), "auto_join")
    assert policy.account_cooldown_remaining("a", "join") > 86000
    assert policy.account_cooldown_remaining("a", "send") == 0


def test_an_old_one_day_row_is_capped_and_marked_legacy(rate_db):
    with sqlite3.connect(rate_db) as db:
        db.execute("CREATE TABLE cooldowns (account TEXT PRIMARY KEY, until REAL NOT NULL)")
        db.execute("INSERT INTO cooldowns VALUES (?, ?)",
                   (policy.hash_identifier("a"), time.time() + 86400))
    policy.prepare_rate_store()
    remaining = policy.account_cooldown_remaining("a")
    assert 0 < remaining <= 900
    with sqlite3.connect(rate_db) as db:
        assert db.execute("SELECT reason FROM cooldowns").fetchone()[0] == "legacy"
