#!/usr/bin/env python3
"""
Telegram Operations — Scraper, Member Adder, Bulk Personal Sender

FastAPI APIRouter providing REST endpoints for:
1. POST /api/scraper/members      — scrape members from channel/group
2. POST /api/members/add          — add members to channel/group
3. POST /api/bulk/send-personal   — send personal messages to many users
"""
import asyncio
import re as _re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional, Literal

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from telethon import errors, functions, types, utils

from open_teleset.app_settings import get_setting

from account_manager import account_manager
from log_manager import log_manager
from outreach_policy import OutreachDenied, outreach_policy, dispatch_outreach, record_provider_restriction
from stats_tracker import stats_tracker


telegram_ops_router = APIRouter()
ActivityFilter = Literal["all", "recently", "7_days", "15_days", "30_days"]


def _client_unavailable_detail(account_id: str) -> str:
    return (account_manager.connection_errors.get(account_id)
            or "Client unavailable — account not connected")


def activity_details(user):
    """Expose only the status Telegram actually shared, never inferred timestamps."""
    status = getattr(user, "status", None)
    if isinstance(status, types.UserStatusOnline):
        return {"activity": "online", "last_seen": None}
    if isinstance(status, types.UserStatusOffline):
        seen = status.was_online
        if isinstance(seen, datetime):
            seen = seen.replace(tzinfo=timezone.utc) if seen.tzinfo is None else seen.astimezone(timezone.utc)
            return {"activity": "offline", "last_seen": seen.isoformat()}
    for status_type, label in ((types.UserStatusRecently, "recently"),
                               (types.UserStatusLastWeek, "within_week"),
                               (types.UserStatusLastMonth, "within_month")):
        if isinstance(status, status_type):
            return {"activity": label, "last_seen": None}
    return {"activity": "unknown", "last_seen": None}


def matches_activity(user, selected: ActivityFilter, now=None):
    if selected == "all":
        return True
    details = activity_details(user)
    current = now or datetime.now(timezone.utc)
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    if details["activity"] in {"online", "recently"}:
        return True
    if details["last_seen"]:
        days = 3 if selected == "recently" else int(selected.split("_")[0])
        return current - timedelta(days=days) <= datetime.fromisoformat(details["last_seen"]) <= current
    if details["activity"] == "within_week":
        return selected in {"7_days", "15_days", "30_days"}
    # Telegram's month bucket cannot establish a 15-day cutoff.
    return details["activity"] == "within_month" and selected == "30_days"


# ============ Request Models ============

class ScrapeRequest(BaseModel):
    account_id: str
    target: str              # channel/group username or ID
    limit: int = Field(default=200, ge=1, le=10000, strict=True)  # scan cap before filtering
    filter_bots: bool = True # exclude bots
    activity_filter: ActivityFilter = "all"
    admin_filter: Literal["include", "exclude"] = "include"


class ChannelScrapeRequest(BaseModel):
    account_id: str = ""   # optional: empty = account-free public discovery
    keywords: List[str] = Field(default_factory=list)      # directory search terms
    country: str = Field(default="", max_length=64)        # optional title/username filter
    group_type: Literal["all", "group", "channel"] = "all"
    limit: int = Field(default=100, ge=1, le=500, strict=True)  # max results after filtering

    @model_validator(mode="after")
    def _normalise(self):
        cleaned = []
        for raw in self.keywords:
            term = raw.strip()
            if not term:
                continue
            if len(term) > 64:
                raise ValueError("Each keyword must be at most 64 characters")
            cleaned.append(term)
        if not cleaned:
            raise ValueError("Provide at least one search keyword")
        if len(cleaned) > 20:
            raise ValueError("Provide at most 20 keywords")
        self.keywords = cleaned
        self.country = self.country.strip()
        return self


class AddMembersRequest(BaseModel):
    account_id: str = ""
    source_target: Optional[str] = None   # source channel/group (if scraping first)
    dest_target: str                       # destination channel/group
    usernames: Optional[List[str]] = None  # explicit list of usernames/IDs
    filter_bots: bool = True
    activity_filter: ActivityFilter = "all"
    limit: int = Field(default=10, ge=1, le=10)                        # max members to add per run
    delay: float = Field(default=35.0, ge=35, le=3600, allow_inf_nan=False)                    # seconds between each add (Telegram rate limit)
    approval_id: Optional[str] = None      # server-side approval reference
    rotation_mode: Literal["single", "round_robin"] = "single"
    account_ids: List[str] = Field(default_factory=list)  # selected accounts for rotation

    @model_validator(mode="after")
    def _validate_member_accounts(self):
        if self.rotation_mode == "single":
            if not self.account_id.strip() or self.account_ids:
                raise ValueError("Single mode requires one account_id and no rotation accounts")
        elif (not self.account_ids or any(not a.strip() for a in self.account_ids)
              or len(set(self.account_ids)) != len(self.account_ids) or self.account_id):
            raise ValueError("Rotation requires unique account_ids and no account_id")
        return self

    def selected_accounts(self):
        return self.account_ids if self.rotation_mode == "round_robin" else [self.account_id]


