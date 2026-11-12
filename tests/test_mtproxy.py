import asyncio
import base64
from unittest.mock import AsyncMock

import pytest
import mtproxy


def test_secrets_are_normalized_to_telethon_form():
    assert mtproxy.normalize_secret("0" * 32) == "dd" + "0" * 32
    assert mtproxy.normalize_secret("DD" + "a" * 32) == "dd" + "a" * 32
    assert mtproxy.normalize_secret("ee" + "b" * 32) == "ee" + "b" * 32
    assert mtproxy.normalize_secret("ee" + "b" * 40) == "ee" + "b" * 40
    encoded = base64.b64encode(bytes.fromhex("c" * 32)).decode()
    assert mtproxy.normalize_secret(encoded) == "dd" + "c" * 32
    assert mtproxy.normalize_secret("dd" + "d" * 30) is None
    assert mtproxy.normalize_secret("not-a-secret!") is None
    assert mtproxy.normalize_secret("") is None


def test_links_are_parsed_deduped_and_private_hosts_rejected():
    secret = "0" * 32
    text = (
        f"tg://proxy?server=8.8.8.8&port=443&secret={secret}\n"
        f"tg://proxy?server=8.8.8.8&port=443&secret={secret}\n"
        "https://t.me/proxy?server=9.9.9.9&port=8443&secret=ee" + "a" * 32 + "\n"
        "tg://proxy?server=127.0.0.1&port=443&secret=" + secret + "\n"
        "tg://proxy?server=8.8.8.8&port=0&secret=" + secret + "\n"
        "tg://proxy?server=8.8.8.8&port=443&secret=bad\n"
        "not a proxy line\n"
    )
    assert mtproxy.parse_tg_links(text) == [
        {"host": "8.8.8.8", "port": 443, "secret": "dd" + secret},
        {"host": "9.9.9.9", "port": 8443, "secret": "ee" + "a" * 32},
    ]


def test_percent_encoded_html_pages_are_decoded():
    text = ("<a href='tg%3A%2F%2Fproxy%3Fserver%3D8.8.4.4%26port%3D443%26secret%3D"
            + "0" * 32 + "'>proxy</a>")
    assert mtproxy.parse_tg_links(text) == [
        {"host": "8.8.4.4", "port": 443, "secret": "dd" + "0" * 32}
    ]


def test_html_entity_pages_keep_the_port_and_secret():
    """Telegram's t.me/s preview escapes the query separator as ``&amp;``.
    Splitting that raw leaves the port under ``amp;port`` and the row dies."""
    text = ("<a href=\"https://t.me/proxy?server=8.8.4.4&amp;port=443"
            "&amp;secret=dd" + "0" * 32 + "\">proxy</a>")
    assert mtproxy.parse_tg_links(text) == [
        {"host": "8.8.4.4", "port": 443, "secret": "dd" + "0" * 32}
    ]


def test_public_catalog_sources_cover_the_reported_pages():
    urls = {source["url"] for source in mtproxy.MTPROXY_SOURCES}
    assert "https://telegramsearchengine.com/proxy" in urls
    assert "https://t.me/s/ProXyMTproto" in urls
    assert len(urls) == len(mtproxy.MTPROXY_SOURCES)


@pytest.mark.asyncio
async def test_catalog_reports_each_source_and_caches(monkeypatch):
    mtproxy._cache.update(at=0.0, items=[], sources=[])
    entries = [{"host": "8.8.8.8", "port": 443, "secret": "dd" + "0" * 32}]

    async def fake_source(source, timeout=12):
        if source["id"] == "stormycloud":
            raise ValueError("blocked")
        if source["id"] == "vanced":
            return []
        return entries

    monkeypatch.setattr(mtproxy, "fetch_source", fake_source)
    first = await mtproxy.fetch_catalog()
    assert [item["source"] for item in first["items"]] == ["radikal"]
    assert {source["id"]: source["status"] for source in first["sources"]} == {
        "radikal": "ok", "stormycloud": "unavailable", "vanced": "empty",
        "tse_proxy": "ok", "proxymtproto": "ok"}
    assert first["cached"] is False
    second = await mtproxy.fetch_catalog()
    assert second["cached"] is True
    assert await mtproxy.fetch_catalog(force=True, source_ids=["radikal"]) == {
        "items": [{**entries[0], "source": "radikal",
                   "source_label": "0xRadikal Free-Telegram-Proxy"}],
        "sources": [{"id": "radikal", "label": "0xRadikal Free-Telegram-Proxy",
                     "count": 1, "status": "ok"}],
        "cached": False,
    }


@pytest.mark.asyncio
async def test_probe_marks_unreachable_and_sorts(monkeypatch):
    mtproxy._latency.clear()
    items = [{"host": "1.1.1.1", "port": 443}, {"host": "8.8.8.8", "port": 443}]

    async def fake_latency(host, port, timeout=3):
        return 120 if host == "8.8.8.8" else None

    monkeypatch.setattr(mtproxy, "tcp_latency", fake_latency)
    probed = await mtproxy.probe_items(items)
    assert [(item["host"], item["reachable"], item["latency_ms"]) for item in probed] == [
        ("8.8.8.8", True, 120), ("1.1.1.1", False, None)]


@pytest.mark.asyncio
async def test_deep_verify_passes_mtproxy_transport_and_always_disconnects(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs

        async def connect(self):
            calls["connected"] = True

        async def disconnect(self):
            calls["disconnected"] = True

    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "fixture-hash")
    monkeypatch.setattr("telethon.TelegramClient", FakeClient)
    result = await mtproxy.deep_verify({"host": "8.8.8.8", "port": 443, "secret": "0" * 32})
    assert result["success"] is True
    assert calls["kwargs"]["proxy"] == ("8.8.8.8", 443, "dd" + "0" * 32)
    assert calls["kwargs"]["connection"].__name__ == "ConnectionTcpMTProxyRandomizedIntermediate"
    assert calls["connected"] and calls["disconnected"]


@pytest.mark.asyncio
async def test_deep_verify_rejects_without_telegram_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    result = await mtproxy.deep_verify({"host": "8.8.8.8", "port": 443, "secret": "0" * 32})
    assert result["success"] is False
    assert "credentials" in result["error"].lower()
