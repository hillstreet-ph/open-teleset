"""Campaigns: the finder presets an operator saves and reuses.

A campaign is the whole discovery query - keywords plus category, country,
language, type, member threshold and result limit - so the operator does not
rebuild it by hand every time. It is stored with the same encrypted,
operator-scoped store the notes use. Proxy credentials are deliberately not
part of a campaign: they never reach disk.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["campaigns"])

STORE = "campaigns"
MAX_CAMPAIGNS = 100
MAX_NAME = 80
MAX_KEYWORDS = 20000
MAX_COUNTRY = 60
MAX_LANGUAGE = 60
GroupType = Literal["all", "channel", "group"]


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    campaigns = entry.get("campaigns") if isinstance(entry, dict) else None
    return {"campaigns": [c for c in (campaigns or [])
                          if isinstance(c, dict) and c.get("id")]}


def _save(operator: str, record: dict) -> None:
    all_records = _all()
    all_records[operator] = {"campaigns": record["campaigns"][:MAX_CAMPAIGNS]}
    write_secret(STORE, all_records)


def _public(campaign: dict) -> dict:
    return {
        "id": campaign["id"],
        "name": campaign["name"],
        "keywords": campaign["keywords"],
        "category": campaign["category"],
        "country": campaign["country"],
        "language": campaign["language"],
        "group_type": campaign["group_type"],
        "min_members": campaign["min_members"],
        "limit": campaign["limit"],
        "created": campaign.get("created"),
        "updated": campaign.get("updated"),
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_campaigns(operator: str) -> dict:
    record = _record(operator)
    return {"campaigns": [_public(c) for c in record["campaigns"]],
            "capacity": {"used": len(record["campaigns"]), "limit": MAX_CAMPAIGNS}}


def get_campaign(operator: str, campaign_id: str) -> dict:
    campaign = next((c for c in _record(operator)["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(404, "No campaign with that id.")
    return _public(campaign)


def add_campaign(operator: str, fields: dict) -> dict:
    name = fields["name"].strip()
    if not name:
        raise HTTPException(400, "Give the campaign a name before saving it.")
    record = _record(operator)
    if len(record["campaigns"]) >= MAX_CAMPAIGNS:
        raise HTTPException(409, f"Campaigns hold {MAX_CAMPAIGNS} entries. "
                                "Delete one before adding another.")
    campaign = {
        "id": uuid4().hex[:12],
        **fields,
        "name": name,
        "created": _now(),
        "updated": _now(),
    }
    record["campaigns"].insert(0, campaign)
    _save(operator, record)
    return _public(campaign)


def update_campaign(operator: str, campaign_id: str, fields: dict) -> dict:
    record = _record(operator)
    campaign = next((c for c in record["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(404, "No campaign with that id.")
    if "name" in fields:
        name = (fields.pop("name") or "").strip()
        if not name:
            raise HTTPException(400, "Give the campaign a name before saving it.")
        campaign["name"] = name
    campaign.update(fields)
    campaign["updated"] = _now()
    _save(operator, record)
    return _public(campaign)


def delete_campaign(operator: str, campaign_id: str) -> bool:
    record = _record(operator)
    kept = [c for c in record["campaigns"] if c["id"] != campaign_id]
    if len(kept) == len(record["campaigns"]):
        return False
    record["campaigns"] = kept
    _save(operator, record)
    return True


class CampaignBody(BaseModel):
    name: str = Field(default="", max_length=MAX_NAME)
    keywords: str = Field(default="", max_length=MAX_KEYWORDS)
    category: str = Field(default="all", max_length=40)
    country: str = Field(default="", max_length=MAX_COUNTRY)
    language: str = Field(default="", max_length=MAX_LANGUAGE)
    group_type: GroupType = "all"
    min_members: int = Field(default=0, ge=0, le=10_000_000)
    limit: int = Field(default=300, ge=1, le=10000)


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_NAME)
    keywords: Optional[str] = Field(default=None, max_length=MAX_KEYWORDS)
    category: Optional[str] = Field(default=None, max_length=40)
    country: Optional[str] = Field(default=None, max_length=MAX_COUNTRY)
    language: Optional[str] = Field(default=None, max_length=MAX_LANGUAGE)
    group_type: Optional[GroupType] = None
    min_members: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    limit: Optional[int] = Field(default=None, ge=1, le=10000)


@router.get("/api/campaigns")
async def campaigns_list(request: Request):
    return {"success": True, **list_campaigns(_operator(request))}


@router.post("/api/campaigns")
async def campaign_create(body: CampaignBody, request: Request):
    return {"success": True,
            "campaign": add_campaign(_operator(request), body.model_dump())}


@router.get("/api/campaigns/{campaign_id}")
async def campaign_read(campaign_id: str, request: Request):
    return {"success": True,
            "campaign": get_campaign(_operator(request), campaign_id)}


@router.put("/api/campaigns/{campaign_id}")
async def campaign_edit(campaign_id: str, body: CampaignUpdate, request: Request):
    # A partial body only changes the fields it names.
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    return {"success": True,
            "campaign": update_campaign(_operator(request), campaign_id, fields)}


@router.delete("/api/campaigns/{campaign_id}")
async def campaign_delete(campaign_id: str, request: Request):
    if not delete_campaign(_operator(request), campaign_id):
        raise HTTPException(404, "No campaign with that id.")
    return {"success": True}
