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
        return [hit("phcasino")], True

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
            return [], False
        return [hit("fallback_channel")], True

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
        return [], False

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
    monkeypatch.setattr(ops, "_tme_preview", preview)
    request = ops.ChannelScrapeRequest(keywords=["casino"], country="Philippines")

    result = await ops._scrape_channels_web(request)

    assert queries == ["casino philippines", "casino"]
    assert result["results"][0]["matched_keyword"] == "casino"


@pytest.mark.asyncio
async def test_web_search_zero_results_names_the_real_cause(monkeypatch):
    monkeypatch.setattr(ops.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(ops.log_manager, "add_log", lambda *args, **kwargs: None)

    async def refused(session, keyword, want, proxy=None):
        raise ops.aiohttp.ClientConnectionError("refused")

    monkeypatch.setattr(ops, "_SEARCH_SOURCES", (("refused", refused),))
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
    monkeypatch.setattr(ops, "_tme_preview", preview)

    result = await ops._scrape_channels_public(ops.ChannelScrapeRequest(keywords=["casino"]))

    assert result["mode"] == "public"
    assert result["diagnostics"]["token_configured"] is False
