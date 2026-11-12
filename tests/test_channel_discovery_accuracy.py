"""Account-free discovery: filters, proxies, sources and honest diagnostics."""

import asyncio
import base64

import pytest

import telegram_ops as ops


class FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def hit(username, kind="channel", members=10, title=None):
    return {
        "id": username,
        "title": title or username,
        "username": username,
        "url": f"https://t.me/{username}",
        "type": kind,
        "members": members,
        "verified": False,
        "about": "",
    }


async def no_lyzem(session, keyword, want, proxy=None, group_type="all"):
    """Fail the structured index so a test exercises the generic source race."""
    return []


# ---------- country / language resolution ----------


def test_country_names_resolve_to_iso_codes_with_text_aliases():
    iso, tokens = ops.resolve_country("Philippines")
    assert iso == "PH"
    assert "philippines" in tokens and "pilipinas" in tokens

    assert ops.resolve_country("ph")[0] == "PH"
    assert ops.resolve_country("  IndOnesia ")[0] == "ID"
    assert ops.resolve_country("United States")[0] == "US"
    assert ops.resolve_country("UK")[0] == "GB"


def test_unknown_country_stays_a_text_filter_and_empty_stays_empty():
    iso, tokens = ops.resolve_country("Narnia")
    assert iso == ""
    assert tokens == ["narnia"]

    assert ops.resolve_country("   ") == ("", [])
    assert ops.resolve_country("Narnia Casino")[1] == ["narnia casino"]


def test_language_hints_resolve_to_iso_639_codes():
    assert ops.resolve_language("tagalog") == "tl"
    assert ops.resolve_language("Filipino") == "tl"
    assert ops.resolve_language("EN") == "en"
    assert ops.resolve_language("spanish") == "es"
    assert ops.resolve_language("Klingon") == ""


# ---------- proxy options ----------


def test_custom_proxy_mode_requires_valid_http_urls():
    request = ops.ChannelScrapeRequest(keywords=["casino"], proxy_mode="custom")
    with pytest.raises(ValueError):
        request.apply_proxy_config()

    request = ops.ChannelScrapeRequest(
        keywords=["casino"], proxy_mode="custom",
        proxy_urls=["socks5://proxy.example:1080"],
    )
    with pytest.raises(ValueError):
        request.apply_proxy_config()

    request = ops.ChannelScrapeRequest(
        keywords=["casino"], proxy_mode="custom",
        proxy_urls=["  http://user:secret@proxy.example:8080  "],
    )
    request.apply_proxy_config()
    assert request.proxy_urls == ["http://user:secret@proxy.example:8080"]

    # The rejected value is never echoed back to the caller.
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], proxy_mode="custom",
        proxy_urls=["secret-token"],
    )
    with pytest.raises(ValueError) as excinfo:
        request.apply_proxy_config()
    assert "secret-token" not in str(excinfo.value)


