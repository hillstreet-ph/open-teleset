#!/usr/bin/env python3
"""
Telegram Operations — Scraper, Member Adder, Bulk Personal Sender

FastAPI APIRouter providing REST endpoints for:
1. POST /api/scraper/members      — scrape members from channel/group
2. POST /api/members/add          — add members to channel/group
3. POST /api/bulk/send-personal   — send personal messages to many users
"""
import asyncio
import base64
import json
import os
import re as _re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional, Literal
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from telethon import errors, functions, types, utils

import bulk_media
from open_teleset.app_settings import get_setting

from account_manager import account_manager
from log_manager import log_manager
from outreach_policy import (
    OutreachDenied,
    clear_account_block,
    clear_account_cooldowns,
    cooldown_scope,
    dispatch_outreach,
    outreach_policy,
    record_provider_restriction,
)
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
    limit: int = Field(default=500, ge=0, le=1_000_000, strict=True)  # 0 = every member the account can enumerate
    filter_bots: bool = True # exclude bots
    activity_filter: ActivityFilter = "all"
    admin_filter: Literal["include", "exclude"] = "include"


class ChannelScrapeRequest(BaseModel):
    account_id: str = ""   # optional: empty = account-free public discovery
    keywords: List[str] = Field(default_factory=list)      # directory search terms
    country: str = Field(default="", max_length=64)        # name or ISO code; server maps it
    language: str = Field(default="", max_length=32)       # optional language hint
    group_type: Literal["all", "group", "channel"] = "all"
    category: Literal["all", "casino"] = "all"   # topic filter over title/username/description
    min_members: int = Field(default=0, ge=0, le=10_000_000)  # 0 = no member filter
    limit: int = Field(default=300, ge=1, le=10000, strict=True)  # result ceiling across all keywords
    proxy_mode: Literal["none", "pool", "custom"] = "none"
    proxy_urls: List[str] = Field(default_factory=list)

    def apply_proxy_config(self):
        """Validate and clean request-scoped proxies.

        Called by the route, not by a pydantic validator: a validation error
        returns 422 with the whole request echoed back, which would copy a
        proxy password into the response body.
        """
        if self.proxy_mode != "custom":
            self.proxy_urls = []
            return
        if not self.proxy_urls:
            raise ValueError(
                "Add at least one proxy URL for Custom proxies, or choose another proxy mode."
            )
        if len(self.proxy_urls) > 50:
            raise ValueError("Custom proxies accept at most 50 URLs.")
        validated = []
        for index, raw in enumerate(self.proxy_urls):
            cleaned_url = _clean_proxy_url(raw)
            if not cleaned_url:
                raise ValueError(
                    f"Proxy URL #{index + 1} must be an http:// or https:// URL with a host."
                )
            validated.append(cleaned_url)
        self.proxy_urls = list(dict.fromkeys(validated))

    @model_validator(mode="after")
    def _normalise(self):
        cleaned = []
        seen = set()
        for raw in self.keywords:
            term = raw.strip()
            if not term:
                continue
            if len(term) > 64:
                raise ValueError("Each keyword must be at most 64 characters")
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(term)
        if not cleaned:
            raise ValueError("Provide at least one search keyword")
        self.keywords = cleaned
        self.country = self.country.strip()
        self.language = self.language.strip()
        return self


