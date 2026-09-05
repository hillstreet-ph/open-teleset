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
from pydantic import BaseModel
from telethon import functions, types

from account_manager import account_manager
from log_manager import log_manager
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
    limit: int = 50                        # max members to add per run
    delay: float = 35.0                    # seconds between each add (Telegram rate limit)


class BulkSendRequest(BaseModel):
    account_id: str
    message: str
    targets: List[str]           # list of usernames or user IDs
    delay: float = 3.0           # seconds between sends
    template_id: Optional[str] = None


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
            f"Scrape failed for {request.target}: {str(e)}", "error",
        )
        raise HTTPException(status_code=500, detail=str(e))


# ============ 2. Member Adder ============

@telegram_ops_router.post("/api/members/add")
async def add_members(request: AddMembersRequest):
    """
    Add members to a channel or group.
    Supports either an explicit usernames list or scraping from a source first.
    """
    try:
        client = await account_manager.get_client(request.account_id)
        if not client:
            raise HTTPException(status_code=400, detail="Client unavailable — account not connected")

        # Resolve the list of users to add
        user_list: List[str] = []
        if request.usernames:
            user_list = request.usernames[:request.limit]
        elif request.source_target:
            # Scrape source first
            source_entity = await client.get_entity(request.source_target)
            participants = await client.get_participants(source_entity, limit=request.limit)
            for p in participants:
                if not p.bot and p.username:
                    user_list.append(p.username)
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide 'usernames' list or 'source_target' to scrape from",
            )

        if not user_list:
            return {"success": True, "added": 0, "failed": 0, "results": [], "message": "No users to add"}

        dest_entity = await client.get_entity(request.dest_target)

        results = []
        added = 0
        failed = 0

        for i, username in enumerate(user_list):
            try:
                user_entity = await client.get_entity(username)
                await client(
                    functions.channels.InviteToChannelRequest(
                        channel=dest_entity,
                        users=[user_entity],
                    )
                )
                results.append({"username": username, "success": True})
                added += 1
                log_manager.add_log(
                    "MemberAdder", request.account_id,
                    f"Added {username} to {request.dest_target}", "success",
                )
            except Exception as e:
                error_msg = str(e)
                results.append({"username": username, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "MemberAdder", request.account_id,
                    f"Failed to add {username}: {error_msg}", "error",
                )

            # Rate-limit delay (except after the last one)
            if i < len(user_list) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": True,
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
            f"Add members failed: {str(e)}", "error",
        )
        raise HTTPException(status_code=500, detail=str(e))


# ============ 3. Bulk Personal Message Sender ============

@telegram_ops_router.post("/api/bulk/send-personal")
async def bulk_send_personal(request: BulkSendRequest):
    """
    Send a personal message from ONE account to MANY individual users.
    This is distinct from the existing batch_send_message which sends
    from MANY accounts to ONE chat.
    """
    try:
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
                await client.send_message(entity, message_text)
                results.append({"target": target, "success": True})
                sent += 1
                stats_tracker.record_message_sent(request.account_id)
                log_manager.add_log(
                    "BulkSend", request.account_id,
                    f"Sent to {target}", "success",
                )
            except Exception as e:
                error_msg = str(e)
                results.append({"target": target, "success": False, "error": error_msg})
                failed += 1
                log_manager.add_log(
                    "BulkSend", request.account_id,
                    f"Failed to send to {target}: {error_msg}", "error",
                )

            # Delay between sends (except last)
            if i < len(request.targets) - 1:
                await asyncio.sleep(request.delay)

        return {
            "success": True,
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
            f"Bulk send failed: {str(e)}", "error",
        )
        raise HTTPException(status_code=500, detail=str(e))