class BulkSendRequest(BaseModel):
    account_id: str = ""
    message: str = Field(default="", max_length=4096)
    messages: dict[str, str] = Field(default_factory=dict)
    filter_bots: bool = True
    targets: List[str] = Field(min_length=1)  # finite, explicit list; no app batch cap
    delay: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    template_id: Optional[str] = None
    approval_id: Optional[str] = None      # server-side approval reference
    activity_filter: ActivityFilter = "all"
    rotation_mode: Literal["single", "round_robin"] = "single"
    account_ids: List[str] = Field(default_factory=list)
    per_account_limit: int = Field(default=0, ge=0, strict=True)  # 0 = unlimited
    consent_confirmed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_accounts(self):
        normalized = {}
        targets = {t.strip().lower().lstrip("@") for t in self.targets}
        for key, value in self.messages.items():
            recipient = key.strip().lower().lstrip("@")
            if not recipient or recipient not in targets or not value.strip() or len(value) > 4096:
                raise ValueError("CSV messages require a listed recipient and 1–4096 characters")
            if recipient in normalized and normalized[recipient] != value:
                raise ValueError("Conflicting messages for the same recipient")
            normalized[recipient] = value
        self.messages = normalized
        if not self.message.strip() and not self.template_id and any(t not in normalized for t in targets):
            raise ValueError("A message is required for every recipient")
        if self.template_id and self.messages:
            raise ValueError("Choose either template or CSV messages")
        if self.rotation_mode == "single":
            if not self.account_id.strip() or self.account_ids:
                raise ValueError("Single mode requires one account_id and no rotation accounts")
        elif (not self.account_ids or any(not a.strip() for a in self.account_ids)
              or len(set(self.account_ids)) != len(self.account_ids) or self.account_id):
            raise ValueError("Rotation requires unique account_ids and no account_id")
        return self

    def selected_accounts(self):
        return self.account_ids if self.rotation_mode == "round_robin" else [self.account_id]


# ---- Auto Join: parse a channel/group link or username into a join target ----
# A private invite link (t.me/+HASH or t.me/joinchat/HASH) is joined with an
# invite hash; a public channel/group (t.me/<username>, @<username> or a bare
# username) is joined by resolving its entity.
_TME_INVITE_RE = _re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]{6,})", _re.I)
_TME_PUBLIC_RE = _re.compile(
    r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?$", _re.I)
_BARE_INVITE_RE = _re.compile(r"^\+([A-Za-z0-9_-]{6,})$")
_BARE_USERNAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _parse_join_target(raw: str):
    """Return ("invite", hash) or ("public", username), or None if unparseable."""
    s = (raw or "").strip()
    if not s:
        return None
    m = _TME_INVITE_RE.match(s) or _BARE_INVITE_RE.match(s)
    if m:
        return ("invite", m.group(1))
    m = _TME_PUBLIC_RE.match(s)
    if m and m.group(1).lower() not in _TME_RESERVED:
        return ("public", m.group(1))
    bare = s.lstrip("@")
    if _BARE_USERNAME_RE.match(bare) and bare.lower() not in _TME_RESERVED:
        return ("public", bare)
    return None


class JoinChannelsRequest(BaseModel):
    """Auto Join: an account joins each provided channel/group link itself.

    Joining is a self-action (no messages, no contact with other users), so it
    needs rate-limit safety and optional multi-account rotation, but not the
    recipient-consent gate that member-add and bulk-send require.
    """
    account_id: str = ""
    urls: List[str] = Field(min_length=1)   # channel/group links or usernames
    delay: float = Field(default=45.0, ge=30, le=3600, allow_inf_nan=False)  # seconds between joins
    limit: int = Field(default=20, ge=1, le=50)  # max joins per run
    rotation_mode: Literal["single", "round_robin"] = "single"
    account_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_join(self):
        cleaned = list(dict.fromkeys(u.strip() for u in self.urls if u.strip()))
        if not cleaned:
            raise ValueError("Provide at least one channel or group link")
        self.urls = cleaned
        if self.rotation_mode == "single":
            if not self.account_id.strip() or self.account_ids:
                raise ValueError("Single mode requires one account_id and no rotation accounts")
        elif (not self.account_ids or any(not a.strip() for a in self.account_ids)
              or len(set(self.account_ids)) != len(self.account_ids) or self.account_id):
            raise ValueError("Rotation requires unique account_ids and no account_id")
        return self

    def selected_accounts(self):
        return self.account_ids if self.rotation_mode == "round_robin" else [self.account_id]


def _enforce_outreach(action: str, subjects: List[str], account_id: str, approval_id: Optional[str]):
    decisions = outreach_policy.authorize_many(
        action=action,
        subjects=subjects,
        account_ids=[account_id],
        approval_id=approval_id,
    )
    denied = next((decision for decision in decisions if not decision.allowed and decision.reason != "consent_missing_or_invalid"), None)
    if denied:
        log_manager.add_log("OutreachPolicy", account_id, denied.audit_summary(), "warning")
        raise HTTPException(status_code=403, detail=f"Outreach denied: {denied.reason}")


# ============ 1. Member Scraper ============

# Paths that are Telegram app features rather than public channel/group names.
_TME_RESERVED = {"s", "share", "joinchat", "addstickers", "addtheme", "addemoji", "addlist",
                 "proxy", "socks", "bg", "login", "iv", "setlanguage", "confirmphone", "c", "contact"}

_TARGET_USERNAME_RE = _re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TARGET_URL_RE = _re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})"
    r"(?:/\d+)?/?(?:\?.*)?$",
    _re.I,
)
_TARGET_ID_RE = _re.compile(r"^-?\d+$")
_PRIVATE_LINK_RE = _re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/|c/)", _re.I)
_TARGET_HELP = ("Use a public @username, a t.me/username link, or a numeric channel ID. "
                "Private invite links must be joined before scraping.")


