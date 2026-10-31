"""Administrator-triggered account factory automation.

The dashboard's Automation tab calls these endpoints, which forward a signed
request to the n8n webhook that runs the Telegram account factory (HeroSMS
purchase -> registration service -> session import -> Airtable). n8n owns the
credentials and the long-running workflow; this module only authenticates the
operator, holds the shared webhook token, and keeps a small run history.

Configuration (backend environment):
- N8N_AUTOMATION_WEBHOOK_URL   production webhook URL of the n8n workflow
- N8N_AUTOMATION_WEBHOOK_TOKEN shared secret, also stored in n8n Variables as
                               AUTOMATION_WEBHOOK_TOKEN
- AUTOMATION_COOLDOWN_SECONDS  minimum gap between accepted runs (default 600)
- AUTOMATION_DEFAULT_MAX_PRICE / AUTOMATION_DEFAULT_OTP_ATTEMPTS  UI defaults
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from log_manager import log_manager

router = APIRouter()

RUN_HISTORY_LIMIT = 25
DEFAULT_COOLDOWN_SECONDS = 600
WEBHOOK_TIMEOUT_SECONDS = 15.0

_runs: deque[dict[str, Any]] = deque(maxlen=RUN_HISTORY_LIMIT)
_last_accepted = 0.0


class AutomationRunRequest(BaseModel):
    max_price: float | None = Field(default=None, ge=0.02, le=5.0)
    otp_attempts: int | None = Field(default=None, ge=1, le=120)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _cooldown_seconds() -> int:
    try:
        value = int(os.getenv("AUTOMATION_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS
    return min(max(value, 0), 3600)


def _config() -> dict[str, Any]:
    url = os.getenv("N8N_AUTOMATION_WEBHOOK_URL", "").strip()
    token = os.getenv("N8N_AUTOMATION_WEBHOOK_TOKEN", "").strip()
    parsed = urlsplit(url)
    problem: str | None = None
    if not url:
        problem = "Set N8N_AUTOMATION_WEBHOOK_URL on the backend to the n8n production webhook."
    elif parsed.scheme not in {"http", "https"} or not parsed.netloc:
        problem = "N8N_AUTOMATION_WEBHOOK_URL is not a valid http(s) URL."
    elif parsed.scheme != "https" and os.getenv("APP_ENV") == "production":
        problem = "N8N_AUTOMATION_WEBHOOK_URL must use HTTPS in production."
    elif not token:
        problem = "Set N8N_AUTOMATION_WEBHOOK_TOKEN on the backend (same value as the n8n Variable)."
    return {
        "configured": problem is None,
        "problem": problem,
        "url": url,
        "token": token,
        "host": parsed.netloc,
    }


def _defaults() -> dict[str, float | int]:
    try:
        max_price = float(os.getenv("AUTOMATION_DEFAULT_MAX_PRICE", "0.5"))
    except ValueError:
        max_price = 0.5
    try:
        otp_attempts = int(os.getenv("AUTOMATION_DEFAULT_OTP_ATTEMPTS", "36"))
    except ValueError:
        otp_attempts = 36
    return {"max_price": max_price, "otp_attempts": otp_attempts}


def _cooldown_remaining() -> int:
    cooldown = _cooldown_seconds()
    if cooldown <= 0 or not _last_accepted:
        return 0
    return max(0, math.ceil(cooldown - (time.monotonic() - _last_accepted)))


def _record(run: dict[str, Any]) -> None:
    _runs.appendleft(run)


@router.get("/api/automation/status")
async def automation_status(request: Request):
    """Readiness of the account factory plus the recent run history."""
    config = _config()
    return {
        "configured": config["configured"],
        "problem": config["problem"],
        "webhook_host": config["host"] or None,
        "cooldown_seconds": _cooldown_seconds(),
        "cooldown_remaining": _cooldown_remaining(),
        "defaults": _defaults(),
        "last_run": _runs[0] if _runs else None,
        "runs": list(_runs),
    }


@router.post("/api/automation/run")
async def automation_run(request: Request, body: AutomationRunRequest | None = None):
    """Start one account-factory run through the n8n webhook."""
    config = _config()
    if not config["configured"]:
        raise HTTPException(503, config["problem"] or "Automation is not configured.")

    remaining = _cooldown_remaining()
    if remaining:
        raise HTTPException(
            429,
            f"An account-factory run was accepted recently. Try again in {remaining}s.",
            headers={"Retry-After": str(remaining)},
        )

    user = getattr(request.state, "user", None)
    requested_by = str(getattr(user, "id", None) or "dashboard")
    payload = {
        "source": "open-teleset",
        "run_id": f"run_{uuid4().hex}",
        "requested_by": requested_by,
        "requested_at": _now_iso(),
    }
    if body and body.max_price is not None:
        payload["max_price"] = body.max_price
    if body and body.otp_attempts is not None:
        payload["otp_attempts"] = body.otp_attempts

    record = {
        "run_id": payload["run_id"],
        "requested_by": requested_by,
        "requested_at": payload["requested_at"],
        "max_price": payload.get("max_price"),
        "otp_attempts": payload.get("otp_attempts"),
        "status": "failed",
        "upstream_status": None,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                config["url"],
                json=payload,
                headers={
                    "X-Automation-Token": config["token"],
                    "Accept": "application/json",
                    "User-Agent": "open-teleset-automation/1",
                },
            )
    except httpx.TimeoutException:
        record["error"] = "n8n did not answer in time"
        _record(record)
        log_manager.add_log("Automation", "account-factory", f"{record['run_id']} timed out", "error")
        raise HTTPException(
            504,
            "n8n did not answer in time. Check the workflow and the webhook URL.",
        ) from None
    except httpx.HTTPError:
        record["error"] = "n8n unreachable"
        _record(record)
        log_manager.add_log("Automation", "account-factory", f"{record['run_id']} unreachable", "error")
        raise HTTPException(
            502,
            "Could not reach n8n. Check N8N_AUTOMATION_WEBHOOK_URL and that the workflow is published.",
        ) from None

    record["upstream_status"] = response.status_code
    if response.status_code in {401, 403}:
        record["error"] = "webhook token rejected"
        _record(record)
        log_manager.add_log("Automation", "account-factory", f"{record['run_id']} token rejected", "error")
        raise HTTPException(
            502,
            "n8n rejected the shared token. Make sure AUTOMATION_WEBHOOK_TOKEN matches in n8n and on the backend.",
        )
    if response.status_code >= 400:
        record["error"] = f"n8n responded with HTTP {response.status_code}"
        _record(record)
        log_manager.add_log(
            "Automation", "account-factory", f"{record['run_id']} HTTP {response.status_code}", "error"
        )
        raise HTTPException(
            502,
            f"n8n did not accept the request (HTTP {response.status_code}). "
            "Check the workflow and the webhook URL.",
        )

    global _last_accepted
    _last_accepted = time.monotonic()
    record["status"] = "accepted"
    _record(record)
    log_manager.add_log("Automation", "account-factory", f"{record['run_id']} accepted", "success")
    return {
        "accepted": True,
        "run_id": record["run_id"],
        "requested_at": record["requested_at"],
        "max_price": record.get("max_price"),
        "otp_attempts": record.get("otp_attempts"),
        "cooldown_seconds": _cooldown_seconds(),
    }
