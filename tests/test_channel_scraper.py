"""Channel discovery must fan out lookups and stay inside the client timeout."""

import asyncio
import time

import pytest

import telegram_ops as ops


class FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def hit(username, kind="channel"):
    return {
        "id": username,
        "title": username,
        "username": username,
        "url": f"https://t.me/{username}",
        "type": kind,
        "members": 10,
        "verified": False,
        "about": "",
    }


@pytest.mark.asyncio
async def test_tgstat_keyword_lookups_overlap_instead_of_running_serially(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    active = 0
    peak = 0

    async def search(session, token, keyword, peer_type, country_code, limit):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.2)
        active -= 1
        return [hit(f"{keyword}_channel")]

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(keywords=["alpha", "bravo", "charlie", "delta"])

    started = time.monotonic()
    result = await ops._scrape_channels_tgstat(request, "token")
    elapsed = time.monotonic() - started

    assert peak > 1, "keyword lookups should overlap"
    assert elapsed < 0.7, f"four 0.2 s lookups took {elapsed:.2f} s"
    assert result["mode"] == "tgstat"
    assert [item["username"] for item in result["results"]] == [
        "alpha_channel", "bravo_channel", "charlie_channel", "delta_channel",
    ]


@pytest.mark.asyncio
async def test_tgstat_dedupes_and_stops_at_the_requested_limit(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def search(session, token, keyword, peer_type, country_code, limit):
        return [hit("shared_channel"), hit("shared_channel"), hit(f"{keyword}_channel")]

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(keywords=["alpha", "bravo"], limit=3)

    result = await ops._scrape_channels_tgstat(request, "token")

    usernames = [item["username"] for item in result["results"]]
    assert len(usernames) == 3
    assert len(set(usernames)) == len(usernames)


@pytest.mark.asyncio
async def test_web_fallback_drops_previews_that_miss_the_budget(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(ops, "_PUBLIC_PREVIEW_BUDGET", 0.05)

    async def discover(session, keyword, want):
        return ["slow_channel", "quick_channel"]

    async def preview(session, username):
        await asyncio.sleep(0.2 if username == "slow_channel" else 0)
        return hit(username)

    monkeypatch.setattr(ops, "_ddg_usernames", discover)
    monkeypatch.setattr(ops, "_tme_preview", preview)
    request = ops.ChannelScrapeRequest(keywords=["alpha"])

    started = time.monotonic()
    result = await ops._scrape_channels_web(request)
    elapsed = time.monotonic() - started

    assert elapsed < 0.18, f"fallback waited {elapsed:.2f} s past its budget"
    assert [item["username"] for item in result["results"]] == ["quick_channel"]


def test_keyword_list_is_not_capped_at_twenty_and_dedupes_case_insensitively():
    keywords = [f"term {index}" for index in range(30)] + ["TERM 1"]

    request = ops.ChannelScrapeRequest(keywords=keywords, limit=5000)

    assert len(request.keywords) == 30
    assert request.keywords[1] == "term 1"
    assert request.limit == 5000


@pytest.mark.asyncio
async def test_keyword_budget_returns_partial_results_instead_of_failing(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(ops, "KEYWORD_SEARCH_BUDGET", 0.02)

    async def search(session, token, keyword, peer_type, country_code, limit):
        await asyncio.sleep(0.05)
        return [hit(f"{keyword}_channel")]

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(keywords=[f"kw{index}" for index in range(20)])

    result = await ops._scrape_channels_tgstat(request, "token")

    assert result["partial"] is True
    assert 0 < len(result["results"]) < 20

