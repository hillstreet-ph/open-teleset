"""MTProto proxy catalog.

Only allowlisted public sources are read. A source that fails or serves an
unparseable page is reported per source; it never fails the whole catalog.
Public proxy secrets are not credentials of this product, but they are still
never written to logs.
"""
import asyncio
import base64
import binascii
import html
import ipaddress
import re
import time
from urllib.parse import parse_qs, unquote

import aiohttp

MTPROXY_SOURCES = (
    {
        "id": "radikal",
        "label": "0xRadikal Free-Telegram-Proxy",
        "url": "https://raw.githubusercontent.com/0xRadikal/Free-Telegram-Proxy/main/all/tg.txt",
    },
    {
        "id": "stormycloud",
        "label": "StormyCloud MTProto",
        "url": "https://stormycloud.org/mtproto/",
    },
    {
        "id": "vanced",
        "label": "vanced.to/telegram",
        "url": "https://vanced.to/telegram",
    },
    {
        "id": "tse_proxy",
        "label": "telegramsearchengine.com/proxy",
        "url": "https://telegramsearchengine.com/proxy",
    },
    {
        "id": "proxymtproto",
        "label": "t.me/s/ProXyMTproto",
        "url": "https://t.me/s/ProXyMTproto",
    },
)

CACHE_TTL = 600
LATENCY_TTL = 120
MAX_SOURCE_BYTES = 4 * 1024 * 1024
_LINK = re.compile(r"(?:tg://proxy|(?:https?://)?t(?:elegram)?\.me/proxy)\?([^\s\"'<>\\]+)", re.I)
_HEX = re.compile(r"^[0-9a-f]+$")
_HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)

_cache = {"at": 0.0, "items": [], "sources": []}
_latency = {}
_lock = asyncio.Lock()


def normalize_secret(raw):
    """Return a canonical MTProxy secret, or None when it cannot be used.

    Telethon strips a leading ``dd``/``ee`` tag and accepts the rest as hex or
    base64, so the canonical form here keeps ``dd`` for plain 16-byte secrets
    and preserves ``ee`` fake-TLS secrets byte-for-byte.
    """
    secret = (raw or "").strip()
    if not secret or len(secret) > 256:
        return None
    lowered = secret.lower()
    if _HEX.fullmatch(lowered):
        if lowered.startswith(("dd", "ee")):
            # dd is always dd + 16 bytes. ee may carry a fake-TLS domain.
            if len(lowered) == 34 or (lowered.startswith("ee") and len(lowered) > 34
                                      and len(lowered) % 2 == 0):
                return lowered
            return None
        return "dd" + lowered if len(lowered) == 32 else None
    try:
        decoded = base64.b64decode(secret + "=" * (-len(secret) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded:
        return None
    body = decoded.hex()
    if body.startswith(("dd", "ee")):
        return body
    return "dd" + body if len(decoded) == 16 else None


def _valid_host(host):
    host = (host or "").strip().strip("[]")
    if not host or len(host) > 253:
        return None
    try:
        address = ipaddress.ip_address(host)
        return str(address) if address.is_global else None
    except ValueError:
        return host.lower() if _HOSTNAME.fullmatch(host) else None


def parse_tg_links(text):
    """Extract usable MTProto endpoints from a list file or an HTML page."""
    entries = []
    seen = set()
    # Telegram's own web preview escapes the query separator as ``&amp;``, so
    # entity decoding has to happen before the query string is split.
    decoded = unquote(html.unescape(unquote(text)))
    for match in _LINK.finditer(decoded):
        query = parse_qs(match.group(1))
        host = _valid_host((query.get("server") or [""])[0])
        secret = normalize_secret((query.get("secret") or [""])[0])
        try:
            port = int((query.get("port") or [""])[0])
        except ValueError:
            continue
        if not host or not secret or not 1 <= port <= 65535:
            continue
        key = (host, port, secret)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"host": host, "port": port, "secret": secret})
    return entries