class AddMembersRequest(BaseModel):
    account_id: str = ""
    source_target: Optional[str] = None   # source channel/group (if scraping first)
    dest_target: str                       # destination channel/group
    usernames: Optional[List[str]] = None  # explicit list of usernames/IDs
    filter_bots: bool = True
    activity_filter: ActivityFilter = "all"
    limit: int = Field(default=10, ge=1, le=10)
    delay: float = Field(default=35.0, ge=35, le=3600, allow_inf_nan=False)  # seconds between each add (Telegram rate limit)
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
    media_ids: List[str] = Field(default_factory=list)  # operator-owned uploads

    @model_validator(mode="after")
    def validate_accounts(self):
        if len(self.media_ids) > bulk_media.MAX_ITEMS:
            raise ValueError(f"At most {bulk_media.MAX_ITEMS} attachments per send")
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
        if self.media_ids:
            # Telegram shows a caption under a file, capped at 1024 characters.
            if len(self.message) > 1024 or any(len(v) > 1024 for v in normalized.values()):
                raise ValueError("A caption is limited to 1024 characters when a file is attached")
        if (not self.media_ids and not self.message.strip() and not self.template_id
                and any(t not in normalized for t in targets)):
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
_TME_INVITE_RE = _re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:joinchat/|\+)([A-Za-z0-9_-]{6,})", _re.I)
_TME_PUBLIC_RE = _re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?(?:\?[^#]*)?$", _re.I)
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
    delay: float = Field(default=38.0, ge=30, le=3600, allow_inf_nan=False)  # seconds between joins
    limit: int = Field(default=0, ge=0, strict=True)  # 0 joins every link provided
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


RECENT_POSTER_SCAN_MAX = 10000


def scrape_member_notice(member_count=None):
    """Explain a degraded scrape without exposing Telegram internals."""
    count = f" Telegram reports {member_count:,} members." if member_count else ""
    return ("Telegram hides the full member list from non-admins for this target, so "
            "this lists the people who posted recently instead." + count)


async def collect_scrape_members(client, entity, limit):
    """Use visible message authors when Telegram hides the roster from this account.

    ``limit=0`` means every member the account can enumerate, so the roster call
    omits Telethon's limit (None lets the client page through the whole list).
    """
    try:
        return await client.get_participants(entity, limit=limit or None), "members", None
    except errors.ChatAdminRequiredError:
        pass

    member_count = None
    try:
        full = await client(functions.channels.GetFullChannelRequest(entity))
        member_count = getattr(full.full_chat, "participants_count", None)
        if not isinstance(member_count, int) or isinstance(member_count, bool):
            member_count = None
    except errors.ChatAdminRequiredError:
        pass

    users, seen = [], set()
    scan_limit = RECENT_POSTER_SCAN_MAX if not limit else min(
        max(limit * 8, 500), RECENT_POSTER_SCAN_MAX)
    async for message in client.iter_messages(entity, limit=scan_limit):
        sender = getattr(message, "sender", None)
        if sender is None:
            try:
                sender = await message.get_sender()
            except ValueError:
                continue
        if not isinstance(sender, types.User) or sender.id in seen:
            continue
        seen.add(sender.id)
        users.append(sender)
        if limit and len(users) >= limit:
            break
    return users, "recent_posters", member_count


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

        participants, source, member_count = await collect_scrape_members(
            client, entity, request.limit)
        limited = source == "recent_posters"

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
            except errors.ChatAdminRequiredError:
                raise HTTPException(
                    status_code=400,
                    detail="Telegram hides the admin list from this account. Select Include admins "
                           "to continue without admin exclusion.",
                ) from None

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
            (f"Scraped {len(members)} recent posters from {request.target} "
             "(Telegram hides the member list from non-admins)"
             if limited else
             f"Scraped {len(members)} members from {request.target}"),
            "success",
        )

        return {
            "success": True,
            "target": request.target,
            "total": len(members),
            "scanned": len(participants),
            "excluded": len(participants) - len(members),
            "activity_filter": request.activity_filter,
            "source": source,
            "limited": limited,
            "member_count": member_count,
            "notice": scrape_member_notice(member_count) if limited else None,
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
    except errors.RPCError as e:
        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scrape rejected by Telegram for {request.target}: {type(e).__name__}", "warning",
        )
        raise HTTPException(
            status_code=400,
            detail=f"Telegram rejected the scrape: {type(e).__name__} — check the account connection and target.",
        ) from None
    except Exception as e:
        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scrape failed for {request.target}: {type(e).__name__}", "error",
        )
        raise HTTPException(
            status_code=500,
            detail="Scrape failed for an unexpected reason. Check the account connection and try again.",
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
_BING_WRAPPED_RE = _re.compile(r"[?&]u=a1([A-Za-z0-9_-]{8,})")
_OG_TITLE_RE = _re.compile(r'<meta property="og:title" content="([^"]*)"', _re.I)
_OG_DESC_RE = _re.compile(r'<meta property="og:description" content="([^"]*)"', _re.I)
_EXTRA_RE = _re.compile(r'<div class="tgme_page_extra">([^<]*)</div>', _re.I)
_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Country names an operator is likely to type, mapped to ISO 3166-1 alpha-2.
# The extra tokens are used only when a source cannot filter by country itself.
_COUNTRY_NAMES = {
    "PH": ("philippines", "pilipinas", "filipinas", "pinoy"),
    "ID": ("indonesia", "indonesian"),
    "IN": ("india", "indian"),
    "MY": ("malaysia", "malaysian"),
    "VN": ("vietnam", "vietnamese"),
    "TH": ("thailand", "thai"),
    "SG": ("singapore", "singaporean"),
    "KH": ("cambodia", "cambodian"),
    "BD": ("bangladesh", "bangladeshi"),
    "PK": ("pakistan", "pakistani"),
    "LK": ("sri lanka", "srilanka"),
    "NP": ("nepal", "nepali"),
    "AE": ("uae", "united arab emirates", "dubai"),
    "SA": ("saudi arabia", "saudi"),
    "TR": ("turkey", "turkish", "turkiye"),
    "RU": ("russia", "russian"),
    "UA": ("ukraine", "ukrainian"),
    "NG": ("nigeria", "nigerian"),
    "KE": ("kenya", "kenyan"),
    "GH": ("ghana", "ghanaian"),
    "ZA": ("south africa", "south african"),
    "EG": ("egypt", "egyptian"),
    "MA": ("morocco", "moroccan"),
    "BR": ("brazil", "brazilian", "brasil"),
    "MX": ("mexico", "mexican"),
    "AR": ("argentina", "argentinian"),
    "CO": ("colombia", "colombian"),
    "CL": ("chile", "chilean"),
    "PE": ("peru", "peruvian"),
    "VE": ("venezuela", "venezuelan"),
    "DO": ("dominican republic", "dominican"),
    "US": ("united states", "usa", "american"),
    "CA": ("canada", "canadian"),
    "GB": ("united kingdom", "uk", "great britain", "british", "england"),
    "DE": ("germany", "german"),
    "FR": ("france", "french"),
    "ES": ("spain", "spanish"),
    "IT": ("italy", "italian"),
    "PT": ("portugal", "portuguese"),
    "PL": ("poland", "polish"),
    "NL": ("netherlands", "dutch"),
    "RO": ("romania", "romanian"),
    "AU": ("australia", "australian"),
    "JP": ("japan", "japanese"),
    "KR": ("south korea", "korea", "korean"),
    "CN": ("china", "chinese"),
    "TW": ("taiwan", "taiwanese"),
    "HK": ("hong kong", "hongkong"),
    "IR": ("iran", "iranian", "persian"),
    "IQ": ("iraq", "iraqi"),
    "ET": ("ethiopia", "ethiopian"),
    "TZ": ("tanzania", "tanzanian"),
    "UG": ("uganda", "ugandan"),
    "CM": ("cameroon", "cameroonian"),
}

# Language hints mapped to ISO 639-1 codes for the directory filter.
_LANGUAGE_NAMES = {
    "tagalog": "tl", "filipino": "tl", "pilipino": "tl",
    "english": "en", "spanish": "es", "indonesian": "id", "malay": "ms",
    "thai": "th", "vietnamese": "vi", "hindi": "hi", "bengali": "bn",
    "urdu": "ur", "arabic": "ar", "russian": "ru", "portuguese": "pt",
    "french": "fr", "german": "de", "italian": "it", "turkish": "tr",
    "korean": "ko", "japanese": "ja", "chinese": "zh", "nepali": "ne",
    "sinhala": "si", "khmer": "km", "burmese": "my", "persian": "fa",
    "farsi": "fa", "swahili": "sw", "hausa": "ha", "yoruba": "yo",
    "somali": "so", "amharic": "am", "ukrainian": "uk", "polish": "pl",
    "dutch": "nl", "romanian": "ro", "greek": "el", "hebrew": "he",
}


def resolve_country(value: str):
    """Return (ISO alpha-2 code or "", lowercase text tokens for fallback filtering)."""
    raw = (value or "").strip()
    if not raw:
        return "", []
    compact = _re.sub(r"[\s._-]+", " ", raw.lower()).strip()
    if len(compact) == 2 and compact.isalpha():
        if compact.upper() in _COUNTRY_NAMES:
            return compact.upper(), list(_COUNTRY_NAMES[compact.upper()])
    for iso, names in _COUNTRY_NAMES.items():
        if compact in names:
            return iso, list(names)
    return "", ([compact] if len(compact) >= 3 else [])


def resolve_language(value: str) -> str:
    """Return an ISO 639-1 code, or "" when the hint is unknown."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if len(raw) == 2 and raw.isalpha():
        return raw
    return _LANGUAGE_NAMES.get(raw, "")


def country_matches(haystack: str, tokens, iso_code: str = "") -> bool:
    """Text filter used where no native country field exists.

    Full name aliases match as substrings; a two-letter ISO code only matches as
    a standalone word so `PH` finds "PH Casino" but not "usadeals".
    """
    if any(token in haystack for token in tokens):
        return True
    if not iso_code:
        return False
    pattern = rf"(?<![a-z0-9]){_re.escape(iso_code.lower())}(?![a-z0-9])"
    return _re.search(pattern, haystack) is not None


def _clean_proxy_url(value) -> Optional[str]:
    """Validate an HTTP(S) proxy URL without ever echoing its credentials."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 2048 or any(ch.isspace() for ch in raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return raw


def build_proxy_pool(urls) -> List[str]:
    """HTTP(S) endpoints only, de-duplicated, in the order supplied."""
    pool = []
    for url in urls or []:
        cleaned = _clean_proxy_url(url)
        if cleaned and cleaned not in pool:
            pool.append(cleaned)
    return pool


async def configured_proxy_urls() -> List[str]:
    """Server-side proxy pool: SCRAPER_PROXY_URLS plus HTTP entries in ProxyManager."""
    raw = (os.getenv("SCRAPER_PROXY_URLS") or "").strip()
    if not raw:
        try:
            raw = (await get_setting("scraper_proxy_urls", "SCRAPER_PROXY_URLS") or "").strip()
        except Exception:
            raw = ""
    candidates = [part for part in _re.split(r"[\s,]+", raw) if part]
    try:
        from proxy_manager import proxy_manager
        entries = list(proxy_manager.proxies.values())
        if proxy_manager.global_proxy:
            entries.append(proxy_manager.global_proxy)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("protocol") not in {"http", "https"}:
                continue
            host, port = entry.get("host"), entry.get("port")
            if not host or not port:
                continue
            user, password = entry.get("username"), entry.get("password")
            auth = (f"{quote(str(user), safe='')}:{quote(str(password), safe='')}@"
                    if user and password else "")
            candidates.append(f"http://{auth}{host}:{port}")
    except Exception:
        pass
    return build_proxy_pool(candidates)


class ProxyPicker:
    """Round-robin over a fixed HTTP proxy pool; counts direct fallbacks."""

    def __init__(self, proxies=None):
        self.proxies = build_proxy_pool(proxies)
        self._index = 0
        self.fallbacks = 0

    def pick(self):
        if not self.proxies:
            return None
        url = self.proxies[self._index % len(self.proxies)]
        self._index += 1
        return url

    def __bool__(self):
        return bool(self.proxies)


def proxy_diagnostics(mode: str, picker: Optional[ProxyPicker]) -> dict:
    """Counts only — never endpoints, hosts or credentials."""
    return {
        "mode": mode,
        "available": len(picker.proxies) if picker else 0,
        "used": bool(picker),
        "fallbacks": picker.fallbacks if picker else 0,
    }


def _picker_for(request: ChannelScrapeRequest, picker=None) -> ProxyPicker:
    if picker is not None:
        return picker
    if request.proxy_mode == "custom":
        return ProxyPicker(request.proxy_urls)
    return ProxyPicker()


async def _request(session, method, url, *, proxy=None, timeout=12, **kwargs):
    """One HTTP request; a proxy connection failure never blocks the read."""
    async with session.request(method, url, proxy=proxy,
                               timeout=aiohttp.ClientTimeout(total=timeout),
                               **kwargs) as resp:
        return resp.status, await resp.read()


def _clean_text(value, limit=400):
    """Unescape one line of page text and collapse its whitespace."""
    text = _re.sub(r"\s+", " ", unescape(value or "")).strip()
    return text[:limit]


def _tme_contact_page(html: str, username: str) -> bool:
    """True for t.me's generic page served for a deleted or renamed channel.

    Telegram answers HTTP 200 for an unknown username with the placeholder
    title `Telegram: Contact @<username>` and no member count. Without this
    check those dead rows stayed in the results with an empty member count.
    """
    title_m = _OG_TITLE_RE.search(html or "")
    if not title_m:
        return False
    title = _clean_text(unescape(title_m.group(1))).lower()
    return title == f"telegram: contact @{username.lower()}"


# Returned by _tme_preview for a username Telegram says does not exist, so the
# caller can drop the row instead of mistaking it for a transient preview miss.
_TME_DEAD = object()


def _parse_tme_preview(html: str, username: str):
    """Parse a public t.me/<username> preview page.

    Returns {username,title,type,members,about} for a public channel/supergroup,
    or None for a user, bot, private, or invalid page (no subscribers/members).
    ``about`` is the public description, which is what keyword and country
    matching reads beyond the title.
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
    desc_m = _OG_DESC_RE.search(html)
    about = _clean_text(desc_m.group(1)) if desc_m else ""
    members = None
    head = extra.split("subscriber")[0].split("member")[0]
    digits = _re.sub(r"[^\d]", "", head)
    if digits:
        try:
            members = int(digits)
        except ValueError:
            members = None
    return {"username": username, "title": title, "type": kind,
            "members": members, "about": about}


def _normalise_text(value) -> str:
    return " ".join((value or "").lower().split())


def _keyword_tokens(keyword: str):
    """The phrase plus its own words, so a long keyword still matches its parts."""
    phrase = _normalise_text(keyword)
    if not phrase:
        return []
    tokens = [phrase]
    tokens.extend(word for word in phrase.split() if len(word) >= 3)
    return list(dict.fromkeys(tokens))


def _keyword_matches(text, keyword: str) -> bool:
    """True when the keyword phrase or one of its words appears in the text."""
    haystack = _normalise_text(text)
    if not haystack or not keyword:
        return False
    for term in _keyword_tokens(keyword):
        if len(term) <= 3:
            if _re.search(rf"(?<![a-z0-9]){_re.escape(term)}(?![a-z0-9])", haystack):
                return True
        elif term in haystack:
            return True
    return False


# Topic vocabulary for the finder's Category filter. A row matches when any
# term appears in its title, username or public description. Category matching
# stays phrase-precise on purpose: keyword matching expands phrases into their
# words, which would drag "Online English lessons" into a casino search through
# the "online casino" term.
_CATEGORY_TERMS = {
    "casino": (
        "casino", "kasino", "igaming", "i-gaming", "online casino", "live casino",
        "slot", "slots", "slot gacor", "jackpot", "rtp", "baccarat", "poker",
        "roulette", "blackjack", "sabong", "cockfight", "betting", "sportsbook",
        "bookmaker", "lottery", "lotto", "togel", "jili", "pgsoft", "pragmatic",
        "1xbet", "bet365", "e-games", "egames", "pagcor", "color game", "bingo",
    ),
}


def _category_term_matches(text, term: str) -> bool:
    """Whole-term match; digit-bearing brands may sit inside a username."""
    haystack = _normalise_text(text)
    if not haystack or not term:
        return False
    if not any(char.isdigit() for char in term):
        return bool(_re.search(
            rf"(?<![a-z0-9]){_re.escape(term)}(?![a-z0-9])", haystack))
    return term in haystack


def _category_matches(title, username, about, category):
    """Matched category terms, empty when the row is outside the category."""
    terms = _CATEGORY_TERMS.get(category, ())
    if not terms:
        return []
    hits = []
    for term in terms:
        if (_category_term_matches(title, term)
                or _category_term_matches(username, term)
                or _category_term_matches(about, term)):
            hits.append(term)
    return hits


def _keyword_index(keywords):
    """Prepare one alternation over every keyword phrase and word.

    A long keyword list turns a per-row loop into millions of substring scans,
    so the terms are compiled once and each field is scanned once. Terms keep
    their owning keyword so a hit still reports which keyword matched.
    """
    terms: dict = {}
    for position, keyword in enumerate(keywords):
        for term in _keyword_tokens(keyword):
            terms.setdefault(term, []).append(position)
    parts = []
    for term in sorted(terms, key=len, reverse=True):
        escaped = _re.escape(term)
        if len(term) <= 3:
            parts.append(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
        else:
            parts.append(escaped)
    pattern = _re.compile("|".join(parts)) if parts else None
    return keywords, terms, pattern


def _match_fields(title, username, about, index):
    """Return (matched keywords, field names) for one channel or group."""
    keywords, terms, pattern = index
    hits: set = set()
    fields = []
    for name, value in (("title", title), ("username", username),
                        ("description", about)):
        haystack = _normalise_text(value)
        if not haystack or pattern is None:
            continue
        found = False
        for match in pattern.finditer(haystack):
            for position in terms.get(match.group(0), ()):
                hits.add(position)
                found = True
        if found:
            fields.append(name)
    return [keywords[position] for position in sorted(hits)], fields


def _decode_bing_target(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _usernames_from_html(html, want):
    """Extract public t.me usernames from a search result page.

    Handles plain links, percent-encoded redirect links, and Bing's base64
    wrapped `u=a1...` targets.
    """
    text = unescape(unquote(html or ""))
    found, seen = [], set()

    def add(candidate):
        key = candidate.lower()
        if key in _TME_RESERVED or key in seen:
            return False
        seen.add(key)
        found.append(candidate)
        return len(found) >= want

    for match in _TME_USERNAME_RE.finditer(text):
        if add(match.group(1)):
            return found
    for match in _BING_WRAPPED_RE.finditer(text):
        for link in _TME_USERNAME_RE.finditer(_decode_bing_target(match.group(1))):
            if add(link.group(1)):
                return found
    return found


async def _ddg_lite_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "POST", "https://lite.duckduckgo.com/lite/",
        data={"q": f"site:t.me {keyword}"}, proxy=proxy, timeout=10,
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    return _usernames_from_html(body.decode("utf-8", "replace"), want)


async def _ddg_html_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "GET", "https://html.duckduckgo.com/html/",
        params={"q": f"site:t.me {keyword}"}, proxy=proxy, timeout=10,
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    return _usernames_from_html(body.decode("utf-8", "replace"), want)


async def _mojeek_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "GET", "https://www.mojeek.com/search",
        params={"q": f"site:t.me {keyword}"}, proxy=proxy, timeout=10,
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    return _usernames_from_html(body.decode("utf-8", "replace"), want)


async def _bing_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "GET", "https://www.bing.com/search",
        params={"q": f"site:t.me {keyword}", "count": "50"}, proxy=proxy, timeout=10,
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    return _usernames_from_html(body.decode("utf-8", "replace"), want)


async def _brave_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "GET", "https://search.brave.com/search",
        params={"q": f"site:t.me {keyword}"}, proxy=proxy, timeout=10,
        headers={"Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9"},
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    return _usernames_from_html(body.decode("utf-8", "replace"), want)


# Lyzem links its own channel on every result page; it is never a match.
_LYZEM_NOISE = {"lyzemcom"}

_LYZEM_ROW_RE = _re.compile(r'<li class="search-result">(.*?)</li>', _re.S)
_LYZEM_TITLE_RE = _re.compile(r'class="search-result-title"[^>]*>(.*?)</p>', _re.S)
_LYZEM_DESCR_RE = _re.compile(r'class="search-result-descr"[^>]*>(.*?)</p>', _re.S)
_LYZEM_KIND_RE = _re.compile(
    r'<div class="search-result-type-wrapper[^"]*"[^>]*\btitle="(channel|group|bot)"',
    _re.I)
_LYZEM_KIND_SPAN_RE = _re.compile(
    r'<div class="search-group">\s*<span>\s*(channel|group|bot)\s*</span>', _re.I)
_LYZEM_LINK_RE = _re.compile(r'https://t\.me/([A-Za-z]\w{3,31})')
_LYZEM_TYPE_FILTER = {"channel": "channels", "group": "groups"}
_TAG_RE = _re.compile(r"<[^>]+>")


def _lyzem_text(match) -> str:
    return _clean_text(unescape(_TAG_RE.sub(" ", match.group(1)))) if match else ""


def _parse_lyzem_rows(html, want):
    """Read lyzem's structured result rows: public link, title, description, type.

    lyzem indexes Telegram's own public preview, so these rows already say
    whether a target is a channel or a group and what its description is. That
    lets discovery match keywords and countries without a t.me round trip,
    which is what the preview budget could not keep up with.
    """
    rows, seen = [], set()
    for block in _LYZEM_ROW_RE.findall(html or ""):
        kind_m = _LYZEM_KIND_RE.search(block) or _LYZEM_KIND_SPAN_RE.search(block)
        if not kind_m:
            continue
        kind = kind_m.group(1).lower()
        if kind not in ("channel", "group"):
            continue  # bots and unknown targets are never scrape targets
        link_m = _LYZEM_LINK_RE.search(block)
        if not link_m:
            continue
        username = link_m.group(1)
        key = username.lower()
        if key in _TME_RESERVED or key in _LYZEM_NOISE or key in seen:
            continue
        seen.add(key)
        rows.append({
            "username": username,
            "title": _lyzem_text(_LYZEM_TITLE_RE.search(block)) or username,
            "about": _lyzem_text(_LYZEM_DESCR_RE.search(block)),
            "type": kind,
        })
        if len(rows) >= want:
            break
    return rows


async def _lyzem_rows(session, keyword, want, proxy=None, group_type="all"):
    """Structured lyzem search, paging until the wanted rows or ten pages."""
    params = {"q": keyword, "f": _LYZEM_TYPE_FILTER.get(group_type, "all")}
    per_page = next((size for size in (100, 50, 25, 10) if size <= max(10, want)), 10)
    params["per-page"] = str(per_page)
    max_pages = min(10, max(1, -(-want // per_page)))
    rows, seen = [], set()
    for page in range(1, max_pages + 1):
        if page > 1:
            params["p"] = str(page)
        status, body = await _request(
            session, "GET", "https://lyzem.com/search",
            params=params, proxy=proxy, timeout=10,
            headers={"Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
        if status != 200:
            raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
        page_rows = _parse_lyzem_rows(body.decode("utf-8", "replace"), want)
        for row in page_rows:
            key = row["username"].lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        if len(rows) >= want or not page_rows:
            break
    return rows[:want]


async def _lyzem_usernames(session, keyword, want, proxy=None):
    status, body = await _request(
        session, "GET", "https://lyzem.com/search",
        params={"q": keyword, "type": "all"}, proxy=proxy, timeout=10,
        headers={"Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9"},
    )
    if status != 200:
        raise aiohttp.ClientConnectionError(f"search source returned HTTP {status}")
    names = _usernames_from_html(body.decode("utf-8", "replace"), want + len(_LYZEM_NOISE))
    return [name for name in names if name.lower() not in _LYZEM_NOISE][:want]


# Tried in parallel; the first one that returns candidates wins.
# Brave and Lyzem answer from datacenter IPs where DuckDuckGo and Bing often
# rate-limit the server, so they lead the race.
_SEARCH_SOURCES = (
    ("brave", _brave_usernames),
    ("lyzem", _lyzem_usernames),
    ("duckduckgo", _ddg_lite_usernames),
    ("duckduckgo_html", _ddg_html_usernames),
    ("mojeek", _mojeek_usernames),
    ("bing", _bing_usernames),
)


async def _tme_preview(session, username, proxy=None):
    try:
        status, body = await _request(
            session, "GET", f"https://t.me/{username}", proxy=proxy, timeout=10)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    if status == 404:
        return _TME_DEAD
    if status != 200:
        return None
    html = body.decode("utf-8", "replace")
    info = _parse_tme_preview(html, username)
    if info is None and _tme_contact_page(html, username):
        return _TME_DEAD
    return info


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


# TGStat error strings that mean no keyword can succeed: the credential itself
# is unusable, so retrying the remaining keywords only burns time.
_TGSTAT_FATAL_ERRORS = {
    "no_active_subscription",
    "invalid_token",
    "token_not_found",
    "unauthorized",
    "http_401",
    "http_403",
}


def _tgstat_error_is_fatal(error: str) -> bool:
    return bool(error) and error in _TGSTAT_FATAL_ERRORS


async def _tgstat_search(session, token, keyword, peer_type, country_code, limit,
                         language="", proxy=None):
    """Return (normalised items, whether TGStat answered successfully, error code).

    The error code is TGStat's own `error` string when it reports one (for
    example `no_active_subscription`), otherwise `http_<status>`, `timeout`, or
    `bad_response`. It never contains the request URL or the token.
    """
    params = {"token": token, "q": keyword, "search_by_description": "1",
              "peer_type": peer_type, "limit": str(min(max(limit, 1), 100))}
    if country_code:
        params["country"] = country_code
    if language:
        params["language"] = language
    try:
        status, body = await _request(
            session, "GET", _TGSTAT_SEARCH_URL, params=params, proxy=proxy, timeout=15)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return [], False, "timeout"
    if status != 200:
        return [], False, f"http_{status}"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return [], False, "bad_response"
    if not isinstance(data, dict):
        return [], False, "bad_response"
    if data.get("status") != "ok":
        error = str(data.get("error") or "").strip()
        return [], False, error or "bad_response"
    response = data.get("response")
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        return [], True, ""
    return [info for info in (_parse_tgstat_item(it, peer_type) for it in items) if info], True, ""


KEYWORD_SEARCH_BUDGET = 90.0  # seconds; queued keywords past this are reported as partial


async def _gather_keywords(keywords, worker, budget, concurrency=5):
    """Look every keyword up concurrently, dropping work queued past the budget.

    A lookup already talking to the upstream service is allowed to finish so the
    results stay truthful; keywords that never started are reported as skipped.
    """
    deadline = time.monotonic() + budget
    semaphore = asyncio.Semaphore(max(1, concurrency))
    skipped: list = []

    async def run(keyword):
        async with semaphore:
            if time.monotonic() >= deadline:
                skipped.append(keyword)
                return keyword, None
            return keyword, await worker(keyword)

    return await asyncio.gather(*(run(keyword) for keyword in keywords)), skipped


def _rank_result(info):
    """Best first: more keyword hits, more matched fields, title before members."""
    matched = info.get("matched_keywords") or (
        [info["matched_keyword"]] if info.get("matched_keyword") else [])
    fields = info.get("matched_in") or []
    title = (info.get("title") or "").lower()
    in_title = sum(1 for keyword in matched if keyword and keyword.lower() in title)
    return (len(matched), len(fields), in_title, "title" in fields,
            info.get("members") or 0)


async def _scrape_channels_tgstat(request: ChannelScrapeRequest, token, picker=None):
    """Account-free discovery via the TGStat directory API.

    Country names are mapped to ISO codes for TGStat's native filter. If TGStat
    rejects an optional filter (language, or an unfamiliar country), the lookup
    is retried with just the keyword so a filter guess never empties the run.
    """
    peer_type = _tgstat_peer_type(request.group_type)
    iso_country, country_tokens = resolve_country(request.country)
    language_code = resolve_language(request.language)
    picker = _picker_for(request, picker)
    stats = {"requests": 0, "ok": 0, "errors": 0, "items": 0,
             "rejected_filters": 0, "error": ""}
    filtered = {"type": 0, "country": 0, "category": 0, "members": 0}
    # Filters run after TGStat answers, so fetching only `limit` rows would let a
    # rejected row occupy a slot that a qualifying row behind it deserved.
    fetch_limit = (100 if request.category != "all" or request.min_members
                   else request.limit)
    seen: set = set()
    results: list = []
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        async def lookup(keyword):
            stats["requests"] += 1
            proxy = picker.pick()
            infos, ok, error = await _tgstat_search(
                session, token, keyword, peer_type, iso_country, fetch_limit,
                language_code, proxy=proxy)
            if not ok and (iso_country or language_code) and not _tgstat_error_is_fatal(error):
                stats["requests"] += 1
                infos, ok, error = await _tgstat_search(
                    session, token, keyword, peer_type, "", fetch_limit, "",
                    proxy=picker.pick())
                if ok:
                    stats["rejected_filters"] += 1
            if not ok and proxy and not _tgstat_error_is_fatal(error):
                # A dead proxy must not hide the directory behind it.
                picker.fallbacks += 1
                stats["requests"] += 1
                infos, ok, error = await _tgstat_search(
                    session, token, keyword, peer_type, "", fetch_limit, "")
            stats["ok" if ok else "errors"] += 1
            stats["items"] += len(infos)
            if not ok and error and not stats["error"]:
                stats["error"] = error
            return infos, ok, error

        # Probe one keyword first: an unusable credential fails on the first
        # request, so the remaining keywords are skipped instead of all racing
        # into the same error.
        keywords = list(request.keywords)
        per_keyword: list = []
        skipped: list = []
        started = time.monotonic()
        if keywords:
            infos, ok, error = await lookup(keywords[0])
            per_keyword.append((keywords[0], (infos, ok)))
            if ok or not _tgstat_error_is_fatal(error):
                # Search the rest concurrently; a runaway keyword list must not
                # push the whole request past the dashboard's 120 s timeout. The
                # probe already spent part of the budget.
                remaining = max(0.0, KEYWORD_SEARCH_BUDGET - (time.monotonic() - started))
                gathered, skipped = await _gather_keywords(
                    keywords[1:], lookup, remaining, concurrency=8)
                per_keyword.extend(
                    (keyword, (found[0], found[1])) if found else (keyword, None)
                    for keyword, found in gathered)
        # Text matching is only a stand-in when TGStat has no ISO filter for the
        # input. Once an ISO filter is rejected, the returned rows are simply
        # keyword matches and must not be dropped by a guessed country name.
        text_tokens = country_tokens if not iso_country else []
        for keyword, found in per_keyword:
            if found is None:
                continue
            infos, _ok = found
            for info in infos:
                key = info["username"].lower()
                if key in seen:
                    continue
                if request.group_type != "all" and info["type"] != request.group_type:
                    filtered["type"] += 1
                    continue
                if text_tokens and not any(
                        token in (info["title"] + " " + info["username"]).lower()
                        for token in text_tokens):
                    filtered["country"] += 1
                    continue
                if request.category != "all" and not _category_matches(
                        info["title"], info["username"], info.get("about", ""), request.category):
                    filtered["category"] += 1
                    continue
                if request.min_members and (info["members"] is None
                                            or info["members"] <= request.min_members):
                    filtered["members"] += 1
                    continue
                seen.add(key)
                info["matched_keyword"] = keyword
                info.pop("about", None)
                results.append(info)
    results.sort(key=_rank_result, reverse=True)
    results = results[:request.limit]
    partial = bool(skipped)
    reason = ""
    if not results:
        if stats["error"] == "no_active_subscription":
            reason = ("TGStat rejected the request: TGSTAT_TOKEN has no active TGStat API "
                      "subscription. Enable the subscription or clear the token to use "
                      "public search.")
        elif stats["error"]:
            reason = (f"TGStat did not answer any keyword ({stats['error']}). "
                      "Check TGSTAT_TOKEN and try again.")
        elif stats["ok"] == 0:
            reason = ("TGStat did not answer any keyword. Check TGSTAT_TOKEN and try again.")
        elif stats["rejected_filters"]:
            reason = ("TGStat rejected the country or language filter and still found no "
                      "matches. Broaden the keywords or clear the filter.")
        elif filtered["category"]:
            reason = ("TGStat answered but the category filter removed every match. "
                      "Choose All categories to see them.")
        elif filtered["members"]:
            reason = ("TGStat answered but the member filter removed every match. "
                      "Lower the member threshold or set it to 0 to see them.")
        else:
            reason = ("TGStat answered but matched no channels. Broaden the keywords or "
                      "clear the country/language filter.")
    log_manager.add_log(
        "ChannelScraper", "tgstat",
        f"Found {len(results)} channels/groups (TGStat) for {len(request.keywords)} keyword(s)"
        + (f"; {len(skipped)} skipped at the search deadline" if partial else ""),
        "success",
    )
    return {
        "success": True,
        "total": len(results),
        "results": results,
        "mode": "tgstat",
        "partial": partial,
        "diagnostics": {
            "mode": "tgstat",
            "token_configured": True,
            "country": {
                "input": request.country,
                "iso": iso_country,
                "applied": ("filter" if iso_country and not stats["rejected_filters"]
                            else "text" if text_tokens else "none"),
            },
            "language": {
                "input": request.language,
                "code": language_code,
                "applied": ("filter" if language_code and not stats["rejected_filters"]
                            else "none"),
            },
            "category": {
                "input": request.category,
                "applied": "terms" if request.category != "all" else "none",
            },
            "members": {"above": request.min_members},
            "proxies": proxy_diagnostics(request.proxy_mode, picker),
            "filtered": filtered,
            "sources": {"tgstat": stats},
            "reason": reason,
        },
    }


def _tgstat_failure_sentence(error: str, result) -> str:
    """One sentence explaining why TGStat returned nothing, safe to show."""
    if error == "no_active_subscription":
        return "TGSTAT_TOKEN has no active TGStat subscription."
    if error:
        return f"TGStat returned an error ({error})."
    return (result.get("diagnostics", {}).get("reason")
            or "TGStat returned no matches.")


def _tgstat_fallback_notice(error: str) -> str:
    if error == "no_active_subscription":
        return ("TGStat has no active subscription for TGSTAT_TOKEN; showing public "
                "search results instead.")
    if error:
        return f"TGStat returned no results ({error}); showing public search results instead."
    return "TGStat returned no results; showing public search results instead."


async def _scrape_channels_public(request: ChannelScrapeRequest):
    """Account-free discovery. Uses the TGStat directory API when TGSTAT_TOKEN is
    configured (reliable, searches title + description, native country filter),
    and falls back to public web discovery when TGStat finds nothing. The token
    comes from the TGSTAT_TOKEN env var or the operations_shared.app_settings
    store."""
    token = await get_setting("tgstat_token", "TGSTAT_TOKEN")
    pool = await configured_proxy_urls() if request.proxy_mode == "pool" else []
    picker = ProxyPicker(request.proxy_urls if request.proxy_mode == "custom" else pool)
    if not token:
        return await _scrape_channels_web(request, picker=picker)
    result = await _scrape_channels_tgstat(request, token, picker=picker)
    if result["total"]:
        return result

    # A valid token can still return nothing — most often because the account
    # has no TGStat API subscription. Public search is a worse directory but a
    # working one, so never report zero while it is available.
    fallback = await _scrape_channels_web(request, picker=picker)
    diagnostics = fallback["diagnostics"]
    error = ((result.get("diagnostics", {}).get("sources", {}).get("tgstat", {}) or {})
             .get("error", ""))
    diagnostics["fallback_from"] = "tgstat"
    diagnostics["tgstat_error"] = error
    diagnostics["token_configured"] = True
    if fallback["total"]:
        diagnostics["notice"] = _tgstat_fallback_notice(error)
    else:
        diagnostics["reason"] = (
            f"TGStat: {_tgstat_failure_sentence(error, result)} "
            f"Public search: {diagnostics.get('reason') or 'no results.'}")
    return fallback


_PUBLIC_SEARCH_BUDGET = 90.0   # seconds; keywords are queried in parallel up to 16 at once
_PUBLIC_SEARCH_CONCURRENCY = 16
_PUBLIC_PREVIEW_BUDGET = 35.0  # seconds; scales with the ceiling below
_PUBLIC_PREVIEW_MAX = 120      # floor: preview pages fetched at most per run
_PUBLIC_PREVIEW_CEILING = 360  # cap: follows the result limit up to this many
_PUBLIC_PREVIEW_CONCURRENCY = 6
_PUBLIC_PREVIEW_FAILURES = 20  # consecutive preview failures before the run stops asking
_PUBLIC_CANDIDATE_MAX = 20000  # candidates held at most, regardless of the result ceiling
_SOURCE_GRACE = 1.0            # seconds the losing sources may still add candidates


async def _first_source_candidates(session, keyword, want, proxy, stats, skip=(), grace=_SOURCE_GRACE):
    """Race the public search sources and merge every answer inside a short grace window.

    The first source to answer provides the candidates, but the sources still in
    flight are given a short window to add theirs instead of being cancelled:
    each one indexes a different slice of Telegram, so first-wins silently lost
    recall whenever a fast but thin source won the race.
    """
    async def run(name, source):
        stats[name]["requests"] += 1
        try:
            return await source(session, keyword, want, proxy=proxy)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            stats[name]["errors"] += 1
            return []

    def collect(tasks_done):
        for task in tasks_done:
            names = task.result()
            if not names:
                continue
            stats[tasks[task]]["items"] += len(names)
            stats[tasks[task]]["ok"] += 1
            found.extend(names)

    tasks = {asyncio.create_task(run(name, source)): name
             for name, source in _SEARCH_SOURCES if name not in skip}
    pending = set(tasks)
    found: list = []
    try:
        while pending and not found:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            collect(done)
        if found and pending:
            more, pending = await asyncio.wait(pending, timeout=grace)
            collect(more)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    unique, seen_names = [], set()
    for name in found:
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        unique.append(name)
    return unique[:want]


async def _scrape_channels_web(request: ChannelScrapeRequest, picker=None):
    """Account-free discovery: structured public search, then web search fallback.

    lyzem indexes Telegram's public previews, so it answers with the link, the
    title, the description and the channel/group label in one request. That is
    what keyword and country matching read, and it no longer depends on a t.me
    preview per candidate (which the server's IP could not keep up with).
    Every other public source is still raced as a fallback, and results are
    de-duplicated across keywords and sources.
    """
    picker = _picker_for(request, picker)
    iso_country, country_tokens = resolve_country(request.country)
    language_code = resolve_language(request.language)
    index = _keyword_index(request.keywords)
    candidate_cap = min(max(request.limit * 20, 2000), _PUBLIC_CANDIDATE_MAX)
    # A short keyword list may page lyzem for depth; a long list gets one page
    # each so the search budget covers as many keywords as possible.
    want_per_keyword = (min(max(request.limit, 25), 1000)
                        if len(request.keywords) <= 8
                        else min(max(request.limit, 25), 100))
    seen: set = set()
    candidates: list = []          # (username, keyword) needing a t.me preview
    structured: dict = {}          # username.lower() -> row with title and description
    stats = {name: {"requests": 0, "ok": 0, "errors": 0, "items": 0}
             for name, _source in _SEARCH_SOURCES}
    # lyzem is queried on its own path, not inside the generic source race.
    stats.setdefault("lyzem", {"requests": 0, "ok": 0, "errors": 0, "items": 0})
    preview_stats = {"requests": 0, "ok": 0, "errors": 0, "skipped": 0, "dead": 0}
    filtered = {"type": 0, "country": 0, "category": 0, "members": 0, "dead": 0}

    def remember(username, keyword, row=None):
        key = username.lower()
        if key in seen:
            return
        seen.add(key)
        if row is None:
            candidates.append((username, keyword))
            return
        entry = dict(row)
        entry["username"] = username
        entry["matched_keyword"] = keyword
        structured[key] = entry

    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        country_hint = country_tokens[0] if country_tokens else ""

        def country_query(keyword):
            """Carry the country inside the lyzem query.

            lyzem matches every term against the channel's title, username and
            description, so a country-qualified query returns pages that name
            the country. Searching the bare keyword and filtering afterwards
            threw away almost the whole index (`casino` + Philippines: 58 rows,
            none naming the country).
            """
            if not country_hint:
                return keyword
            lowered = keyword.lower()
            if any(token in lowered for token in country_tokens):
                return keyword
            if iso_country:
                pattern = rf"(?<![a-z0-9]){_re.escape(iso_country.lower())}(?![a-z0-9])"
                if _re.search(pattern, lowered):
                    return keyword
            return f"{keyword} {country_hint}".strip()

        async def lyzem(query, proxy):
            stats["lyzem"]["requests"] += 1
            try:
                return await _lyzem_rows(
                    session, query, want_per_keyword, proxy=proxy,
                    group_type=request.group_type)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                stats["lyzem"]["errors"] += 1
                return []

        async def discover(keyword):
            if len(seen) >= candidate_cap:
                return
            proxy = picker.pick()
            qualified = country_query(keyword)
            rows = await lyzem(qualified, proxy)
            if not rows and qualified != keyword:
                # A country-qualified query can starve where no page names
                # both terms; retry the bare keyword before the generic race.
                rows = await lyzem(keyword, proxy)
            if rows:
                stats["lyzem"]["ok"] += 1
                stats["lyzem"]["items"] += len(rows)
                for row in rows:
                    remember(row["username"], keyword, row)
                    if len(seen) >= candidate_cap:
                        break
                return
            query = f"{keyword} {country_hint}".strip() if country_hint else keyword
            names = await _first_source_candidates(
                session, query, want_per_keyword, proxy, stats, skip=("lyzem",))
            if not names and country_hint:
                # Engines that require every term can starve a country-qualified
                # query; retry the bare keyword before calling the run empty.
                names = await _first_source_candidates(
                    session, keyword, want_per_keyword, proxy, stats, skip=("lyzem",))
            if not names and proxy:
                # If the proxy itself is dead, retry the race directly once.
                picker.fallbacks += 1
                names = await _first_source_candidates(
                    session, query, want_per_keyword, None, stats, skip=("lyzem",))
            for username in names:
                remember(username, keyword)
                if len(seen) >= candidate_cap:
                    break

        _, skipped = await _gather_keywords(
            request.keywords, discover, _PUBLIC_SEARCH_BUDGET,
            concurrency=_PUBLIC_SEARCH_CONCURRENCY)

        entries: list = []
        for row in structured.values():
            if request.group_type != "all" and row["type"] != request.group_type:
                filtered["type"] += 1
                continue
            haystack = f"{row['title']} {row['username']} {row['about']}".lower()
            if (country_tokens or iso_country) and not country_matches(
                    haystack, country_tokens, iso_country):
                filtered["country"] += 1
                continue
            if request.category != "all" and not _category_matches(
                    row["title"], row["username"], row["about"], request.category):
                filtered["category"] += 1
                continue
            matched, fields = _match_fields(
                row["title"], row["username"], row["about"], index)
            if not matched:
                matched = [row["matched_keyword"]]
            entries.append({
                "username": row["username"],
                "title": row["title"],
                "about": row["about"],
                "type": row["type"],
                "members": None,
                "matched_keywords": matched,
                "matched_in": fields,
                "source": "lyzem",
            })

        entries.sort(key=_rank_result, reverse=True)
        # Generic candidates exist only through a preview, so they get first call
        # on the budget; structured rows already carry type and description.
        preview_cap = min(max(request.limit, _PUBLIC_PREVIEW_MAX),
                          _PUBLIC_PREVIEW_CEILING)
        preview_targets = [username for username, _keyword
                           in candidates[:preview_cap // 2]]
        reserved = {username.lower() for username in preview_targets}
        preview_targets += [entry["username"] for entry in entries
                            if entry["username"].lower() not in reserved]
        preview_targets = preview_targets[:preview_cap]
        preview_stats["skipped"] = max(
            0, len(candidates) + len(entries) - len(preview_targets))

        semaphore = asyncio.Semaphore(_PUBLIC_PREVIEW_CONCURRENCY)
        stop = asyncio.Event()

        async def enrich(username):
            async with semaphore:
                if stop.is_set():
                    preview_stats["skipped"] += 1
                    return username, None
                preview_stats["requests"] += 1
                info = await _tme_preview(session, username, proxy=picker.pick())
                if info is _TME_DEAD:
                    preview_stats["dead"] += 1
                    return username, _TME_DEAD
                if not info:
                    preview_stats["errors"] += 1
                    if (preview_stats["errors"] >= _PUBLIC_PREVIEW_FAILURES
                            and preview_stats["errors"] > preview_stats["ok"] * 2):
                        stop.set()  # the server is rate limiting; stop asking
                    return username, None
                preview_stats["ok"] += 1
                return username, info

        tasks = [asyncio.create_task(enrich(username)) for username in preview_targets]
        done, pending = (await asyncio.wait(tasks, timeout=_PUBLIC_PREVIEW_BUDGET)
                         if tasks else (set(), set()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    previews = {}
    for task in tasks:
        if task not in done or task.cancelled() or task.exception() is not None:
            continue
        username, info = task.result()
        if info:
            previews[username.lower()] = info

    results: list = []
    for entry in entries:
        info = previews.get(entry["username"].lower())
        if info is _TME_DEAD:
            filtered["dead"] += 1
            continue
        if info:
            entry["type"] = info["type"]
            entry["members"] = info["members"]
            entry["title"] = info["title"] or entry["title"]
            if info.get("about"):
                entry["about"] = info["about"]
        # The preview is the authoritative metadata, so the category decision
        # has to be made again now that it may have replaced title/description.
        if request.category != "all" and not _category_matches(
                entry["title"], entry["username"], entry["about"], request.category):
            filtered["category"] += 1
            continue
        if request.group_type != "all" and entry["type"] != request.group_type:
            filtered["type"] += 1
            continue
        if request.min_members and (entry["members"] is None
                                    or entry["members"] <= request.min_members):
            filtered["members"] += 1
            continue
        results.append({
            "id": entry["username"],
            "title": entry["title"],
            "username": entry["username"],
            "url": f"https://t.me/{entry['username']}",
            "type": entry["type"],
            "members": entry["members"],
            "verified": False,
            "about": entry["about"],
            "matched_keyword": entry["matched_keywords"][0],
            "matched_keywords": entry["matched_keywords"],
            "matched_in": entry["matched_in"],
        })

    structured_names = {entry["username"].lower() for entry in entries}
    for username, keyword in candidates:
        key = username.lower()
        if key in structured_names:
            continue
        info = previews.get(key)
        if info is _TME_DEAD:
            filtered["dead"] += 1
            continue
        if not info:
            continue  # without a preview there is no public type to trust
        if request.group_type != "all" and info["type"] != request.group_type:
            filtered["type"] += 1
            continue
        haystack = f"{info['title']} {info['username']} {info.get('about', '')}".lower()
        if (country_tokens or iso_country) and not country_matches(
                haystack, country_tokens, iso_country):
            filtered["country"] += 1
            continue
        if request.category != "all" and not _category_matches(
                info["title"], info["username"], info.get("about", ""), request.category):
            filtered["category"] += 1
            continue
        if request.min_members and (info["members"] is None
                                    or info["members"] <= request.min_members):
            filtered["members"] += 1
            continue
        matched, fields = _match_fields(
            info["title"], info["username"], info.get("about", ""), index)
        if not matched:
            matched = [keyword]
        results.append({
            "id": info["username"],
            "title": info["title"],
            "username": info["username"],
            "url": f"https://t.me/{info['username']}",
            "type": info["type"],
            "members": info["members"],
            "verified": False,
            "about": info.get("about", ""),
            "matched_keyword": matched[0],
            "matched_keywords": matched,
            "matched_in": fields,
        })

    results.sort(key=_rank_result, reverse=True)
    results = results[:request.limit]
    partial = bool(skipped)
    reason = ""
    if not results:
        attempted = sum(source["requests"] for source in stats.values())
        answered = sum(source["ok"] for source in stats.values())
        errored = sum(source["errors"] for source in stats.values())
        if not seen and errored and not answered:
            reason = ("Every public search source refused or timed out for this server "
                      f"({errored} of {attempted} requests). Set TGSTAT_TOKEN for reliable "
                      "search, or choose an HTTP proxy.")
        elif not seen:
            reason = ("Public search returned no t.me links for these keywords. Try broader "
                      "keywords or set TGSTAT_TOKEN for directory search.")
        elif filtered["country"]:
            reason = ("Candidates were found but the country filter removed all of them. "
                      "Clear the country filter to see them.")
        elif filtered["category"]:
            reason = ("Candidates were found but the category filter removed all of them. "
                      "Choose All categories to see them.")
        elif filtered["members"]:
            reason = ("Candidates were found but the member filter removed all of them. "
                      "Lower the member threshold or set it to 0 to see them.")
        elif filtered["type"]:
            reason = ("Candidates were found but the type filter removed all of them. "
                      "Choose All to see them.")
        elif preview_stats["requests"] and preview_stats["ok"] == 0:
            reason = "Candidate links were found but the t.me previews failed. Retry in a moment."
        else:
            reason = "Candidates were found but none of them is a public channel or group."
    log_manager.add_log(
        "ChannelScraper", "public",
        f"Found {len(results)} channels/groups from {len(seen)} candidate(s) for "
        f"{len(request.keywords)} keyword(s)"
        + ("; results are partial because a budget ran out" if partial else ""),
        "success",
    )
    return {
        "success": True,
        "total": len(results),
        "results": results,
        "mode": "public",
        "partial": partial,
        "diagnostics": {
            "mode": "public",
            "token_configured": False,
            "country": {
                "input": request.country,
                "iso": iso_country,
                "applied": "text" if country_tokens else "none",
            },
            "language": {"input": request.language, "code": language_code, "applied": "none"},
            "category": {
                "input": request.category,
                "applied": "terms" if request.category != "all" else "none",
            },
            "members": {"above": request.min_members},
            "proxies": proxy_diagnostics(request.proxy_mode, picker),
            "candidates": len(seen),
            "keywords": {
                "input": len(request.keywords),
                "queried": len(request.keywords) - len(skipped),
                "skipped": len(skipped),
            },
            "filtered": filtered,
            "sources": {**stats, "previews": preview_stats},
            "reason": reason,
        },
    }


async def _chat_description(client, chat, username: str) -> str:
    """Public description for the category check; empty when Telegram hides it."""
    try:
        if isinstance(chat, types.Chat):
            full = await client(functions.messages.GetFullChatRequest(chat_id=chat.id))
        else:
            full = await client(functions.channels.GetFullChannelRequest(channel=username))
    except errors.FloodWaitError as exc:
        raise HTTPException(status_code=429,
                            detail=f"Telegram rate limit reached — retry in {exc.seconds}s") from None
    except errors.RPCError:
        return ""
    full_chat = getattr(full, "full_chat", None)
    return getattr(full_chat, "about", "") or ""


@telegram_ops_router.post("/api/scraper/channels")
async def scrape_channels(request: ChannelScrapeRequest):
    """Discover public Telegram channels/groups by keyword.

    With a selected account, uses Telegram's authenticated directory search
    (contacts.SearchRequest). With no account, falls back to account-free public
    discovery (web search + public t.me preview pages). Either way returns only
    public channels/supergroups with a t.me URL, de-duplicated. Account-free
    discovery maps the country to TGStat's ISO filter when it can; the account
    path falls back to a case-insensitive title/username filter because Telegram
    exposes no channel country. Read-only: no messages, no joins.
    """
    try:
        # The model is schema-validated by FastAPI; proxy checks live here so a
        # bad proxy URL becomes a plain, sanitized 400 instead of a 422 that
        # echoes the request body (which can carry proxy credentials).
        request.apply_proxy_config()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    account_id = request.account_id.strip()
    if not account_id:
        return await _scrape_channels_public(request)

    client = await account_manager.get_client(account_id)
    if not client:
        raise HTTPException(status_code=400, detail=_client_unavailable_detail(account_id))

    iso_country, country_tokens = resolve_country(request.country)
    language_code = resolve_language(request.language)
    seen: set = set()
    results: list = []
    filtered = {"type": 0, "country": 0, "category": 0, "members": 0}
    descriptions: dict = {}
    deadline = time.monotonic() + KEYWORD_SEARCH_BUDGET
    partial = False
    for keyword in request.keywords:
        if len(results) >= request.limit:
            break
        if time.monotonic() >= deadline:
            partial = True
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
                filtered["type"] += 1
                continue
            title = getattr(chat, "title", "") or ""
            haystack = f"{title} {username}".lower()
            if (country_tokens or iso_country) and not country_matches(
                    haystack, country_tokens, iso_country):
                filtered["country"] += 1
                continue
            if request.category != "all" and not _category_matches(
                    title, username, "", request.category):
                # The directory entity carries no description, so a neutral
                # title/username still needs the public description before the
                # row is counted as outside the category.
                if chat_id not in descriptions:
                    descriptions[chat_id] = await _chat_description(
                        client, chat, username)
                if not _category_matches(
                        title, username, descriptions[chat_id], request.category):
                    filtered["category"] += 1
                    continue
            members = getattr(chat, "participants_count", None)
            if request.min_members and (members is None
                                        or members <= request.min_members):
                filtered["members"] += 1
                continue
            seen.add(chat_id)
            results.append({
                "id": chat_id,
                "title": title,
                "username": username,
                "url": f"https://t.me/{username}",
                "type": kind,
                "members": members,
                "verified": bool(getattr(chat, "verified", False)),
                "about": descriptions.get(chat_id, ""),
                "matched_keyword": keyword,
            })
            if len(results) >= request.limit:
                break

    if results:
        reason = ""
    elif filtered["country"]:
        reason = ("Candidates were found but the country filter removed all of them. "
                  "Clear the country filter to see them.")
    elif filtered["category"]:
        reason = ("Candidates were found but the category filter removed all of them. "
                  "Choose All categories to see them.")
    elif filtered["members"]:
        reason = ("Candidates were found but the member filter removed all of them. "
                  "Lower the member threshold or set it to 0 to see them.")
    elif filtered["type"]:
        reason = ("Candidates were found but the type filter removed all of them. "
                  "Choose All to see them.")
    else:
        reason = ("Telegram's directory returned no public channels for these keywords. "
                  "Broaden the keywords or clear the country filter.")
    log_manager.add_log(
        "ChannelScraper", account_id,
        f"Found {len(results)} channels/groups for {len(request.keywords)} keyword(s)"
        + ("; stopped at the search deadline" if partial else ""),
        "success",
    )
    return {
        "success": True,
        "total": len(results),
        "results": results,
        "mode": "account",
        "partial": partial,
        "diagnostics": {
            "mode": "account",
            "token_configured": True,
            "country": {
                "input": request.country,
                "iso": "",
                "applied": "text" if country_tokens else "none",
            },
            "language": {
                "input": request.language,
                "code": language_code,
                "applied": "none",
            },
            "category": {
                "input": request.category,
                "applied": "terms" if request.category != "all" else "none",
            },
            "members": {"above": request.min_members},
            "filtered": filtered,
            "proxies": {"mode": request.proxy_mode, "available": 0, "used": False, "fallbacks": 0},
            "sources": {"account": {"requests": len(request.keywords), "ok": 1 if results else 0}},
            "reason": reason,
        },
    }


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
                record_provider_restriction(request.account_id, e, "member_add")
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
        media = []
        if request.media_ids:
            if not operator or not getattr(operator, "id", None):
                raise HTTPException(status_code=403, detail="Authenticated operator required")
            try:
                media = bulk_media.resolve(request.media_ids, str(operator.id))
            except HTTPException as error:
                if error.status_code == 404:
                    raise HTTPException(status_code=400,
                                        detail="Unknown attachment; upload it again.") from None
                raise
        media_paths = [item["path"] for item in media]
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

        if media_paths:
            if len(message_text) > 1024 or any(len(v) > 1024 for v in request.messages.values()):
                raise HTTPException(status_code=400, detail="A caption is limited to 1024 characters when a file is attached")
        elif (not message_text.strip() and any(t not in request.messages for t in targets)) or len(message_text) > 4096:
            raise HTTPException(status_code=400, detail="A valid message or renderable template is required")

        async def deliver(client, entity, text):
            if media_paths:
                payload = media_paths[0] if len(media_paths) == 1 else media_paths
                return await client.send_file(entity, payload, caption=text or None)
            return await client.send_message(entity, text)

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
                        send=lambda c=client, e=entity, t=target: deliver(
                            c, e, request.messages.get(t, message_text)))
                    outcome = ("sent", "", account_id)
                    break
                except account_limit_errors as exc:
                    # Account-wide limit: drop it from the pool and switch to the next
                    # available account for this recipient.
                    record_provider_restriction(account_id, exc, "personal_send")
                    if rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", type(exc).__name__, account_id)
                    break
                except OutreachDenied as exc:
                    record_provider_restriction(account_id, exc, "personal_send")
                    if str(exc) == "account_cooldown" and rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", str(exc), account_id)
                    break
                except Exception as exc:
                    record_provider_restriction(account_id, exc, "personal_send")
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

        links = request.urls[:request.limit] if request.limit else request.urls
        # Invite hashes and public usernames are separate RPCs; Telegram floods
        # them independently, so each link records against its own scope.
        account_limit_errors = (errors.FloodWaitError, errors.UserDeactivatedBanError,
                                errors.AuthKeyUnregisteredError, errors.FrozenMethodInvalidError)
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

        def _clear(account_id, scope):
            """A completed join proves the account is usable for this method now."""
            try:
                clear_account_cooldowns(account_id, scope)
                clear_account_block(account_id, "join")
            except OutreachDenied:
                pass

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
            join_action = "auto_join_invite" if kind == "invite" else "auto_join_public"
            join_scope = cooldown_scope(join_action)

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
                    _clear(account_id, join_scope)
                    outcome = ("joined", "", account_id)
                    break
                except errors.UserAlreadyParticipantError:
                    _clear(account_id, join_scope)
                    outcome = ("already", "", account_id)
                    break
                except errors.ChannelsTooMuchError as exc:
                    # A permanent account ceiling, not a timer.
                    record_provider_restriction(account_id, exc, join_action)
                    if rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", "channel_limit_reached", account_id)
                    break
                except account_limit_errors as exc:
                    record_provider_restriction(account_id, exc, join_action)
                    if rotate:
                        limited.add(account_id)
                        continue
                    outcome = ("stop", type(exc).__name__, account_id)
                    break
                except invalid_link_errors as exc:
                    outcome = ("failed", type(exc).__name__, account_id)
                    break
                except Exception as exc:
                    record_provider_restriction(account_id, exc, join_action)
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
