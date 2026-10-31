"""Operator-facing dashboard features.

Covers the settings that support the dashboard's AI, contacts, session,
notification, integration, recovery-vault and automation surfaces. Secrets are
write-only: they are encrypted at rest and list endpoints return only whether a
secret exists.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import re
import struct
import time
from datetime import datetime, timezone
from typing import List, Literal, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from telethon import functions, types

from account_manager import account_manager
from log_manager import log_manager
from open_teleset.operator_store import (
    feature_enabled,
    get_features,
    read_json,
    read_secret,
    secret_key_available,
    set_features,
    write_json,
    write_secret,
)
from open_teleset.security import SupabaseAuthMiddleware  # noqa: F401  (import keeps auth settings loaded)
from open_teleset.telegram_notify import (
    get_settings as get_notification_settings,
    save_settings as save_notification_settings,
    send_message as send_telegram_message,
)
from proxy_manager import proxy_manager
from telegram_ops import _client_unavailable_detail, activity_details
from template_manager import template_manager
from webhooks import _validate_url

router = APIRouter()

PROVIDERS = {
    "open_connect": {
        "label": "Open Connect",
        "base_url": "https://open-connect.site/v1",
        "models": ["open-connect/auto"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "google/gemini-2.0-flash-001"],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
    },
    "google": {
        "label": "Google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro"],
    },
    "custom": {"label": "Custom (OpenAI-compatible)", "base_url": "", "models": []},
}

RECOVERY_FIELDS = ("login_email", "email_password", "twofa_password", "otp_secret", "backup_codes", "note")


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _client_or_400(account_id: str):
    if account_id not in account_manager.accounts:
        raise HTTPException(404, "Account does not exist")
    return account_id


async def _client(account_id: str):
    _client_or_400(account_id)
    client = await account_manager.get_client(account_id)
    if not client:
        raise HTTPException(400, _client_unavailable_detail(account_id))
    return client


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    return None


# ============ AI providers ============


def _ai_config(operator: str) -> dict:
    return read_json("ai_settings", {}).get(operator, {})


def _ai_keys(operator: str) -> dict:
    return read_secret("ai_keys", {}).get(operator, {}) or {}


def _public_ai_settings(operator: str) -> dict:
    config = _ai_config(operator)
    keys = _ai_keys(operator)
    provider = config.get("provider", "open_connect")
    preset = PROVIDERS.get(provider, PROVIDERS["custom"])
    return {
        "provider": provider,
        "base_url": config.get("base_url") or preset["base_url"],
        "model": config.get("model") or (preset["models"][0] if preset["models"] else ""),
        "has_key": bool(keys.get(provider)),
        "configured_providers": sorted(name for name, value in keys.items() if value),
        "providers": [
            {"id": name, **value}
            for name, value in PROVIDERS.items()
        ],
        "updated_at": config.get("updated_at"),
        "encryption_ready": secret_key_available(),
    }


class AISettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=120)
    api_key: Optional[str] = Field(default=None, max_length=500)
    clear_key: bool = False


def _resolve_ai(provider: str, base_url: str, model: str) -> tuple[str, str]:
    if provider not in PROVIDERS:
        raise HTTPException(400, "Choose a supported AI provider")
    preset = PROVIDERS[provider]
    resolved_base = (base_url or preset["base_url"]).strip().rstrip("/")
    if not resolved_base:
        raise HTTPException(400, "A Base URL is required for a custom provider")
    try:
        _validate_url(resolved_base)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    resolved_model = (model or (preset["models"][0] if preset["models"] else "")).strip()
    if not resolved_model:
        raise HTTPException(400, "A model name is required")
    return resolved_base, resolved_model


@router.get("/api/ai/settings")
async def ai_settings(request: Request):
    return _public_ai_settings(_operator(request))


@router.put("/api/ai/settings")
async def update_ai_settings(body: AISettingsUpdate, request: Request):
    operator = _operator(request)
    base_url, model = _resolve_ai(body.provider, body.base_url, body.model)
    if body.api_key and not secret_key_available():
        raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; API keys cannot be stored safely.")
    all_config = read_json("ai_settings", {})
    config = all_config.get(operator, {})
    config.update({
        "provider": body.provider,
        "base_url": base_url,
        "model": model,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    all_config[operator] = config
    write_json("ai_settings", all_config)
    if body.api_key is not None or body.clear_key:
        secrets = read_secret("ai_keys", {}) or {}
        provider_keys = secrets.get(operator, {})
        if body.api_key:
            provider_keys[body.provider] = body.api_key.strip()
        elif body.clear_key:
            provider_keys.pop(body.provider, None)
        secrets[operator] = provider_keys
        write_secret("ai_keys", secrets)
    return _public_ai_settings(operator)


class AITestRequest(BaseModel):
    provider: Optional[str] = None
    base_url: str = ""
    model: str = ""
    api_key: Optional[str] = None


@router.post("/api/ai/test")
async def test_ai_provider(body: AITestRequest, request: Request):
    operator = _operator(request)
    config = _ai_config(operator)
    provider = body.provider or config.get("provider", "open_connect")
    base_url, model = _resolve_ai(provider, body.base_url or config.get("base_url", ""), body.model or config.get("model", ""))
    key = (body.api_key or _ai_keys(operator).get(provider) or "").strip()
    if not key:
        raise HTTPException(400, "Add an API key for this provider first")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(f"{base_url}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Provider unreachable ({type(exc).__name__})") from None
    if response.status_code == 200:
        models = []
        try:
            payload = response.json()
            models = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")][:12]
        except ValueError:
            pass
        return {"success": True, "detail": "Provider accepted the API key.", "models": models}
    if response.status_code in (401, 403):
        raise HTTPException(400, "Provider rejected the API key.")
    # Some OpenAI-compatible gateways do not expose /models; a minimal completion
    # is the definitive check.
    try:
        reply = await _ai_chat(base_url, key, model, [{"role": "user", "content": "Reply with OK"}], max_tokens=8)
    except HTTPException as exc:
        raise HTTPException(400, f"Provider check failed: {exc.detail}") from None
    return {"success": True, "detail": f"Provider replied: {reply[:40]}", "models": []}


async def _ai_chat(base_url: str, key: str, model: str, messages: list, max_tokens: int = 800) -> str:
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Provider unreachable ({type(exc).__name__})") from None
    if response.status_code in (401, 403):
        raise HTTPException(400, "Provider rejected the API key.")
    if response.status_code >= 400:
        raise HTTPException(400, f"Provider returned HTTP {response.status_code}.")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(502, "Provider returned an unexpected response.") from None
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(502, "Provider returned an empty response.")
    return content.strip()


class RewriteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    instruction: str = Field(default="", max_length=500)
    provider: Optional[str] = None
    base_url: str = ""
    model: str = ""


@router.post("/api/ai/rewrite")
async def rewrite_text(body: RewriteRequest, request: Request):
    operator = _operator(request)
    config = _ai_config(operator)
    provider = body.provider or config.get("provider", "open_connect")
    base_url, model = _resolve_ai(provider, body.base_url or config.get("base_url", ""), body.model or config.get("model", ""))
    key = (_ai_keys(operator).get(provider) or "").strip()
    if not key:
        raise HTTPException(503, "Configure an AI provider and API key in Settings → AI providers.")
    system = (
        "You rewrite Telegram outreach copy for a professional operator. Keep every {placeholder} exactly as "
        "written, preserve the original language, remove hype, and return only the rewritten message."
    )
    if body.instruction:
        system += f" Additional instruction: {body.instruction}"
    text = await _ai_chat(base_url, key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": body.text},
    ])
    return {"success": True, "text": text, "provider": provider, "model": model}


# ============ Templates ============


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    content: Optional[str] = Field(default=None, max_length=8000)
    category: Optional[str] = Field(default=None, max_length=60)


@router.put("/api/templates/{template_id}")
async def update_template(template_id: str, body: TemplateUpdate):
    if not template_manager.update_template(template_id, name=body.name, content=body.content, category=body.category):
        raise HTTPException(404, "Template does not exist")
    log_manager.add_log("Template", template_id, "Template updated", "success")
    return {"success": True}


# ============ Contacts ============


def _public_contact(user) -> dict:
    details = activity_details(user)
    return {
        "id": user.id,
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "username": getattr(user, "username", "") or "",
        "phone": getattr(user, "phone", "") or "",
        "bot": bool(getattr(user, "bot", False)),
        "deleted": bool(getattr(user, "deleted", False)),
        "mutual": bool(getattr(user, "mutual_contact", False)),
        **details,
    }


async def _contacts(client) -> list:
    result = await client(functions.contacts.GetContactsRequest(hash=0))
    return [_public_contact(user) for user in getattr(result, "users", []) if not getattr(user, "deleted", False)]


@router.get("/api/accounts/{account_id}/contacts")
async def list_contacts(account_id: str, q: str = ""):
    client = await _client(account_id)
    try:
        contacts = await _contacts(client)
    except Exception:
        raise HTTPException(502, "Telegram did not return the contact list. Try again.") from None
    term = (q or "").strip().lower()
    if term:
        contacts = [c for c in contacts if term in " ".join(
            str(c.get(key, "")) for key in ("id", "first_name", "last_name", "username", "phone")
        ).lower()]
    contacts.sort(key=lambda item: (item.get("first_name") or item.get("username") or "").lower())
    return {"success": True, "contacts": contacts, "total": len(contacts)}


@router.get("/api/accounts/{account_id}/contacts/export")
async def export_contacts(account_id: str):
    client = await _client(account_id)
    try:
        contacts = await _contacts(client)
    except Exception:
        raise HTTPException(502, "Telegram did not return the contact list. Try again.") from None
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "first_name", "last_name", "username", "phone", "mutual"])
    for contact in contacts:
        writer.writerow([contact["id"], contact["first_name"], contact["last_name"],
                         contact["username"], contact["phone"], contact["mutual"]])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="telegram-contacts.csv"'},
    )


class ContactInput(BaseModel):
    phone: str = Field(min_length=5, max_length=24)
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)

    @property
    def normalised_phone(self) -> str:
        phone = re.sub(r"[\s()\-]", "", self.phone)
        if not re.fullmatch(r"\+?\d{5,20}", phone):
            raise ValueError("Enter a phone number with country code")
        return phone


def _contact_batches(contacts: List[ContactInput]):
    batch = []
    for contact in contacts:
        try:
            batch.append(types.InputPhoneContact(
                client_id=len(batch) + 1,
                phone=contact.normalised_phone,
                first_name=contact.first_name.strip()[:80],
                last_name=contact.last_name.strip()[:80],
            ))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if len(batch) == 100:
            yield batch
            batch = []
    if batch:
        yield batch


@router.post("/api/accounts/{account_id}/contacts")
async def add_contact(account_id: str, body: ContactInput):
    client = await _client(account_id)
    batch = next(_contact_batches([body]))
    try:
        result = await client(functions.contacts.ImportContactsRequest(contacts=batch))
    except Exception:
        raise HTTPException(502, "Telegram did not accept the contact. Check the number and try again.") from None
    imported = len(getattr(result, "imported", []) or [])
    users = getattr(result, "users", []) or []
    return {"success": imported > 0, "imported": imported,
            "contact": _public_contact(users[0]) if users else None}


class ContactImport(BaseModel):
    contacts: List[ContactInput] = Field(default_factory=list, max_length=2000)


@router.post("/api/accounts/{account_id}/contacts/import")
async def import_contacts(account_id: str, body: ContactImport):
    if not body.contacts:
        raise HTTPException(400, "Add at least one contact")
    client = await _client(account_id)
    imported = 0
    retried = 0
    for batch in _contact_batches(body.contacts):
        try:
            result = await client(functions.contacts.ImportContactsRequest(contacts=batch))
        except Exception:
            raise HTTPException(502, "Telegram stopped accepting contacts. Check the file and try again.") from None
        imported += len(getattr(result, "imported", []) or [])
        retried += len(getattr(result, "retry_contacts", []) or [])
    return {"success": True, "imported": imported, "retry": retried, "total": len(body.contacts)}


@router.delete("/api/accounts/{account_id}/contacts/{user_id}")
async def delete_contact(account_id: str, user_id: int):
    client = await _client(account_id)
    try:
        entity = await client.get_input_entity(user_id)
        await client(functions.contacts.DeleteContactsRequest(id=[entity]))
    except Exception:
        raise HTTPException(502, "Telegram did not delete the contact. Try again.") from None
    return {"success": True}


class BlockRequest(BaseModel):
    blocked: bool = True


@router.post("/api/accounts/{account_id}/contacts/{user_id}/block")
async def block_contact(account_id: str, user_id: int, body: BlockRequest):
    client = await _client(account_id)
    try:
        entity = await client.get_input_entity(user_id)
        if body.blocked:
            await client(functions.contacts.BlockRequest(id=entity))
        else:
            await client(functions.contacts.UnblockRequest(id=entity))
    except Exception:
        raise HTTPException(502, "Telegram did not change the block state. Try again.") from None
    return {"success": True, "blocked": body.blocked}


# ============ Login sessions and profile photo ============


@router.get("/api/accounts/{account_id}/sessions")
async def list_sessions(account_id: str):
    client = await _client(account_id)
    try:
        result = await client(functions.account.GetAuthorizationsRequest())
    except Exception:
        raise HTTPException(502, "Telegram did not return login activity. Try again.") from None
    sessions = []
    for item in getattr(result, "authorizations", []) or []:
        sessions.append({
            "hash": item.hash.hex() if isinstance(item.hash, bytes) else str(item.hash),
            "device": getattr(item, "device_model", "") or "",
            "platform": getattr(item, "platform", "") or "",
            "system": getattr(item, "system_version", "") or "",
            "app": " ".join(filter(None, [getattr(item, "app_name", ""), getattr(item, "app_version", "")])),
            "ip": getattr(item, "ip", "") or "",
            "country": getattr(item, "country", "") or "",
            "region": getattr(item, "region", "") or "",
            "created": _iso(getattr(item, "date_created", None)),
            "active": _iso(getattr(item, "date_active", None)),
            "current": bool(getattr(item, "current", False)),
        })
    sessions.sort(key=lambda item: (not item["current"], item["active"] or ""), reverse=False)
    return {"success": True, "sessions": sessions}


class RevokeSession(BaseModel):
    hash: str = Field(min_length=2, max_length=64)


@router.post("/api/accounts/{account_id}/sessions/revoke")
async def revoke_session(account_id: str, body: RevokeSession):
    client = await _client(account_id)
    value = body.hash.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{2,64}", value) or len(value) % 2:
        raise HTTPException(400, "Invalid session reference")
    try:
        await client(functions.account.ResetAuthorizationRequest(
            hash=int.from_bytes(bytes.fromhex(value), "little")))
    except Exception:
        raise HTTPException(502, "Telegram did not end that session. It may already be gone.") from None
    log_manager.add_log("Account", account_id, "Signed out a Telegram login session", "warning")
    return {"success": True}


@router.get("/api/accounts/{account_id}/photo")
async def account_photo(account_id: str):
    client = await _client(account_id)
    try:
        data = await client.download_profile_photo("me", file=bytes)
    except Exception:
        data = None
    if not data:
        raise HTTPException(404, "No profile photo")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, no-store"})


@router.post("/api/accounts/{account_id}/photo")
async def upload_account_photo(account_id: str, photo: UploadFile = File(...)):
    client = await _client(account_id)
    content_type = (photo.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(400, "Upload a JPEG or PNG image")
    data = await photo.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Profile photo must be 5 MB or smaller")
    try:
        uploaded = await client.upload_file(data, file_name=photo.filename or "profile.jpg")
        await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
    except Exception:
        raise HTTPException(502, "Telegram did not accept the photo. Use a square JPEG or PNG under 5 MB.") from None
    log_manager.add_log("Account", account_id, "Profile photo updated", "success")
    return {"success": True}


# ============ Platform feature toggles ============


@router.get("/api/platform/features")
async def platform_features():
    return get_features()


class FeatureUpdate(BaseModel):
    automation_enabled: Optional[bool] = None
    factory_enabled: Optional[bool] = None
    cloud_browser_enabled: Optional[bool] = None


@router.put("/api/platform/features")
async def update_platform_features(body: FeatureUpdate):
    return set_features(body.model_dump(exclude_none=True))


# ============ MCP / app integrations ============


def _public_integration(item: dict) -> dict:
    return {key: item.get(key) for key in ("id", "name", "base_url", "transport", "has_token", "created_at")}


def _integrations() -> list:
    items = read_json("integrations", [])
    return items if isinstance(items, list) else []


class IntegrationInput(BaseModel):
    id: Optional[str] = Field(default=None, max_length=60)
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=8, max_length=500)
    transport: Literal["http", "sse"] = "http"
    api_key: Optional[str] = Field(default=None, max_length=800)


@router.get("/api/integrations/mcp")
async def list_integrations(request: Request):
    return {
        "integrations": [_public_integration(item) for item in _integrations()],
        "suggested": [
            {"name": "Open Connect", "base_url": "https://open-connect.site/mcp"},
            {"name": "Composio", "base_url": "https://mcp.composio.dev"},
            {"name": "Airtable", "base_url": "https://mcp.airtable.com/mcp"},
            {"name": "n8n", "base_url": "https://your-instance.n8n.cloud/mcp-server/http"},
        ],
    }


@router.put("/api/integrations/mcp")
async def save_integration(body: IntegrationInput, request: Request):
    try:
        base_url = _validate_url(body.base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    items = _integrations()
    integration_id = body.id or f"mcp_{int(time.time() * 1000)}"
    tokens = read_secret("integration_tokens", {}) or {}
    existing = next((item for item in items if item.get("id") == integration_id), None)
    record = {
        "id": integration_id,
        "name": body.name.strip(),
        "base_url": base_url,
        "transport": body.transport,
        "has_token": bool(existing and existing.get("has_token")),
        "created_at": (existing or {}).get("created_at") or datetime.now(timezone.utc).isoformat(),
    }
    if body.api_key is not None:
        if body.api_key and not secret_key_available():
            raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; tokens cannot be stored safely.")
        if body.api_key:
            tokens[integration_id] = body.api_key.strip()
            record["has_token"] = True
        else:
            tokens.pop(integration_id, None)
        write_secret("integration_tokens", tokens)
    if existing:
        items[items.index(existing)] = record
    else:
        items.append(record)
    write_json("integrations", items[:40])
    return {"success": True, "integration": _public_integration(record)}


@router.delete("/api/integrations/mcp/{integration_id}")
async def delete_integration(integration_id: str):
    items = _integrations()
    remaining = [item for item in items if item.get("id") != integration_id]
    if len(remaining) == len(items):
        raise HTTPException(404, "Integration does not exist")
    write_json("integrations", remaining)
    tokens = read_secret("integration_tokens", {}) or {}
    if tokens.pop(integration_id, None) is not None:
        write_secret("integration_tokens", tokens)
    return {"success": True}


@router.post("/api/integrations/mcp/{integration_id}/test")
async def test_integration(integration_id: str):
    item = next((entry for entry in _integrations() if entry.get("id") == integration_id), None)
    if not item:
        raise HTTPException(404, "Integration does not exist")
    token = (read_secret("integration_tokens", {}) or {}).get(integration_id, "")
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get(item["base_url"], headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Integration unreachable ({type(exc).__name__})") from None
    ok = response.status_code < 500
    return {"success": ok, "status": response.status_code,
            "detail": "Endpoint reachable." if ok else f"Endpoint returned HTTP {response.status_code}."}


# ============ Notifications ============


@router.get("/api/notifications/settings")
async def notification_settings(request: Request):
    return get_notification_settings(_operator(request))


class NotificationUpdate(BaseModel):
    enabled: Optional[bool] = None
    chat_id: Optional[str] = Field(default=None, max_length=64)
    topic_id: Optional[str] = Field(default=None, max_length=20)
    events: Optional[List[str]] = Field(default=None, max_length=20)
    bot_token: Optional[str] = Field(default=None, max_length=120)


@router.put("/api/notifications/settings")
async def update_notification_settings(body: NotificationUpdate, request: Request):
    operator = _operator(request)
    if body.bot_token and not secret_key_available():
        raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; the bot token cannot be stored safely.")
    try:
        return save_notification_settings(
            operator,
            enabled=body.enabled,
            chat_id=body.chat_id,
            topic_id=body.topic_id,
            events=body.events,
            bot_token=body.bot_token,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/notifications/test")
async def test_notification(request: Request):
    operator = _operator(request)
    result = await send_telegram_message(
        operator, "open-teleset notifications are connected. Job alerts will arrive here."
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("detail") or "Could not send the test message.")
    return result


@router.get("/api/notifications/inbox")
async def notification_inbox(limit: int = 40):
    limit = max(1, min(int(limit), 100))
    items = []
    for entry in log_manager.get_logs(limit=limit):
        items.append({
            "time": entry.get("timestamp") or entry.get("time"),
            "title": entry.get("action") or "Activity",
            "detail": entry.get("detail") or "",
            "level": entry.get("level") or "info",
        })
    return {"items": items, "total": len(items)}


# ============ Cloud Phone recovery vault ============


def _recovery_meta() -> dict:
    data = read_json("recovery_meta", {})
    return data if isinstance(data, dict) else {}


def _recovery_for(operator: str) -> dict:
    return (read_secret("recovery_vault", {}) or {}).get(operator, {})


@router.get("/api/recovery")
async def list_recovery(request: Request):
    operator = _operator(request)
    metadata = _recovery_meta().get(operator, {})
    entries = []
    for account_id, info in metadata.items():
        entries.append({
            "account_id": account_id,
            "fields": info.get("fields", []),
            "has_otp": bool(info.get("has_otp")),
            "updated_at": info.get("updated_at"),
            "account_known": account_id in account_manager.accounts,
        })
    return {"entries": entries, "encryption_ready": secret_key_available()}


class RecoveryInput(BaseModel):
    login_email: Optional[str] = Field(default=None, max_length=200)
    email_password: Optional[str] = Field(default=None, max_length=400)
    twofa_password: Optional[str] = Field(default=None, max_length=400)
    otp_secret: Optional[str] = Field(default=None, max_length=400)
    backup_codes: Optional[str] = Field(default=None, max_length=4000)
    note: Optional[str] = Field(default=None, max_length=1000)
    clear: List[str] = Field(default_factory=list, max_length=len(RECOVERY_FIELDS))


@router.put("/api/recovery/{account_id}")
async def save_recovery(account_id: str, body: RecoveryInput, request: Request):
    if not secret_key_available():
        raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; recovery data cannot be stored safely.")
    operator = _operator(request)
    all_secrets = read_secret("recovery_vault", {}) or {}
    entries = all_secrets.get(operator, {})
    entry = entries.get(account_id, {})
    for field in RECOVERY_FIELDS:
        value = getattr(body, field)
        if value is not None and value.strip():
            entry[field] = value.strip()
    for field in body.clear:
        if field in RECOVERY_FIELDS:
            entry.pop(field, None)
    entries[account_id] = entry
    all_secrets[operator] = entries
    write_secret("recovery_vault", all_secrets)
    metadata = _recovery_meta()
    operator_meta = metadata.get(operator, {})
    operator_meta[account_id] = {
        "fields": sorted(entry.keys()),
        "has_otp": bool(entry.get("otp_secret")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata[operator] = operator_meta
    write_json("recovery_meta", metadata)
    return {"success": True, "account_id": account_id, "fields": sorted(entry.keys())}


@router.delete("/api/recovery/{account_id}")
async def delete_recovery(account_id: str, request: Request):
    operator = _operator(request)
    all_secrets = read_secret("recovery_vault", {}) or {}
    entries = all_secrets.get(operator, {})
    if entries.pop(account_id, None) is None:
        raise HTTPException(404, "No recovery record for this account")
    all_secrets[operator] = entries
    write_secret("recovery_vault", all_secrets)
    metadata = _recovery_meta()
    metadata.get(operator, {}).pop(account_id, None)
    write_json("recovery_meta", metadata)
    return {"success": True}


def _totp(secret: str, digits: int = 6, period: int = 30, at: Optional[float] = None) -> tuple[str, int]:
    raw = secret.strip()
    if raw.lower().startswith("otpauth://"):
        params = parse_qs(urlsplit(raw).query)
        raw = (params.get("secret") or [""])[0]
        digits = int((params.get("digits") or [digits])[0])
        period = int((params.get("period") or [period])[0])
    raw = raw.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z2-7]+=*", raw):
        raise HTTPException(400, "OTP secret must be a Base32 value or an otpauth:// URI")
    padded = raw + "=" * (-len(raw) % 8)
    try:
        key = base64.b32decode(padded)
    except ValueError:
        raise HTTPException(400, "OTP secret is not valid Base32") from None
    now = time.time() if at is None else at
    counter = int(now // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits), period - int(now % period)


@router.post("/api/recovery/{account_id}/otp")
async def recovery_otp(account_id: str, request: Request):
    operator = _operator(request)
    entry = _recovery_for(operator).get(account_id, {})
    if not entry.get("otp_secret"):
        raise HTTPException(404, "No OTP secret is stored for this account")
    code, remaining = _totp(entry["otp_secret"])
    return {"code": code, "seconds_remaining": remaining}


# ============ Free proxy import ============


class FreeProxyImport(BaseModel):
    count: int = Field(default=3, ge=1, le=10)


@router.post("/api/proxies/import-free")
async def import_free_proxies(body: FreeProxyImport):
    try:
        candidates = await proxy_manager.fetch_free_candidates()
    except Exception:
        raise HTTPException(502, "The free proxy list is unavailable. Try again later.") from None
    added = []
    tested = 0
    for start in range(0, min(len(candidates), body.count * 4), 4):
        group = candidates[start:start + 4]
        results = await asyncio.gather(*(proxy_manager.test_proxy(item, timeout=4) for item in group))
        tested += len(group)
        for config, result in zip(group, results):
            if not result.get("success"):
                continue
            proxy_id = f"free-{config['host'].replace('.', '-')}-{config['port']}"
            if proxy_id in proxy_manager.proxies:
                continue
            if proxy_manager.add_proxy(proxy_id, config["protocol"], config["host"], config["port"]):
                added.append({"proxy_id": proxy_id, **config})
            if len(added) >= body.count:
                break
        if len(added) >= body.count:
            break
    if added:
        proxy_manager._save_proxies()
    return {"success": bool(added), "added": added, "tested": tested,
            "detail": f"Added {len(added)} verified free proxies." if added else "No free proxy passed the check."}