def _normalise_scrape_target(raw: str):
    """Return an @username or integer ID Telethon can resolve, else None.

    Public links may carry a public preview prefix (t.me/s/name) or point at a
    specific post (t.me/name/123); Telegram resolves both to the same channel.
    """
    target = (raw or "").strip()
    if not target:
        return None
    if _PRIVATE_LINK_RE.match(target):
        return None
    url_match = _TARGET_URL_RE.match(target)
    if url_match and url_match.group(1).lower() not in _TME_RESERVED:
        return "@" + url_match.group(1)
    if _TARGET_ID_RE.match(target):
        return int(target)
    username_match = _TARGET_USERNAME_RE.match(target)
    if username_match and username_match.group(1).lower() not in _TME_RESERVED:
        return "@" + username_match.group(1)
    return None


async def _resolve_target_entity(client, target):
    """Resolve a username, ID, or peer ID, falling back to the account's dialogs.

    Numeric IDs of channels the account is a member of are not always cached in
    a fresh StringSession; scanning dialogs recovers those without asking the
    operator for a public username.
    """
    try:
        return await client.get_entity(target)
    except (ValueError, KeyError):
        if not isinstance(target, int):
            raise
        async for dialog in client.iter_dialogs():
            if utils.get_peer_id(dialog.entity) == target:
                return dialog.entity
        raise


@telegram_ops_router.post("/api/scraper/members")
async def scrape_members(request: ScrapeRequest):
    """
    Scrape members from a Telegram channel or group.
    Returns list of {id, username, first_name, last_name, is_bot}.
    """
    try:
        target = _normalise_scrape_target(request.target)
        if target is None:
            raise HTTPException(status_code=400, detail=f"Invalid scrape target. {_TARGET_HELP}")

        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400,
                                detail=_client_unavailable_detail(request.account_id))

        try:
            entity = await _resolve_target_entity(client, target)
        except (ValueError, KeyError):
            raise HTTPException(
                status_code=400,
                detail=f"Could not find {request.target}. Check the username or ID and that "
                       "the selected account can access it.",
            ) from None

        participants = await client.get_participants(entity, limit=request.limit)

        admin_ids = set()
        if request.admin_filter == "exclude":
            try:
                admins = await client(functions.channels.GetParticipantsRequest(
                    channel=entity,
                    filter=types.ChannelParticipantsAdmins(),
                    offset=0,
                    limit=100,
                    hash=0,
                ))
                admin_ids = {user.id for user in admins.users}
            except Exception:
                admin_ids = set()

        members = []
        now = datetime.now(timezone.utc)
        for p in participants:
            if request.filter_bots and p.bot:
                continue
            if p.id in admin_ids:
                continue
            if not matches_activity(p, request.activity_filter, now):
                continue
            members.append({
                "id": p.id,
                "username": p.username or "",
                "first_name": p.first_name or "",
                "last_name": p.last_name or "",
                "is_bot": p.bot,
                **activity_details(p),
            })

        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scraped {len(members)} members from {request.target}",
            "success",
        )

        return {
            "success": True,
            "target": request.target,
            "total": len(members),
            "scanned": len(participants),
            "excluded": len(participants) - len(members),
            "activity_filter": request.activity_filter,
            "members": members,
        }

    except HTTPException:
        raise
    except errors.FloodWaitError as e:
        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scrape rate-limited for {request.target}: retry in {e.seconds}s", "warning",
        )
        raise HTTPException(
            status_code=429,
            detail=f"Telegram rate limit reached — retry in {e.seconds}s",
        ) from None
    except Exception as e:
        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scrape failed for {request.target}: {type(e).__name__}", "error",
        )
        raise HTTPException(
            status_code=500,
            detail="Scrape failed. Telegram did not complete the request — try again or "
                   "check the account connection.",
        ) from None


class GroupListRequest(BaseModel):
    account_id: str


@telegram_ops_router.post("/api/channels/groups")
async def list_account_groups(request: GroupListRequest):
    """Groups and channels the account belongs to, for the target pickers."""
    account_id = (request.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="Select an account first")
    client = await account_manager.get_client(account_id)
    if not client:
        raise HTTPException(status_code=400, detail=_client_unavailable_detail(account_id))
    try:
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=None), 45)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Telegram did not return the group list in time. Try again.",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Could not load groups from Telegram. Check the account connection.",
        ) from None

    groups = []
    for dialog in dialogs:
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        username = getattr(entity, "username", None) or ""
        members = getattr(entity, "participants_count", None)
        groups.append({
            "peer_id": utils.get_peer_id(entity),
            "name": dialog.name or (f"@{username}" if username else str(dialog.id)),
            "username": username,
            "type": "group" if dialog.is_group else "channel",
            "members": members if isinstance(members, int) else None,
        })
    groups.sort(key=lambda g: (g["type"], g["name"].lower()))
    log_manager.add_log("Groups", account_id, f"Detected {len(groups)} groups and channels", "info")
    return {"success": True, "groups": groups}


