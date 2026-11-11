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
    policy.account_cooldown("a", 60, "join_invite", "flood_wait")
    assert policy.account_cooldown_remaining("a", "join_invite") > 0
    assert policy.account_cooldown_remaining("a", "send") == 0
    # A bare read reports the longest wait across every scope.
    assert policy.account_cooldown_remaining("a") > 0


def test_a_hard_account_limit_blocks_every_action(rate_db):
    policy.account_cooldown("a", 60, "account", "account_banned")
    assert policy.account_cooldown_remaining("a", "send") > 0
    assert policy.account_cooldown_remaining("a", "join_invite") > 0
    assert policy.account_cooldown_remaining("a", "join_public") > 0


def test_a_success_clears_only_the_action_it_proved(rate_db):
    policy.account_cooldown("a", 60, "send", "flood_wait")
    policy.account_cooldown("a", 60, "join_invite", "flood_wait")
    policy.clear_account_cooldowns("a", "send")
    assert policy.account_cooldown_remaining("a", "send") == 0
    assert policy.account_cooldown_remaining("a", "join_invite") > 0


def test_a_live_connection_clears_only_the_hard_scope(rate_db):
    policy.account_cooldown("a", 60, "account", "account_frozen")
    policy.account_cooldown("a", 60, "join_invite", "flood_wait")
    policy.clear_account_cooldowns("a", "account")
    assert policy.account_cooldown_remaining("a", "join_invite") > 0
    policy.clear_account_cooldowns("a", None)
    assert policy.account_cooldown_remaining("a", "join_invite") == 0


def test_flood_wait_is_recorded_against_the_action_that_hit_it(rate_db):
    policy.record_provider_restriction("a", errors.FloodWaitError(None, 37), "auto_join_invite")
    assert 30 < policy.account_cooldown_remaining("a", "join_invite") <= 37
    assert policy.account_cooldown_remaining("a", "send") == 0


def test_an_invite_flood_does_not_block_joining_by_username(rate_db):
    policy.record_provider_restriction("a", errors.FloodWaitError(None, 3600), "auto_join_invite")
    assert policy.account_cooldown_remaining("a", "join_invite") > 3000
    # Telegram floods the RPC that was used; the public-username path is separate.
    assert policy.account_cooldown_remaining("a", "join_public") == 0


def test_a_channel_limit_is_a_block_without_a_countdown(rate_db):
    policy.record_provider_restriction("a", errors.ChannelsTooMuchError(None), "auto_join_invite")
    # Not a timer: the account is over Telegram's channel ceiling until it leaves
    # a channel, so showing "next attempt in 23h" would be a lie.
    assert policy.account_cooldown_remaining("a", "join_invite") == 0
    assert policy.account_cooldown_remaining("a", "join_public") == 0
    assert policy.account_cooldown_remaining("a", "send") == 0
    assert policy.account_block("a", "join") == "channel_limit_reached"
    policy.clear_account_block("a", "join")
    assert policy.account_block("a", "join") is None


def test_a_successful_join_clears_a_recorded_channel_block(rate_db):
    policy.block_account("a", "join", "channel_limit_reached")
    policy.clear_account_cooldowns("a", "join_public")
    assert policy.account_block("a", "join") == "channel_limit_reached"
    policy.clear_account_block("a", "join")
    assert policy.account_block("a", "join") is None


def test_a_legacy_shared_join_row_no_longer_blocks_either_method(rate_db):
    with sqlite3.connect(rate_db) as db:
        db.execute("CREATE TABLE cooldowns (account TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT)")
        db.execute("INSERT INTO cooldowns VALUES (?, ?, 'channel_limit_reached')",
                   (f"{policy.hash_identifier('a')}|join", time.time() + 86400))
    policy.prepare_rate_store()
    assert policy.account_cooldown_remaining("a", "join_invite") == 0
    assert policy.account_cooldown_remaining("a", "join_public") == 0
    assert policy.account_cooldown_remaining("a") == 0


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