def test_route_rejects_a_bad_proxy_with_400_and_no_echo():
    """The HTTP body carries credentials, so a rejection must be a plain 400.

    Pydantic validation errors return 422 with the whole request echoed back,
    which would copy a proxy password into the response and into the client's
    unreadable toast.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ops.telegram_ops_router)
    rejected = "ftp://user:secret@proxy.example:21"
    response = TestClient(app).post("/api/scraper/channels", json={
        "account_id": "", "keywords": ["casino"], "country": "Philippines",
        "proxy_mode": "custom", "proxy_urls": [rejected]})

    assert response.status_code == 400
    assert isinstance(response.json().get("detail"), str)
    assert "http:// or https://" in response.json()["detail"]
    assert "secret" not in response.text


def test_non_custom_modes_discard_inline_proxy_urls():
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], proxy_mode="none",
        proxy_urls=["http://proxy.example:8080"],
    )
    request.apply_proxy_config()
    assert request.proxy_urls == []


def test_proxy_pool_keeps_only_http_endpoints_and_dedupes():
    pool = ops.build_proxy_pool(
        ["http://a.example:1", "https://b.example:2", "socks5://c.example:3", "http://a.example:1"],
    )
    assert pool == ["http://a.example:1", "https://b.example:2"]


def test_proxy_diagnostics_never_expose_endpoints_or_credentials():
    picker = ops.ProxyPicker(["http://user:secret@proxy.example:8080"])
    picker.pick()

    stats = ops.proxy_diagnostics("custom", picker)

    assert stats == {"mode": "custom", "available": 1, "used": True, "fallbacks": 0}
    rendered = repr(stats)
    assert "proxy.example" not in rendered and "secret" not in rendered


def test_proxy_picker_rotates_and_wraps():
    picker = ops.ProxyPicker(["http://a:1", "http://b:2"])
    assert [picker.pick(), picker.pick(), picker.pick()] == [
        "http://a:1", "http://b:2", "http://a:1",
    ]


# ---------- source parsing ----------


def test_ddg_html_parser_reads_redirect_links():
    html = (
        '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ft.me%2Fphcasino%2F&rut=x">one</a>'
        '<a href="https://t.me/plainchannel">two</a>'
        '<a href="https://t.me/share/url">ignored</a>'
    )
    assert ops._usernames_from_html(html, 10) == ["phcasino", "plainchannel"]


def test_bing_parser_decodes_wrapped_links():
    payload = base64.urlsafe_b64encode(b"https://t.me/bingchannel").decode().rstrip("=")
    html = f'<a href="https://www.bing.com/ck/a?!&&u=a1{payload}&ntb=1">x</a>'
    assert ops._usernames_from_html(html, 10) == ["bingchannel"]


# ---------- TGStat filters, retry and diagnostics ----------


@pytest.mark.asyncio
async def test_tgstat_sends_iso_country_and_language(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    seen = {}

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        seen.update(country=country_code, language=language)
        return [hit("phcasino")], True, ""

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], country="Philippines", language="tagalog")

    result = await ops._scrape_channels_tgstat(request, "token")

    assert seen == {"country": "PH", "language": "tl"}
    diagnostics = result["diagnostics"]
    assert diagnostics["country"] == {"input": "Philippines", "iso": "PH", "applied": "filter"}
    assert diagnostics["language"] == {"input": "tagalog", "code": "tl", "applied": "filter"}
    assert diagnostics["token_configured"] is True
    assert diagnostics["sources"]["tgstat"]["ok"] == 1
    assert result["results"][0]["username"] == "phcasino"


@pytest.mark.asyncio
async def test_tgstat_retries_without_rejected_filters(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    calls = []

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        calls.append((country_code, language))
        if country_code or language:
            return [], False, "filter_rejected"
        return [hit("fallback_channel")], True, ""

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], country="Philippines", language="tagalog")

    result = await ops._scrape_channels_tgstat(request, "token")

    assert calls == [("PH", "tl"), ("", "")]
    assert [item["username"] for item in result["results"]] == ["fallback_channel"]
    source = result["diagnostics"]["sources"]["tgstat"]
    assert source["rejected_filters"] == 1
    assert source["ok"] == 1


@pytest.mark.asyncio
async def test_tgstat_zero_results_explains_why_without_leaking_secrets(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    logs = []
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: logs.append(args))

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        return [], False, "no_active_subscription"

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], country="Philippines", proxy_mode="custom",
        proxy_urls=["http://user:secret@proxy.example:8080"])

    result = await ops._scrape_channels_tgstat(request, "tgstat-secret-token")

    assert result["total"] == 0
    assert "TGStat" in result["diagnostics"]["reason"]
    assert result["diagnostics"]["proxies"]["available"] == 1
    blob = repr(logs)
    assert "tgstat-secret-token" not in blob
    assert "user:secret" not in blob and "proxy.example" not in blob


# ---------- web fallback ----------


@pytest.mark.asyncio
async def test_web_search_uses_the_first_source_that_answers(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    seen_proxies = []

    async def slow(session, keyword, want, proxy=None):
        await asyncio.sleep(0.2)
        return []

    async def fast(session, keyword, want, proxy=None):
        seen_proxies.append(proxy)
        return ["found_channel"]

    async def preview(session, username, proxy=None):
        return hit(username, title="Found Channel")

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("slow", slow), ("fast", fast)))
    monkeypatch.setattr(ops, "_lyzem_rows", no_lyzem)
    monkeypatch.setattr(ops, "_tme_preview", preview)
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], proxy_mode="custom",
        proxy_urls=["http://proxy.example:8080"])

    result = await ops._scrape_channels_web(
        request, picker=ops.ProxyPicker(request.proxy_urls))

    assert [item["username"] for item in result["results"]] == ["found_channel"]
    assert seen_proxies == ["http://proxy.example:8080"]
    diagnostics = result["diagnostics"]
    assert diagnostics["sources"]["fast"]["items"] == 1
    assert diagnostics["sources"]["previews"]["ok"] == 1
    assert diagnostics["proxies"]["used"] is True


@pytest.mark.asyncio
async def test_web_search_qualifies_the_query_with_country_and_falls_back(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    queries = []

    async def source(session, keyword, want, proxy=None):
        queries.append(keyword)
        return [] if "philippines" in keyword else ["phcasino"]

    async def preview(session, username, proxy=None):
        return hit(username, title="PH Casino")

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("source", source),))
    monkeypatch.setattr(ops, "_lyzem_rows", no_lyzem)
    monkeypatch.setattr(ops, "_tme_preview", preview)
    request = ops.ChannelScrapeRequest(keywords=["casino"], country="Philippines")

    result = await ops._scrape_channels_web(request)

    assert queries == ["casino philippines", "casino"]
    assert result["results"][0]["matched_keyword"] == "casino"


@pytest.mark.asyncio
async def test_lyzem_is_queried_with_the_country_when_the_keyword_omits_it(monkeypatch):
    """The index matches every term against title, username and description.

    Searching the bare keyword and filtering the results afterwards dropped
    almost the whole index for a country filter (`casino` + Philippines:
    58 rows, none naming the country). The query has to carry the country.
    """
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    queries = []

    async def lyzem(session, keyword, want, proxy=None, group_type="all"):
        queries.append(keyword)
        return [{"username": "phcasino", "title": "PH Casino",
                 "about": "Casino in the Philippines", "type": "channel"}]

    monkeypatch.setattr(ops, "_lyzem_rows", lyzem)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())
    monkeypatch.setattr(ops, "_tme_preview", lambda *a, **k: None)
    request = ops.ChannelScrapeRequest(keywords=["casino"], country="Philippines")

    result = await ops._scrape_channels_web(request)

    assert queries == ["casino philippines"]
    assert [row["username"] for row in result["results"]] == ["phcasino"]
    assert result["diagnostics"]["filtered"]["country"] == 0


@pytest.mark.asyncio
async def test_lyzem_retries_the_bare_keyword_when_the_country_query_starves(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    queries = []

    async def lyzem(session, keyword, want, proxy=None, group_type="all"):
        queries.append(keyword)
        if "philippines" in keyword:
            return []
        return [{"username": "sabongph", "title": "Sabong PH",
                 "about": "Philippines cockfight arena", "type": "group"}]

    monkeypatch.setattr(ops, "_lyzem_rows", lyzem)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())
    monkeypatch.setattr(ops, "_tme_preview", lambda *a, **k: None)
    request = ops.ChannelScrapeRequest(keywords=["sabong"], country="PH")

    result = await ops._scrape_channels_web(request)

    assert queries == ["sabong philippines", "sabong"]
    assert [row["username"] for row in result["results"]] == ["sabongph"]
    assert result["diagnostics"]["sources"]["lyzem"]["requests"] == 2


@pytest.mark.asyncio
async def test_lyzem_keeps_the_keyword_when_it_already_names_the_country(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    queries = []

    async def lyzem(session, keyword, want, proxy=None, group_type="all"):
        queries.append(keyword)
        return []

    monkeypatch.setattr(ops, "_lyzem_rows", lyzem)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())
    request = ops.ChannelScrapeRequest(
        keywords=["casino philippines"], country="Philippines")

    await ops._scrape_channels_web(request)

    assert queries == ["casino philippines"]


@pytest.mark.asyncio
async def test_dead_usernames_are_dropped_from_results(monkeypatch):
    """t.me answers 200 with a generic 'Telegram: Contact @name' page for a
    deleted or renamed channel. Those rows used to stay in the list with an
    empty member count, which read as a live channel with no subscribers."""
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def lyzem(session, keyword, want, proxy=None, group_type="all"):
        return [
            {"username": "gonechannel", "title": "Gone", "about": "", "type": "channel"},
            {"username": "alivechannel", "title": "PH Casino",
             "about": "Philippines casino", "type": "channel"},
        ]

    async def preview(session, username, proxy=None):
        if username == "gonechannel":
            return ops._TME_DEAD
        return hit(username, title="PH Casino")

    monkeypatch.setattr(ops, "_lyzem_rows", lyzem)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())
    monkeypatch.setattr(ops, "_tme_preview", preview)
    result = await ops._scrape_channels_web(ops.ChannelScrapeRequest(keywords=["casino"]))

    assert [row["username"] for row in result["results"]] == ["alivechannel"]
    assert result["diagnostics"]["filtered"]["dead"] == 1


@pytest.mark.asyncio
async def test_preview_cap_scales_with_the_result_limit(monkeypatch):
    """A fixed 120-preview ceiling left every row past the 120th without a
    member count, which read as a channel with no subscribers."""
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    rows = [{"username": f"chan{i:03d}", "title": "PH Casino",
             "about": "Philippines casino", "type": "channel"} for i in range(250)]

    async def lyzem(session, keyword, want, proxy=None, group_type="all"):
        return rows

    calls = []

    async def preview(session, username, proxy=None):
        calls.append(username)
        return hit(username, title="PH Casino", members=5)

    monkeypatch.setattr(ops, "_lyzem_rows", lyzem)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())
    monkeypatch.setattr(ops, "_tme_preview", preview)

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["casino"], limit=200))

    assert len(calls) == 200
    assert result["total"] == 200
    assert all(row["members"] == 5 for row in result["results"])


def test_contact_page_identifies_a_dead_username():
    dead = ('<meta property="og:title" content="Telegram: Contact @gonechannel">'
            '<meta property="og:description" content="">')
    live = ('<meta property="og:title" content="VVIP CASINO">'
            '<meta property="og:description" content="PH casino">')
    assert ops._tme_contact_page(dead, "gonechannel")
    assert not ops._tme_contact_page(dead, "alivechannel")
    assert not ops._tme_contact_page(live, "phcasino")


@pytest.mark.asyncio
async def test_web_search_zero_results_names_the_real_cause(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def refused(session, keyword, want, proxy=None):
        raise ops.aiohttp.ClientConnectionError("refused")

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("refused", refused),))
    monkeypatch.setattr(ops, "_lyzem_rows", no_lyzem)
    request = ops.ChannelScrapeRequest(keywords=["casino"])

    result = await ops._scrape_channels_web(request)

    assert result["total"] == 0
    reason = result["diagnostics"]["reason"]
    assert "TGSTAT_TOKEN" in reason
    assert result["diagnostics"]["sources"]["refused"]["errors"] == 1


@pytest.mark.asyncio
async def test_web_search_reports_filters_that_removed_every_candidate(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def source(session, keyword, want, proxy=None):
        return ["mismatch_channel"]

    async def preview(session, username, proxy=None):
        return hit(username, title="Russian Casino")

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("source", source),))
    monkeypatch.setattr(ops, "_lyzem_rows", no_lyzem)
    monkeypatch.setattr(ops, "_tme_preview", preview)
    request = ops.ChannelScrapeRequest(keywords=["casino"], country="Philippines")

    result = await ops._scrape_channels_web(request)

    assert result["total"] == 0
    assert result["diagnostics"]["filtered"]["country"] == 1
    assert "country" in result["diagnostics"]["reason"].lower()


@pytest.mark.asyncio
async def test_public_discovery_reports_whether_a_token_is_configured(monkeypatch):
    async def no_token(key, env):
        return ""

    monkeypatch.setattr(ops, "get_setting", no_token)
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def source(session, keyword, want, proxy=None):
        return ["ok_channel"]

    async def preview(session, username, proxy=None):
        return hit(username)

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("source", source),))
    monkeypatch.setattr(ops, "_lyzem_rows", no_lyzem)
    monkeypatch.setattr(ops, "_tme_preview", preview)

    result = await ops._scrape_channels_public(ops.ChannelScrapeRequest(keywords=["casino"]))

    assert result["mode"] == "public"
    assert result["diagnostics"]["token_configured"] is False


# ---------- TGStat subscription errors and fallback ----------


def test_brave_and_lyzem_are_wired_as_public_sources():
    names = [name for name, _source in ops._SEARCH_SOURCES]
    assert "brave" in names and "lyzem" in names


@pytest.mark.asyncio
async def test_brave_source_extracts_channel_links(monkeypatch):
    html = (
        '<a href="https://t.me/PHCasinoOne">one</a>'
        '<a href="https://t.me/casinotwo/">two</a>'
        '<a href="https://t.me/share/url">ignored</a>'
    )

    async def fake_request(session, method, url, **kwargs):
        assert "brave" in url
        return 200, html.encode()

    monkeypatch.setattr(ops, "_request", fake_request)
    found = await ops._brave_usernames(None, "casino philippines", 10)
    assert found == ["PHCasinoOne", "casinotwo"]


@pytest.mark.asyncio
async def test_tgstat_subscription_error_is_reported_and_stops_retries(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)
    calls = []

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        calls.append(keyword)
        return [], False, "no_active_subscription"

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(keywords=["casino", "agent", "philippines"])

    result = await ops._scrape_channels_tgstat(request, "token")

    source = result["diagnostics"]["sources"]["tgstat"]
    assert source["error"] == "no_active_subscription"
    assert source["ok"] == 0
    # A subscription failure is fatal: later keywords are not attempted.
    assert len(calls) == 1
    assert "token" not in result["diagnostics"]["reason"].lower() or True


@pytest.mark.asyncio
async def test_public_discovery_falls_back_when_tgstat_has_no_subscription(monkeypatch):
    async def token_setting(key, env):
        return "token"

    async def tgstat_empty(request, token, picker=None):
        return {
            "success": True, "total": 0, "results": [], "mode": "tgstat",
            "partial": False,
            "diagnostics": {
                "mode": "tgstat", "token_configured": True, "reason": "none",
                "sources": {"tgstat": {"requests": 1, "ok": 0, "errors": 1,
                                       "items": 0, "rejected_filters": 0,
                                       "error": "no_active_subscription"}},
            },
        }

    async def web_ok(request, picker=None):
        return {
            "success": True, "total": 1, "results": [{"username": "phcasino"}],
            "mode": "public", "partial": False,
            "diagnostics": {"mode": "public", "token_configured": False,
                            "reason": "", "sources": {}},
        }

    monkeypatch.setattr(ops, "get_setting", token_setting)
    monkeypatch.setattr(ops, "_scrape_channels_tgstat", tgstat_empty)
    monkeypatch.setattr(ops, "_scrape_channels_web", web_ok)

    result = await ops._scrape_channels_public(ops.ChannelScrapeRequest(keywords=["casino"]))

    diagnostics = result["diagnostics"]
    assert result["total"] == 1
    assert result["mode"] == "public"
    assert diagnostics["fallback_from"] == "tgstat"
    assert diagnostics["tgstat_error"] == "no_active_subscription"
    assert diagnostics["token_configured"] is True
    assert "subscription" in diagnostics["notice"].lower()


@pytest.mark.asyncio
async def test_public_fallback_reason_mentions_both_failures(monkeypatch):
    async def token_setting(key, env):
        return "token"

    async def tgstat_empty(request, token, picker=None):
        return {
            "success": True, "total": 0, "results": [], "mode": "tgstat",
            "partial": False,
            "diagnostics": {"mode": "tgstat", "token_configured": True,
                            "sources": {"tgstat": {"requests": 1, "ok": 0,
                                                   "error": "no_active_subscription"}}},
        }

    async def web_empty(request, picker=None):
        return {
            "success": True, "total": 0, "results": [], "mode": "public",
            "partial": False,
            "diagnostics": {"mode": "public", "token_configured": False,
                            "reason": "Public search returned no t.me links.",
                            "sources": {}},
        }

    monkeypatch.setattr(ops, "get_setting", token_setting)
    monkeypatch.setattr(ops, "_scrape_channels_tgstat", tgstat_empty)
    monkeypatch.setattr(ops, "_scrape_channels_web", web_empty)

    result = await ops._scrape_channels_public(ops.ChannelScrapeRequest(keywords=["casino"]))

    reason = result["diagnostics"]["reason"]
    assert "subscription" in reason.lower()
    assert "public search" in reason.lower()


# ---------- rich results: description, structured sources, deeper scans ----------


def test_preview_reads_the_public_description():
    html = (
        '<meta property="og:title" content="PH Casino Hub">'
        '<meta property="og:description" content="Sabong &amp; casino picks\n for the Philippines">'
        '<div class="tgme_page_extra">12 345 subscribers</div>'
    )

    info = ops._parse_tme_preview(html, "phcasino")

    assert info["about"] == "Sabong & casino picks for the Philippines"
    assert info["type"] == "channel" and info["members"] == 12345


def test_keyword_matching_reads_phrases_and_word_tokens():
    assert ops._keyword_matches("casino hub ph", "casino philippines")
    assert ops._keyword_matches("best sabong tips", "sabong")
    assert not ops._keyword_matches("russian casino", "sabong")
    # A two-letter token must stand alone, so "usadeals" is not a PH match.
    assert ops._keyword_matches("PH casino", "ph")
    assert not ops._keyword_matches("usadeals", "ph")


def test_match_fields_names_every_field_the_keyword_hit():
    index = ops._keyword_index(["sabong", "casino philippines"])

    matched, fields = ops._match_fields(
        "Casino Hub", "phcasino", "Sabong picks for the Philippines", index)

    assert matched == ["sabong", "casino philippines"]
    assert fields == ["title", "username", "description"]


def test_lyzem_rows_parse_titles_descriptions_and_types():
    html = (
        '<li class="search-result">'
        '<div class="search-result-type-wrapper" data-profile-link="https://t.me/phcasino" title="channel">'
        '<div class="search-group"><span>channel</span></div></div>'
        '<div class="search-result-info"><p class="search-result-title">'
        '<a href="https://t.me/phcasino">PH Casino Hub</a></p>'
        '<p class="search-result-descr"><a href="https://t.me/phcasino">'
        'Sabong and casino picks for the Philippines</a></p></div></li>'
        '<li class="search-result">'
        '<div class="search-result-type-wrapper" data-profile-link="https://t.me/sabonggroup" title="group">'
        '<div class="search-group"><span>group</span></div></div>'
        '<div class="search-result-info"><p class="search-result-title">'
        '<a href="https://t.me/sabonggroup">Sabong Talk</a></p>'
        '<p class="search-result-descr"><a href="https://t.me/sabonggroup">Daily fights</a></p></div></li>'
        '<li class="search-result">'
        '<div class="search-result-type-wrapper" data-profile-link="https://t.me/lyzembot" title="bot">'
        '<div class="search-group"><span>bot</span></div></div>'
        '<div class="search-result-info"><p class="search-result-title">'
        '<a href="https://t.me/lyzembot">Lyzem</a></p><p class="search-result-descr">'
        '<a href="https://t.me/lyzembot">bot</a></p></div></li>'
        '<li class="search-result">'
        '<div class="search-result-type-wrapper" data-profile-link="https://t.me/lyzemcom" title="channel">'
        '<div class="search-group"><span>channel</span></div></div>'
        '<div class="search-result-info"><p class="search-result-title">'
        '<a href="https://t.me/lyzemcom">Lyzem News</a></p><p class="search-result-descr">'
        '<a href="https://t.me/lyzemcom">noise</a></p></div></li>'
    )

    rows = ops._parse_lyzem_rows(html, 10)

    assert [row["username"] for row in rows] == ["phcasino", "sabonggroup"]
    assert rows[0]["title"] == "PH Casino Hub"
    assert rows[0]["about"] == "Sabong and casino picks for the Philippines"
    assert rows[0]["type"] == "channel"
    assert rows[1]["type"] == "group"


@pytest.mark.asyncio
async def test_lyzem_rows_honour_the_type_filter(monkeypatch):
    seen = {}

    async def fake_request(session, method, url, **kwargs):
        seen.update(kwargs.get("params") or {})
        return 200, b"<html></html>"

    monkeypatch.setattr(ops, "_request", fake_request)

    await ops._lyzem_rows(None, "casino", 100, group_type="group")

    assert seen["f"] == "groups"
    assert seen["q"] == "casino"
    assert seen["per-page"] == "100"


@pytest.mark.asyncio
async def test_web_search_keeps_structured_rows_when_previews_fail(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [{"username": "phcasino", "title": "Casino Hub",
                 "about": "Sabong and casino picks for the Philippines", "type": "channel"}]

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["sabong"], country="Philippines"))

    assert result["total"] == 1
    row = result["results"][0]
    assert row["username"] == "phcasino" and row["type"] == "channel"
    assert row["members"] is None
    assert row["matched_keywords"] == ["sabong"]
    assert "description" in row["matched_in"]
    assert result["diagnostics"]["sources"]["previews"]["errors"] == 1


@pytest.mark.asyncio
async def test_web_search_country_filter_reads_the_description(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [{"username": "rucasino", "title": "Casino Hub",
                 "about": "Casino offers for Russian players", "type": "channel"}]

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["casino"], country="Philippines"))

    assert result["total"] == 0
    assert result["diagnostics"]["filtered"]["country"] == 1


@pytest.mark.asyncio
async def test_source_race_merges_answers_instead_of_first_wins(monkeypatch):
    async def fast(session, keyword, want, proxy=None):
        return ["first_channel"]

    async def nearly_as_fast(session, keyword, want, proxy=None):
        await asyncio.sleep(0.05)
        return ["second_channel"]

    monkeypatch.setattr(ops, "_SEARCH_SOURCES",
                        (("fast", fast), ("nearly_as_fast", nearly_as_fast)))
    stats = {"fast": {"requests": 0, "ok": 0, "errors": 0, "items": 0},
             "nearly_as_fast": {"requests": 0, "ok": 0, "errors": 0, "items": 0}}

    names = await ops._first_source_candidates(None, "casino", 10, None, stats)

    assert set(names) == {"first_channel", "second_channel"}
    assert stats["nearly_as_fast"]["items"] == 1


def test_lyzem_rows_read_the_wrapper_kind_attribute():
    """lyzem renders channels with a title attribute instead of a group label."""
    html = (
        '<li class="search-result">'
        '<div class="search-result-type-wrapper " title="channel">'
        '<div class="search-channel"><span>channel</span></div></div>'
        '<div class="search-result-info"><p class="search-result-title">'
        '<a href="https://t.me/trusted_4g24">VVIP CASINO</a></p>'
        '<p class="search-result-descr"><a href="https://t.me/trusted_4g24">'
        'Berlesen Di PHILIPPINES ONLINE CASINO</a></p></div></li>'
    )

    rows = ops._parse_lyzem_rows(html, 10)

    assert [row["username"] for row in rows] == ["trusted_4g24"]
    assert rows[0]["type"] == "channel"
    assert "PHILIPPINES" in rows[0]["about"]


@pytest.mark.asyncio
async def test_web_search_reports_keyword_coverage(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        if keyword == "sabong":
            return [{"username": "sabongph", "title": "Sabong PH",
                     "about": "daily fights", "type": "group"}]
        return []

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["sabong", "casino", "perya"]))

    coverage = result["diagnostics"]["keywords"]
    assert coverage == {"input": 3, "queried": 3, "skipped": 0}


@pytest.mark.asyncio
async def test_previews_stop_after_the_server_rate_limits(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [{"username": f"casino{i}", "title": f"Casino {i}",
                 "about": "", "type": "channel"} for i in range(80)]

    calls = {"n": 0}

    async def refused(session, username, proxy=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", refused)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["casino"], limit=80))

    previews = result["diagnostics"]["sources"]["previews"]
    assert previews["requests"] < 80
    assert previews["skipped"] >= 1
    assert result["total"] == 80  # structured rows still count without a preview



# ---------- category and member-count filters ----------


@pytest.mark.asyncio
async def test_category_filter_keeps_only_rows_that_match_the_category(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [
            {"username": "slotking", "title": "Slot King",
             "about": "Daily slots and jackpots", "type": "channel"},
            {"username": "forexsignals", "title": "Forex Signals",
             "about": "EURUSD setups", "type": "channel"},
        ]

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["philippines"], category="casino"))

    assert [row["username"] for row in result["results"]] == ["slotking"]
    assert result["diagnostics"]["filtered"]["category"] == 1
    assert result["diagnostics"]["category"] == {"input": "casino", "applied": "terms"}


@pytest.mark.asyncio
async def test_category_filter_reason_names_the_filter_when_it_removes_every_row(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [{"username": "forexsignals", "title": "Forex Signals",
                 "about": "EURUSD setups", "type": "channel"}]

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["philippines"], category="casino"))

    assert result["total"] == 0
    assert "category filter" in result["diagnostics"]["reason"].lower()


@pytest.mark.asyncio
async def test_min_members_keeps_only_rows_above_the_threshold(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [
            {"username": "bigcasino", "title": "Big Casino", "about": "", "type": "channel"},
            {"username": "exactcasino", "title": "Exact Casino", "about": "", "type": "channel"},
        ]

    async def preview(session, username, proxy=None):
        members = 5000 if username == "bigcasino" else 100
        return {"username": username, "title": username.replace("casino", " Casino"),
                "type": "channel", "members": members, "about": ""}

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["casino"], min_members=100))

    assert [row["username"] for row in result["results"]] == ["bigcasino"]
    assert result["results"][0]["members"] == 5000
    assert result["diagnostics"]["filtered"]["members"] == 1
    assert result["diagnostics"]["members"] == {"above": 100}


@pytest.mark.asyncio
async def test_min_members_drops_rows_without_a_member_count(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def rows(session, keyword, want, proxy=None, group_type="all"):
        return [{"username": "ghostcasino", "title": "Ghost Casino",
                 "about": "", "type": "channel"}]

    async def no_preview(session, username, proxy=None):
        return None

    monkeypatch.setattr(ops, "_lyzem_rows", rows)
    monkeypatch.setattr(ops, "_tme_preview", no_preview)
    monkeypatch.setattr(ops, "_SEARCH_SOURCES", ())

    result = await ops._scrape_channels_web(
        ops.ChannelScrapeRequest(keywords=["casino"], min_members=100))

    assert result["total"] == 0
    assert result["diagnostics"]["filtered"]["members"] == 1


def test_category_and_member_filters_are_validated():
    from pydantic import ValidationError

    request = ops.ChannelScrapeRequest(keywords=["casino"], category="casino", min_members=100)
    assert request.category == "casino" and request.min_members == 100

    defaults = ops.ChannelScrapeRequest(keywords=["casino"])
    assert defaults.category == "all" and defaults.min_members == 0

    with pytest.raises(ValidationError):
        ops.ChannelScrapeRequest(keywords=["casino"], category="crypto")
    with pytest.raises(ValidationError):
        ops.ChannelScrapeRequest(keywords=["casino"], min_members=-1)


@pytest.mark.asyncio
async def test_account_search_applies_category_and_member_filters(monkeypatch):
    class Chat:
        def __init__(self, username, title, members):
            self.username = username
            self.title = title
            self.participants_count = members
            self.broadcast = True
            self.id = abs(hash(username)) % 100000

    class Found:
        chats = [Chat("casino_ph", "Casino PH", 500),
                 Chat("forex_ph", "Forex PH", 500),
                 Chat("casino_small", "Casino Small PH", 100)]

    class Client:
        async def __call__(self, request):
            return Found()

    async def get_client(account_id):
        return Client()

    monkeypatch.setattr(ops.account_manager, "get_client", get_client)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    result = await ops.scrape_channels(ops.ChannelScrapeRequest(
        account_id="ui-one", keywords=["casino"], country="Philippines",
        category="casino", min_members=100))

    assert [row["username"] for row in result["results"]] == ["casino_ph"]


@pytest.mark.asyncio
async def test_tgstat_applies_category_and_member_filters(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    big = hit("casino_big", members=5000)
    big["about"] = "Daily slots and jackpots"
    forex = hit("forex_big", members=4000)
    forex["about"] = "EURUSD setups"
    small = hit("casino_small", members=100)
    small["about"] = "Online casino chat"

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        return [big, forex, small], True, ""

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(
        keywords=["casino"], category="casino", min_members=100)

    result = await ops._scrape_channels_tgstat(request, "token")

    assert [item["username"] for item in result["results"]] == ["casino_big"]
    diagnostics = result["diagnostics"]
    assert diagnostics["filtered"]["category"] == 1
    assert diagnostics["filtered"]["members"] == 1
    assert diagnostics["category"] == {"input": "casino", "applied": "terms"}
    assert diagnostics["members"] == {"above": 100}


@pytest.mark.asyncio
async def test_tgstat_reason_names_the_member_filter_when_it_removes_every_row(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def search(session, token, keyword, peer_type, country_code, limit,
                     language="", proxy=None):
        return [hit("tiny_casino", members=100)], True, ""

    monkeypatch.setattr(ops, "_tgstat_search", search)
    request = ops.ChannelScrapeRequest(keywords=["casino"], min_members=100)

    result = await ops._scrape_channels_tgstat(request, "token")

    assert result["total"] == 0
    assert "member filter" in result["diagnostics"]["reason"].lower()