# Public t.me/web discovery (used when no Telegram account is selected).
_TME_USERNAME_RE = _re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z]\w{3,31})\b", _re.I)
_OG_TITLE_RE = _re.compile(r'<meta property="og:title" content="([^"]*)"', _re.I)
_EXTRA_RE = _re.compile(r'<div class="tgme_page_extra">([^<]*)</div>', _re.I)
_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _parse_tme_preview(html: str, username: str):
    """Parse a public t.me/<username> preview page.

    Returns {username,title,type,members} for a public channel/supergroup, or
    None for a user, bot, private, or invalid page (no subscribers/members).
    """
    extra_m = _EXTRA_RE.search(html)
    extra = (extra_m.group(1) if extra_m else "").lower()
    if "subscriber" in extra:
        kind = "channel"
    elif "member" in extra:
        kind = "group"
    else:
        return None
    title_m = _OG_TITLE_RE.search(html)
    title = unescape(title_m.group(1)) if title_m else username
    members = None
    head = extra.split("subscriber")[0].split("member")[0]
    digits = _re.sub(r"[^\d]", "", head)
    if digits:
        try:
            members = int(digits)
        except ValueError:
            members = None
    return {"username": username, "title": title, "type": kind, "members": members}


async def _ddg_usernames(session, keyword, want):
    """Find candidate t.me usernames via a credential-free web search."""
    usernames: list = []
    try:
        async with session.post("https://lite.duckduckgo.com/lite/",
                                data={"q": f"site:t.me {keyword}"},
                                timeout=aiohttp.ClientTimeout(total=12)) as resp:
            html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return usernames
    for match in _TME_USERNAME_RE.finditer(html):
        candidate = match.group(1)
        if candidate.lower() in _TME_RESERVED or candidate in usernames:
            continue
        usernames.append(candidate)
        if len(usernames) >= want:
            break
    return usernames