async def fetch_source(source, timeout=12):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(source["url"], allow_redirects=True,
                               headers={"User-Agent": "Mozilla/5.0 (compatible; open-teleset proxy catalog)"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise ValueError("Proxy source exceeds the size limit")
                chunks.append(chunk)
    return parse_tg_links(b"".join(chunks).decode("utf-8", "replace"))


async def fetch_catalog(limit=60, force=False, source_ids=None):
    """Return catalog items plus the status of every allowlisted source."""
    async with _lock:
        fresh = (not force and _cache["at"]
                 and time.monotonic() - _cache["at"] < CACHE_TTL)
        if fresh:
            items = [dict(item) for item in _cache["items"]]
            sources = [dict(source) for source in _cache["sources"]]
        else:
            sources = []
            merged = []
            seen = set()
            chosen = [s for s in MTPROXY_SOURCES if not source_ids or s["id"] in source_ids]
            results = await asyncio.gather(*(fetch_source(s) for s in chosen), return_exceptions=True)
            for source, result in zip(chosen, results):
                if isinstance(result, BaseException):
                    sources.append({"id": source["id"], "label": source["label"],
                                    "count": 0, "status": "unavailable",
                                    "error": type(result).__name__})
                    continue
                sources.append({"id": source["id"], "label": source["label"],
                                "count": len(result), "status": "ok" if result else "empty"})
                for entry in result:
                    key = (entry["host"], entry["port"], entry["secret"])
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append({**entry, "source": source["id"], "source_label": source["label"]})
            _cache.update(at=time.monotonic(), items=merged, sources=sources)
            items = [dict(item) for item in merged]
        items = items[:max(1, min(int(limit), 200))]
        return {"items": items, "sources": sources, "cached": bool(fresh)}


async def tcp_latency(host, port, timeout=3):
    """Return the TCP connect time in ms, or None when unreachable."""
    start = time.perf_counter()
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        return int((time.perf_counter() - start) * 1000)
    except (asyncio.TimeoutError, TimeoutError, OSError):
        return None
    finally:
        if writer:
            writer.close()


async def probe_items(items, timeout=3, concurrency=10):
    """Attach reachability and latency to a page of catalog items."""
    semaphore = asyncio.Semaphore(concurrency)
    now = time.monotonic()

    async def probe(item):
        key = (item["host"], item["port"])
        cached = _latency.get(key)
        if cached and now - cached[0] < LATENCY_TTL:
            latency = cached[1]
        else:
            async with semaphore:
                latency = await tcp_latency(item["host"], item["port"], timeout)
            _latency[key] = (now, latency)
        return {**item, "reachable": latency is not None, "latency_ms": latency}

    probed = await asyncio.gather(*(probe(item) for item in items))
    return sorted(probed, key=lambda item: (not item["reachable"], item["latency_ms"] or 10 ** 9))


async def deep_verify(entry, timeout=12):
    """Prove an MTProto endpoint completes a real Telegram handshake."""
    import os

    from telethon import TelegramClient
    from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate
    from telethon.sessions import StringSession

    host = _valid_host(entry.get("host"))
    secret = normalize_secret(entry.get("secret"))
    try:
        port = int(entry.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not host or not secret or not 1 <= port <= 65535:
        return {"success": False, "error": "Invalid MTProto endpoint."}
    api_id = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        return {"success": False, "error": "Telegram API credentials are not configured."}
    client = TelegramClient(
        StringSession(), api_id, api_hash,
        proxy=(host, port, secret),
        connection=ConnectionTcpMTProxyRandomizedIntermediate,
    )
    start = time.perf_counter()
    try:
        await asyncio.wait_for(client.connect(), timeout)
        return {"success": True, "latency_ms": int((time.perf_counter() - start) * 1000)}
    except (asyncio.TimeoutError, TimeoutError):
        return {"success": False, "error": "MTProto handshake timed out."}
    except Exception as exc:
        return {"success": False, "error": type(exc).__name__}
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 3)
        except Exception:
            pass
