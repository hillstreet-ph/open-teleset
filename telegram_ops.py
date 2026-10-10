#!/usr/bin/env python3
"""
Telegram Operations — Scraper, Member Adder, Bulk Personal Sender

FastAPI APIRouter providing REST endpoints for:
1. POST /api/scraper/members      — scrape members from channel/group
2. POST /api/members/add          — add members to channel/group
3. POST /api/bulk/send-personal   — send personal messages to many users
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from telethon import errors, functions, types, utils

from account_manager import account_manager
from log_manager import log_manager
from outreach_policy import OutreachDenied, outreach_policy, dispatch_outreach, record_provider_restriction
from stats_tracker import stats_tracker


telegram_ops_router = APIRouter()
ActivityFilter = Literal["all", "recently", "7_days", "15_days", "30_days"]


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


class AddMembersRequest(BaseModel):
    account_id: str
    source_target: Optional[str] = None   # source channel/group (if scraping first)
    dest_target: str                       # destination channel/group
    usernames: Optional[List[str]] = None  # explicit list of usernames/IDs
    filter_bots: bool = True
    activity_filter: ActivityFilter = "all"
    limit: int = Field(default=10, ge=1, le=10)                        # max members to add per run
    delay: float = Field(default=35.0, ge=35, le=3600, allow_inf_nan=False)                    # seconds between each add (Telegram rate limit)
    approval_id: Optional[str] = None      # server-side approval reference


class BulkSendRequest(BaseModel):
    account_id: str = ""
    message: str = Field(default="", max_length=4096)
    messages: dict[str, str] = Field(default_factory=dict, max_length=20)
    filter_bots: bool = True
    targets: List[str] = Field(min_length=1, max_length=20)           # list of usernames or user IDs
    delay: float = Field(default=3.0, ge=3, le=3600, allow_inf_nan=False)           # seconds between sends
    template_id: Optional[str] = None
    approval_id: Optional[str] = None      # server-side approval reference
    activity_filter: ActivityFilter = "all"
    rotation_mode: Literal["single", "round_robin"] = "single"
    account_ids: List[str] = Field(default_factory=list, max_length=5)
    per_account_limit: int = Field(default=20, ge=1, le=20, strict=True)

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
            raise ValueError("Rotation requires 1–5 unique account_ids and no account_id")
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

@telegram_ops_router.post("/api/scraper/members")
async def scrape_members(request: ScrapeRequest):
    """
    Scrape members from a Telegram channel or group.
    Returns list of {id, username, first_name, last_name, is_bot}.
    """
    try:
        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400, detail="Client unavailable — account not connected")

        entity = await client.get_entity(request.target)

        participants = await client.get_participants(entity, limit=request.limit)

        members = []
        now = datetime.now(timezone.utc)
        for p in participants:
            if request.filter_bots and p.bot:
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
    except Exception as e:
        log_manager.add_log(
            "Scraper", request.account_id,
            f"Scrape failed for {request.target}: {type(e).__name__}", "error",
        )
        raise HTTPException(status_code=500, detail=type(e).__name__)


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
        _enforce_outreach("member_add", user_list, request.account_id, request.approval_id)

        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400, detail="Client unavailable — account not connected")

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
async def bulk_send_personal(request: BulkSendRequest):
    """
    Send once per consenting recipient using one account or an explicit pool.
    Rotation never retries a recipient on another account after a restriction.
    """
    try:
        if not request.targets:
            raise HTTPException(status_code=400, detail="At least one target is required")
        if len(request.targets) > 20 or request.delay < 3:
            raise HTTPException(status_code=400, detail="Bulk-send safety bounds exceeded")
        targets = list(dict.fromkeys(t.strip().lower().lstrip("@") for t in request.targets))
        if not all(targets):
            raise HTTPException(status_code=400, detail="Recipient cannot be empty")
        account_ids = request.selected_accounts()
        if len(targets) > len(account_ids) * request.per_account_limit:
            raise HTTPException(status_code=400, detail="Per-account run limit exceeded")
        for account_id in account_ids:
            _enforce_outreach("personal_send", targets, account_id, request.approval_id)
        clients = {}
        for account_id in account_ids:
            clients[account_id] = await account_manager.get_client(account_id)
            if not clients[account_id]:
                raise HTTPException(status_code=400, detail="Selected account unavailable; no messages sent")

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

        for i, target in enumerate(targets):
            account_id = account_ids[i % len(account_ids)]
            client = clients[account_id]
            try:
                entity = await client.get_entity(int(target) if target.isdigit() else target)
                peer_id = utils.get_peer_id(entity)
                if peer_id in peers_seen:
                    results.append({"target": target, "account_id": account_id, "success": False, "skipped": "duplicate_recipient"})
                    skipped += 1
                    continue
                peers_seen.add(peer_id)
                if request.filter_bots and getattr(entity, "bot", False):
                    results.append({"target": target, "account_id": account_id, "success": False, "skipped": "bot_excluded"})
                    skipped += 1
                    continue
                if not matches_activity(entity, request.activity_filter):
                    results.append({"target": target, "account_id": account_id, "success": False, "skipped": "activity_not_matched"})
                    skipped += 1
                    continue
                await dispatch_outreach(action="personal_send", subject=peer_id,
                    account_id=account_id, approval_id=request.approval_id, delay=request.delay,
                    send=lambda: client.send_message(entity, request.messages.get(target, message_text)))
                results.append({"target": target, "account_id": account_id, "success": True})
                sent += 1
                stats_tracker.record_message_sent(account_id)
                log_manager.add_log(
                    "BulkSend", account_id,
                    "Approved personal message sent", "success",
                )
            except Exception as e:
                error_msg = str(e) if isinstance(e, OutreachDenied) else type(e).__name__
                record_provider_restriction(account_id, e)
                results.append({"target": target, "account_id": account_id, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "BulkSend", account_id,
                    f"Approved personal send failed: {type(e).__name__}", "error",
                )
                if isinstance(e, (errors.FloodWaitError, errors.PeerFloodError,
                                  errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                                  errors.FrozenMethodInvalidError, OutreachDenied)):
                    stopped_reason = error_msg
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
