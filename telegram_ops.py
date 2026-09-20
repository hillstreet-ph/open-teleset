#!/usr/bin/env python3
"""
Telegram Operations — Scraper, Member Adder, Bulk Personal Sender

FastAPI APIRouter providing REST endpoints for:
1. POST /api/scraper/members      — scrape members from channel/group
2. POST /api/members/add          — add members to channel/group
3. POST /api/bulk/send-personal   — send personal messages to many users
"""
import asyncio
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from telethon import functions, types, utils

from account_manager import account_manager
from log_manager import log_manager
from outreach_policy import outreach_policy, dispatch_outreach
from stats_tracker import stats_tracker


telegram_ops_router = APIRouter()


# ============ Request Models ============

class ScrapeRequest(BaseModel):
    account_id: str
    target: str              # channel/group username or ID
    limit: int = 200         # max members to scrape
    filter_bots: bool = True # exclude bots


class AddMembersRequest(BaseModel):
    account_id: str
    source_target: Optional[str] = None   # source channel/group (if scraping first)
    dest_target: str                       # destination channel/group
    usernames: Optional[List[str]] = None  # explicit list of usernames/IDs
    limit: int = Field(default=10, ge=1, le=10)                        # max members to add per run
    delay: float = Field(default=35.0, ge=35, le=3600, allow_inf_nan=False)                    # seconds between each add (Telegram rate limit)
    approval_id: Optional[str] = None      # server-side approval reference


class BulkSendRequest(BaseModel):
    account_id: str
    message: str
    targets: List[str] = Field(min_length=1, max_length=20)           # list of usernames or user IDs
    delay: float = Field(default=3.0, ge=3, le=3600, allow_inf_nan=False)           # seconds between sends
    template_id: Optional[str] = None
    approval_id: Optional[str] = None      # server-side approval reference


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
        for p in participants:
            if request.filter_bots and p.bot:
                continue
            members.append({
                "id": p.id,
                "username": p.username or "",
                "first_name": p.first_name or "",
                "last_name": p.last_name or "",
                "is_bot": p.bot,
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
            "members": members,
        }

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

        user_list = request.usernames[:request.limit]
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

        for i, username in enumerate(user_list):
            try:
                user_entity = await client.get_entity(username)
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
                error_msg = type(e).__name__
                results.append({"username": username, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "MemberAdder", request.account_id,
                    f"Approved member addition failed: {type(e).__name__}", "error",
                )

            # Rate-limit delay (except after the last one)
            if i < len(user_list) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": failed == 0,
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
    Send a personal message from ONE account to MANY individual users.
    This is distinct from the existing batch_send_message which sends
    from MANY accounts to ONE chat.
    """
    try:
        if not request.targets:
            raise HTTPException(status_code=400, detail="At least one target is required")
        if len(request.targets) > 20 or request.delay < 3:
            raise HTTPException(status_code=400, detail="Bulk-send safety bounds exceeded")
        _enforce_outreach(
            "personal_send",
            request.targets,
            request.account_id,
            request.approval_id,
        )

        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400, detail="Client unavailable — account not connected")

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

        results = []
        sent = 0
        failed = 0

        for i, target in enumerate(request.targets):
            try:
                entity = await client.get_entity(target)
                await dispatch_outreach(action="personal_send", subject=utils.get_peer_id(entity), account_id=request.account_id, approval_id=request.approval_id, delay=request.delay, send=lambda: client.send_message(entity, message_text))
                results.append({"target": target, "success": True})
                sent += 1
                stats_tracker.record_message_sent(request.account_id)
                log_manager.add_log(
                    "BulkSend", request.account_id,
                    "Approved personal message sent", "success",
                )
            except Exception as e:
                error_msg = type(e).__name__
                results.append({"target": target, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "BulkSend", request.account_id,
                    f"Approved personal send failed: {type(e).__name__}", "error",
                )

            # Delay between sends (except last)
            if i < len(request.targets) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": failed == 0,
            "account_id": request.account_id,
            "sent": sent,
            "failed": failed,
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