async def _tme_preview(session, username):
    try:
        async with session.get(f"https://t.me/{username}",
                               timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    return _parse_tme_preview(html, username)


# TGStat directory API — reliable account-free search (keyword over title and
# description, native country filter). Requires a free TGSTAT_TOKEN.
_TGSTAT_SEARCH_URL = "https://api.tgstat.ru/channels/search"


def _tgstat_peer_type(group_type):
    return {"channel": "channel", "group": "chat"}.get(group_type, "all")


def _parse_tgstat_item(item, fallback_type):
    """Normalise one TGStat search item to our result shape (defensive)."""
    if not isinstance(item, dict):
        return None
    username = str(item.get("username") or "").lstrip("@").strip()
    if not username:
        match = _TME_USERNAME_RE.search(str(item.get("link") or item.get("url") or ""))
        username = match.group(1) if match else ""
    if not username or username.lower() in _TME_RESERVED:
        return None
    raw_type = str(item.get("type") or item.get("peer_type") or fallback_type or "").lower()
    kind = "group" if raw_type in ("chat", "group", "supergroup", "megagroup") else "channel"
    members = item.get("participants_count")
    if not isinstance(members, int) or isinstance(members, bool):
        members = None
    return {
        "id": username,
        "title": str(item.get("title") or username),
        "username": username,
        "url": f"https://t.me/{username}",
        "type": kind,
        "members": members,
        "verified": bool(item.get("verified") or item.get("is_verified")),
        "about": str(item.get("about") or ""),
    }


async def _tgstat_search(session, token, keyword, peer_type, country_code, limit):
    params = {"token": token, "q": keyword, "search_by_description": "1",
              "peer_type": peer_type, "limit": str(min(max(limit, 1), 100))}
    if country_code:
        params["country"] = country_code
    try:
        async with session.get(_TGSTAT_SEARCH_URL, params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("status") != "ok":
        return []
    response = data.get("response")
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        return []
    return [info for info in (_parse_tgstat_item(it, peer_type) for it in items) if info]


async def _scrape_channels_tgstat(request: ChannelScrapeRequest, token):
    """Account-free discovery via the TGStat directory API.

    Matches the keyword over channel/chat titles AND descriptions, with TGStat's
    native country filter when a 2-letter country code is given; a free-text
    country narrows client-side on title/description/username.
    """
    peer_type = _tgstat_peer_type(request.group_type)
    country = request.country.strip()
    country_code = country if len(country) == 2 and country.isalpha() else ""
    text_filter = "" if country_code else country.lower()
    seen: set = set()
    results: list = []
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        # Search every keyword concurrently; a slow query must not push the
        # whole request past the dashboard's 120 s timeout (20 keywords x 15 s
        # serially would).
        semaphore = asyncio.Semaphore(5)

        async def search(keyword):
            async with semaphore:
                return await _tgstat_search(
                    session, token, keyword, peer_type, country_code, request.limit)

        per_keyword = await asyncio.gather(*(search(k) for k in request.keywords))
        for keyword, infos in zip(request.keywords, per_keyword):
            if len(results) >= request.limit:
                break
            for info in infos:
                key = info["username"].lower()
                if key in seen:
                    continue
                if request.group_type != "all" and info["type"] != request.group_type:
                    continue
                if text_filter and text_filter not in (
                        info["title"] + " " + info["about"] + " " + info["username"]).lower():
                    continue
                seen.add(key)
                info["matched_keyword"] = keyword
                info.pop("about", None)
                results.append(info)
                if len(results) >= request.limit:
                    break
    log_manager.add_log(
        "ChannelScraper", "tgstat",
        f"Found {len(results)} channels/groups (TGStat) for {len(request.keywords)} keyword(s)", "success",
    )
    return {"success": True, "total": len(results), "results": results, "mode": "tgstat"}


async def _scrape_channels_public(request: ChannelScrapeRequest):
    """Account-free discovery. Uses the TGStat directory API when TGSTAT_TOKEN is
    configured (reliable, searches title + description, native country filter);
    otherwise falls back to best-effort public web discovery. The token comes
    from the TGSTAT_TOKEN env var or the operations_shared.app_settings store."""
    token = await get_setting("tgstat_token", "TGSTAT_TOKEN")
    if token:
        return await _scrape_channels_tgstat(request, token)
    return await _scrape_channels_web(request)


_PUBLIC_PREVIEW_BUDGET = 45.0  # seconds; keeps the fallback inside the client timeout


async def _scrape_channels_web(request: ChannelScrapeRequest):
    """Account-free fallback: public web search + public t.me preview pages.

    No credentials. Results depend on the public sources being reachable and not
    rate-limiting the server, so this can legitimately return nothing; configure
    TGSTAT_TOKEN for reliable account-free search.
    """
    preview_budget = _PUBLIC_PREVIEW_BUDGET
    country = request.country.lower()
    want_candidates = min(request.limit * 3, 180)
    seen: set = set()
    candidates: list = []
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        discover_semaphore = asyncio.Semaphore(4)

        async def discover(keyword):
            async with discover_semaphore:
                return await _ddg_usernames(session, keyword, want_candidates)

        discovered = await asyncio.gather(*(discover(k) for k in request.keywords))
        for keyword, usernames in zip(request.keywords, discovered):
            if len(candidates) >= want_candidates:
                break
            for username in usernames:
                key = username.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((username, keyword))
                if len(candidates) >= want_candidates:
                    break
        semaphore = asyncio.Semaphore(8)

        async def enrich(username, keyword):
            async with semaphore:
                info = await _tme_preview(session, username)
                if info:
                    info["matched_keyword"] = keyword
                return info

        tasks = [asyncio.create_task(enrich(u, k)) for u, k in candidates]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=preview_budget)
        else:
            done, pending = set(), set()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        enriched: list = []
        for task in tasks:
            if task in done and not task.cancelled() and task.exception() is None:
                enriched.append(task.result())
            else:
                enriched.append(None)

    results: list = []
    for info in enriched:
        if not info:
            continue
        if request.group_type != "all" and info["type"] != request.group_type:
            continue
        if country and country not in info["title"].lower() and country not in info["username"].lower():
            continue
        results.append({
            "id": info["username"],
            "title": info["title"],
            "username": info["username"],
            "url": f"https://t.me/{info['username']}",
            "type": info["type"],
            "members": info["members"],
            "verified": False,
            "matched_keyword": info["matched_keyword"],
        })
        if len(results) >= request.limit:
            break

    log_manager.add_log(
        "ChannelScraper", "public",
        f"Found {len(results)} channels/groups (account-free) for {len(request.keywords)} keyword(s)", "success",
    )
    return {"success": True, "total": len(results), "results": results, "mode": "public"}


@telegram_ops_router.post("/api/scraper/channels")
async def scrape_channels(request: ChannelScrapeRequest):
    """Discover public Telegram channels/groups by keyword.

    With a selected account, uses Telegram's authenticated directory search
    (contacts.SearchRequest). With no account, falls back to account-free public
    discovery (web search + public t.me preview pages). Either way returns only
    public channels/supergroups with a t.me URL, de-duplicated. Telegram exposes
    no channel country, so `country` is a case-insensitive filter on the
    title/username, not a geographic lookup. Read-only: no messages, no joins.
    """
    account_id = request.account_id.strip()
    if not account_id:
        return await _scrape_channels_public(request)

    client = await account_manager.get_client(account_id)
    if not client:
        raise HTTPException(status_code=400, detail=_client_unavailable_detail(account_id))

    country = request.country.lower()
    seen: set = set()
    results: list = []
    for keyword in request.keywords:
        if len(results) >= request.limit:
            break
        try:
            found = await client(functions.contacts.SearchRequest(q=keyword, limit=min(request.limit, 100)))
        except errors.FloodWaitError as exc:
            raise HTTPException(status_code=429,
                                detail=f"Telegram rate limit reached — retry in {exc.seconds}s") from None
        except errors.RPCError:
            # A single bad/blocked query must not fail the whole search.
            continue
        for chat in getattr(found, "chats", []):
            username = getattr(chat, "username", None)
            chat_id = getattr(chat, "id", None)
            if not username or chat_id in seen:
                continue  # only public, shareable, de-duplicated entries
            if getattr(chat, "broadcast", False):
                kind = "channel"
            elif getattr(chat, "megagroup", False) or isinstance(chat, types.Chat):
                kind = "group"
            else:
                continue
            if request.group_type != "all" and kind != request.group_type:
                continue
            title = getattr(chat, "title", "") or ""
            if country and country not in title.lower() and country not in username.lower():
                continue
            seen.add(chat_id)
            results.append({
                "id": chat_id,
                "title": title,
                "username": username,
                "url": f"https://t.me/{username}",
                "type": kind,
                "members": getattr(chat, "participants_count", None),
                "verified": bool(getattr(chat, "verified", False)),
                "matched_keyword": keyword,
            })
            if len(results) >= request.limit:
                break

    log_manager.add_log(
        "ChannelScraper", account_id,
        f"Found {len(results)} channels/groups for {len(request.keywords)} keyword(s)", "success",
    )
    return {"success": True, "total": len(results), "results": results, "mode": "account"}


# ============ 2. Member Adder ============

@telegram_ops_router.post("/api/members/add")
async def add_members(request: AddMembersRequest):
    """
    Add members to a channel or group.
    Only accepts explicit usernames with current consent. Scraped additions are denied.
    """
    try:
        if request.source_target:
            log_manager.add_log(
                "OutreachPolicy", request.account_id,
                "outreach_policy deny action=member_add reason=scraped_source_forbidden",
                "warning",
            )
            raise HTTPException(
                status_code=403,
                detail="Outreach denied: scraped_source_forbidden",
            )
        if request.limit > 10 or request.delay < 35:
            raise HTTPException(status_code=400, detail="Member-add safety bounds exceeded")
        if not request.usernames:
            raise HTTPException(status_code=400, detail="Explicit usernames are required")

        user_list = list(dict.fromkeys(u.strip().lower().lstrip("@") for u in request.usernames))[:request.limit]
        if not all(user_list):
            raise HTTPException(status_code=400, detail="Recipient cannot be empty")
        try:
            outreach_policy.validate_new_member_approval(request.approval_id)
        except OutreachDenied as exc:
            raise HTTPException(status_code=403, detail=f"Outreach denied: {exc}") from None
        _enforce_outreach("member_add", user_list, request.account_id, request.approval_id)

        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400,
                                detail=_client_unavailable_detail(request.account_id))

        if not user_list:
            return {"success": True, "added": 0, "failed": 0, "results": [], "message": "No users to add"}

        dest_entity = await client.get_entity(request.dest_target)

        results = []
        added = 0
        failed = 0
        skipped = 0
        stopped_reason = None
        peers_seen = set()

        for i, username in enumerate(user_list):
            try:
                user_entity = await client.get_entity(int(username) if username.isdigit() else username)
                peer_id = utils.get_peer_id(user_entity)
                reason = ("bot_excluded" if request.filter_bots and getattr(user_entity, "bot", False)
                          else "duplicate_recipient" if peer_id in peers_seen
                          else "activity_not_matched" if not matches_activity(user_entity, request.activity_filter)
                          else None)
                peers_seen.add(peer_id)
                if reason:
                    results.append({"username": username, "success": False, "skipped": reason})
                    skipped += 1
                    continue
                await dispatch_outreach(action="member_add", subject=utils.get_peer_id(user_entity),
                    account_id=request.account_id, approval_id=request.approval_id, delay=request.delay,
                    send=lambda: client(
                    functions.channels.InviteToChannelRequest(
                        channel=dest_entity,
                        users=[user_entity],
                    )
                ))
                results.append({"username": username, "success": True})
                added += 1
                log_manager.add_log(
                    "MemberAdder", request.account_id,
                    "Approved member addition completed", "success",
                )
            except Exception as e:
                error_msg = str(e) if isinstance(e, OutreachDenied) else type(e).__name__
                record_provider_restriction(request.account_id, e)
                results.append({"username": username, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "MemberAdder", request.account_id,
                    f"Approved member addition failed: {type(e).__name__}", "error",
                )
                if isinstance(e, (errors.FloodWaitError, errors.PeerFloodError,
                                  errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                                  errors.FrozenMethodInvalidError, OutreachDenied)):
                    stopped_reason = error_msg
                    break

            # Rate-limit delay (except after the last one)
            if i < len(user_list) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": failed == 0 and stopped_reason is None,
            "skipped": skipped,
            "pending": len(user_list) - len(results),
            "stopped_reason": stopped_reason,
            "dest_target": request.dest_target,
            "added": added,
            "failed": failed,
            "total": len(user_list),
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        log_manager.add_log(
            "MemberAdder", request.account_id,
            f"Add members failed: {type(e).__name__}", "error",
        )
        raise HTTPException(status_code=500, detail=type(e).__name__)


# ============ 3. Bulk Personal Message Sender ============

@telegram_ops_router.post("/api/bulk/send-personal")
async def bulk_send_personal(request: BulkSendRequest, http_request: Request = None):
    """
    Send once per consenting recipient using one account or an explicit pool.
    Rotation never retries a recipient on another account after a restriction.
    """
    try:
        if not request.targets:
            raise HTTPException(status_code=400, detail="At least one target is required")
        targets = list(dict.fromkeys(t.strip().lower().lstrip("@") for t in request.targets))
        if not all(targets):
            raise HTTPException(status_code=400, detail="Recipient cannot be empty")
        account_ids = request.selected_accounts()
        # Rotation: a recipient may be sent through whichever selected account is
        # available, so one account hitting a Telegram limit never stops the run.
        rotate = request.rotation_mode == "round_robin" and len(account_ids) > 1
        if request.per_account_limit and len(targets) > len(account_ids) * request.per_account_limit:
            raise HTTPException(status_code=400, detail="Per-account run limit exceeded")
        operator = getattr(getattr(http_request, "state", None), "user", None)
        if request.consent_confirmed:
            if not operator or not operator.id:
                raise HTTPException(status_code=403, detail="Authenticated operator required")
            try:
                if rotate:
                    # The operator confirmed consent for the whole selected set, so each
                    # recipient may be sent through any of the selected accounts.
                    outreach_policy.record_personal_consents(targets, account_ids, operator.id)
                else:
                    for index, account_id in enumerate(account_ids):
                        outreach_policy.record_personal_consents(targets[index::len(account_ids)], account_id, operator.id)
            except OutreachDenied as error:
                raise HTTPException(status_code=403, detail=str(error)) from None
        for account_id in account_ids:
            _enforce_outreach("personal_send", targets, account_id, request.approval_id)
        clients = {}
        for account_id in account_ids:
            clients[account_id] = await account_manager.get_client(account_id)
            if not clients[account_id]:
                reason = account_manager.connection_errors.get(account_id, "Check the connection from Accounts.")
                raise HTTPException(status_code=400, detail=f"Selected account unavailable; no messages sent. {reason}")

        message_text = request.message

        # If a template_id is provided, render it (fall back to raw message)
        if request.template_id:
            try:
                from template_manager import template_manager
                rendered = template_manager.render_template(
                    request.template_id,
                    time=datetime.now().strftime("%H:%M"),
                    date=datetime.now().strftime("%Y-%m-%d"),
                )
                if rendered:
                    message_text = rendered
            except Exception:
                pass  # fall back to request.message

        if (not message_text.strip() and any(t not in request.messages for t in targets)) or len(message_text) > 4096:
            raise HTTPException(status_code=400, detail="A valid message or renderable template is required")

        results = []
        sent = 0
        failed = 0
        skipped = len(request.targets) - len(targets)
        peers_seen = set()
        stopped_reason = None
        limited = set()  # accounts dropped from the pool after a provider limit this run
        account_limit_errors = (errors.FloodWaitError, errors.PeerFloodError,
                                errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                                errors.FrozenMethodInvalidError)

        for i, target in enumerate(targets):
            if rotate:
                # Try the round-robin account first, then the other selected accounts,
                # skipping any already limited this run.
                ordered = [account_ids[(i + k) % len(account_ids)] for k in range(len(account_ids))]
                candidates = [a for a in ordered if a not in limited]
                if not candidates:
                    stopped_reason = "all_accounts_limited"
                    break
            else:
                candidates = [account_ids[i % len(account_ids)]]
            outcome = None
            resolved_peer = None
            for account_id in candidates:
                client = clients[account_id]
                try:
                    entity = await client.get_entity(int(target) if target.isdigit() else target)
                    resolved_peer = utils.get_peer_id(entity)
                    if resolved_peer in peers_seen:
                        outcome = ("skipped", "duplicate_recipient", account_id)
                        break
                    if request.filter_bots and getattr(entity, "bot", False):
                        outcome = ("skipped", "bot_excluded", account_id)
                        break
                    if not matches_activity(entity, request.activity_filter):
                        outcome = ("skipped", "activity_not_matched", account_id)
                        break
                    if request.consent_confirmed:
                        outreach_policy.record_personal_consents([resolved_peer], account_id, operator.id)
                    await dispatch_outreach(action="personal_send", subject=resolved_peer,
                        account_id=account_id, approval_id=request.approval_id, delay=request.delay,
                        send=lambda c=client, e=entity: c.send_message(e, request.messages.get(target, message_text)))
                    outcome = ("sent", "", account_id)
                    break
                except account_limit_errors as exc:
                    # Account-wide limit: drop it from the pool and switch to the next
                    # available account for this recipient.
                    record_provider_restriction(account_id, exc)
                    if rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", type(exc).__name__, account_id)
                    break
                except OutreachDenied as exc:
                    record_provider_restriction(account_id, exc)
                    if str(exc) == "account_cooldown" and rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", str(exc), account_id)
                    break
                except Exception as exc:
                    record_provider_restriction(account_id, exc)
                    outcome = ("failed", type(exc).__name__, account_id)
                    break
            if outcome is None:
                # Every remaining account was limited while trying this recipient.
                stopped_reason = "all_accounts_limited"
                break
            kind, detail, account_id = outcome
            if resolved_peer is not None and detail != "duplicate_recipient":
                peers_seen.add(resolved_peer)
            if kind == "sent":
                results.append({"target": target, "account_id": account_id, "success": True})
                sent += 1
                stats_tracker.record_message_sent(account_id)
                log_manager.add_log("BulkSend", account_id, "Personal message sent", "success")
            elif kind == "skipped":
                results.append({"target": target, "account_id": account_id, "success": False, "skipped": detail})
                skipped += 1
            else:  # failed or stop
                results.append({"target": target, "account_id": account_id, "success": False, "error": detail})
                failed += 1
                log_manager.add_log("BulkSend", account_id, f"Personal send failed: {detail}", "error")
                if kind == "stop":
                    stopped_reason = detail
                    break

            # Delay between sends (except last)
            if i < len(targets) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": failed == 0 and stopped_reason is None,
            "account_id": request.account_id,
            "account_ids": account_ids,
            "rotation_mode": request.rotation_mode,
            "activity_filter": request.activity_filter,
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "pending": len(targets) - len(results),
            "stopped_reason": stopped_reason,
            "total": len(request.targets),
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        log_manager.add_log(
            "BulkSend", request.account_id,
            f"Bulk send failed: {type(e).__name__}", "error",
        )
        raise HTTPException(status_code=500, detail=type(e).__name__)


# ============ 4. Auto Join ============

@telegram_ops_router.post("/api/channels/join")
async def join_channels(request: JoinChannelsRequest):
    """
    Join channels/groups from the provided links using one account or an
    explicit pool. Rotation rotates an account out when it hits a Telegram
    join limit; the remaining accounts carry on. Joining is a self-action,
    so no recipient consent is involved.
    """
    try:
        account_ids = request.selected_accounts()
        rotate = request.rotation_mode == "round_robin" and len(account_ids) > 1

        clients = {}
        for account_id in account_ids:
            clients[account_id] = await account_manager.get_client(account_id)
            if not clients[account_id]:
                reason = account_manager.connection_errors.get(account_id, "Check the connection from Accounts.")
                raise HTTPException(status_code=400, detail=f"Selected account unavailable; no joins performed. {reason}")

        links = request.urls[:request.limit]
        account_limit_errors = (errors.FloodWaitError, errors.ChannelsTooMuchError,
                                errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                                errors.FrozenMethodInvalidError)
        # Already-joined / invalid-link errors are per-link, never account-wide.
        invalid_link_errors = (errors.InviteHashExpiredError, errors.InviteHashInvalidError,
                               errors.InviteRequestSentError, errors.UsernameInvalidError,
                               errors.UsernameNotOccupiedError)

        async def _attempt(client, kind, value):
            if kind == "invite":
                await client(functions.messages.ImportChatInviteRequest(value))
            else:
                entity = await client.get_entity(value)
                await client(functions.channels.JoinChannelRequest(entity))

        results = []
        joined = failed = skipped = 0
        stopped_reason = None
        limited = set()  # accounts dropped from the pool after a join limit this run

        for i, raw in enumerate(links):
            parsed = _parse_join_target(raw)
            if parsed is None:
                results.append({"url": raw, "success": False, "skipped": "invalid_link"})
                skipped += 1
                continue
            kind, value = parsed

            if rotate:
                ordered = [account_ids[(i + k) % len(account_ids)] for k in range(len(account_ids))]
                candidates = [a for a in ordered if a not in limited]
                if not candidates:
                    stopped_reason = "all_accounts_limited"
                    break
            else:
                candidates = [account_ids[i % len(account_ids)]]

            outcome = None
            for account_id in candidates:
                client = clients[account_id]
                try:
                    await _attempt(client, kind, value)
                    outcome = ("joined", "", account_id)
                    break
                except errors.UserAlreadyParticipantError:
                    outcome = ("already", "", account_id)
                    break
                except account_limit_errors as exc:
                    record_provider_restriction(account_id, exc)
                    if rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", type(exc).__name__, account_id)
                    break
                except invalid_link_errors as exc:
                    outcome = ("failed", type(exc).__name__, account_id)
                    break
                except Exception as exc:
                    record_provider_restriction(account_id, exc)
                    outcome = ("failed", type(exc).__name__, account_id)
                    break

            if outcome is None:
                stopped_reason = "all_accounts_limited"
                break
            state, detail, account_id = outcome
            if state in ("joined", "already"):
                results.append({"url": raw, "account_id": account_id, "success": True,
                                "already": state == "already"})
                joined += 1
                log_manager.add_log("AutoJoin", account_id,
                                    f"Joined {raw}" if state == "joined" else f"Already a member of {raw}",
                                    "success")
            else:  # failed or stop
                results.append({"url": raw, "account_id": account_id, "success": False, "error": detail})
                failed += 1
                log_manager.add_log("AutoJoin", account_id, f"Join failed for {raw}: {detail}", "error")
                if state == "stop":
                    stopped_reason = detail
                    break

            # Rate-limit delay between joins (except after the last link)
            if i < len(links) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": failed == 0 and stopped_reason is None,
            "account_id": request.account_id,
            "account_ids": account_ids,
            "rotation_mode": request.rotation_mode,
            "joined": joined,
            "failed": failed,
            "skipped": skipped,
            "pending": len(links) - len(results),
            "stopped_reason": stopped_reason,
            "total": len(request.urls),
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        log_manager.add_log(
            "AutoJoin", request.account_id,
            f"Auto join failed: {type(e).__name__}", "error",
        )
        raise HTTPException(status_code=500, detail=type(e).__name__)
