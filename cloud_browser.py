"""Cloud Browser: an operator-owned hosted browser driven over DevTools.

Telegram operations stay on Telethon/MTProto. This panel exists for the
automations that genuinely need a browser - signing in to Telegram Web, reaching
a service that has no API - and for handing a Cloud Phone code straight into that
page. Nothing here touches the account clients.

Addresses and access tokens are stored encrypted per operator. Typed text and
one-time codes are passed through to the browser and never stored, logged or
returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from log_manager import log_manager
from open_teleset.cdp_client import _KEY_CODES, CdpClient, CdpError
from open_teleset.operator_store import (
    feature_enabled,
    read_secret,
    secret_key_available,
    write_secret,
)

router = APIRouter()

CONFIG_STORE = "cloud_browser"
MAX_PAGES = 40
_ENDPOINT_SCHEMES = {"ws", "wss"}
_PAGE_SCHEMES = {"http", "https"}


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled("cloud_browser"):
        raise HTTPException(403, "Cloud Browser is switched off. Turn it on under Integrations > Platform switches.")


def _all_config() -> dict:
    data = read_secret(CONFIG_STORE, {})
    return data if isinstance(data, dict) else {}


def _config_for(operator: str) -> dict:
    entry = _all_config().get(operator, {})
    return entry if isinstance(entry, dict) else {}


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _validate_endpoint(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise HTTPException(400, "Enter the DevTools address of your hosted browser.")
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        raise HTTPException(400, "That DevTools address is not readable.") from None
    if parts.scheme not in _ENDPOINT_SCHEMES:
        raise HTTPException(400, "Use a ws:// or wss:// DevTools address - the debugger URL, not the web page.")
    if parts.username or parts.password:
        raise HTTPException(400, "Keep credentials out of the address; put the token in the access-token field.")
    if not parts.hostname:
        raise HTTPException(400, "That DevTools address has no host.")
    return cleaned


def _validate_page_url(url: str) -> str:
    cleaned = url.strip()
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        raise HTTPException(400, "That page address is not readable.") from None
    if parts.scheme not in _PAGE_SCHEMES:
        raise HTTPException(400, "Open http(s) addresses only.")
    if not parts.hostname:
        raise HTTPException(400, "That page address has no host.")
    return cleaned


async def _connect(operator: str) -> CdpClient:
    config = _config_for(operator)
    url = config.get("endpoint", "")
    if not url:
        raise HTTPException(400, "Add your hosted browser address in the Cloud Browser panel first.")
    headers = {"Authorization": f"Bearer {config['token']}"} if config.get("token") else None
    try:
        return await CdpClient.connect(url, headers)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None


def _label(operator: str) -> str:
    return _host(_config_for(operator).get("endpoint", "")) or "cloud browser"


def _public_config(operator: str) -> dict:
    config = _config_for(operator)
    return {
        "configured": bool(config.get("endpoint")),
        "host": _host(config.get("endpoint", "")),
        "has_token": bool(config.get("token")),
        "updated_at": config.get("updated_at"),
        "enabled": feature_enabled("cloud_browser"),
        "encryption_ready": secret_key_available(),
    }


class ConfigInput(BaseModel):
    endpoint: str = Field(default="", max_length=600)
    token: str | None = Field(default=None, max_length=600)
    clear: bool = False

    @field_validator("endpoint")
    @classmethod
    def _strip_endpoint(cls, value: str) -> str:
        return value.strip()


@router.get("/api/cloud-browser/config")
async def get_config(request: Request):
    operator = _operator(request)
    return _public_config(operator)


@router.put("/api/cloud-browser/config")
async def save_config(body: ConfigInput, request: Request):
    operator = _operator(request)
    _enabled()
    if not secret_key_available():
        raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; browser addresses cannot be stored safely.")
    all_config = _all_config()
    if body.clear:
        all_config.pop(operator, None)
        write_secret(CONFIG_STORE, all_config)
        log_manager.add_log("Cloud Browser", operator, "Hosted browser address removed", "info")
        # Same shape as the save branch: the panel keeps its switch and key state.
        return {"success": True, **_public_config(operator)}
    endpoint = _validate_endpoint(body.endpoint)
    entry = {"endpoint": endpoint, "updated_at": datetime.now(UTC).isoformat()}
    existing = _config_for(operator)
    if body.token and body.token.strip():
        entry["token"] = body.token.strip()
    elif existing.get("token"):
        entry["token"] = existing["token"]
    all_config[operator] = entry
    write_secret(CONFIG_STORE, all_config)
    log_manager.add_log("Cloud Browser", operator, f"Hosted browser address set to {_host(endpoint)}", "info")
    return {"success": True, **_public_config(operator)}


@router.post("/api/cloud-browser/check")
async def check_connection(request: Request):
    operator = _operator(request)
    _enabled()
    client = await _connect(operator)
    try:
        version = await client.version()
        pages = await client.pages()
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    return {
        "success": True,
        "product": version["product"],
        "revision": version["revision"],
        "pages": pages[:MAX_PAGES],
    }


@router.get("/api/cloud-browser/pages")
async def list_pages(request: Request):
    operator = _operator(request)
    _enabled()
    client = await _connect(operator)
    try:
        pages = await client.pages()
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    return {"pages": pages[:MAX_PAGES]}


class OpenPageInput(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@router.post("/api/cloud-browser/pages")
async def open_page(body: OpenPageInput, request: Request):
    operator = _operator(request)
    _enabled()
    url = _validate_page_url(body.url)
    client = await _connect(operator)
    try:
        opened = await client.open_page(url)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    log_manager.add_log("Cloud Browser", _label(operator), f"Opened a page on {_host(url) or 'an internal address'}", "info")
    return {"success": True, "target_id": opened["target_id"]}


@router.delete("/api/cloud-browser/pages/{target_id}")
async def close_page(target_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    client = await _connect(operator)
    try:
        await client.close_page(target_id)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    log_manager.add_log("Cloud Browser", _label(operator), "Closed a page", "info")
    return {"success": True}


class NavigateInput(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@router.post("/api/cloud-browser/pages/{target_id}/navigate")
async def navigate_page(target_id: str, body: NavigateInput, request: Request):
    operator = _operator(request)
    _enabled()
    url = _validate_page_url(body.url)
    client = await _connect(operator)
    try:
        await client.navigate(target_id, url)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    log_manager.add_log("Cloud Browser", _label(operator), f"Navigated to {_host(url) or 'an internal address'}", "info")
    return {"success": True}


@router.post("/api/cloud-browser/pages/{target_id}/screenshot")
async def page_screenshot(target_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    client = await _connect(operator)
    try:
        image = await client.screenshot(target_id)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    if not image:
        raise HTTPException(502, "The browser endpoint returned no view.")
    return {"image": image}


class TypeInput(BaseModel):
    text: str = Field(min_length=1, max_length=400)


@router.post("/api/cloud-browser/pages/{target_id}/type")
async def type_into_page(target_id: str, body: TypeInput, request: Request):
    operator = _operator(request)
    _enabled()
    client = await _connect(operator)
    try:
        await client.type_text(target_id, body.text)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    # The text is a credential being handed to a login form: count it, never record it.
    log_manager.add_log("Cloud Browser", _label(operator), f"Typed {len(body.text)} characters into a page", "info")
    return {"success": True, "characters": len(body.text)}


class KeyInput(BaseModel):
    key: str = Field(min_length=1, max_length=10)


@router.post("/api/cloud-browser/pages/{target_id}/key")
async def press_key(target_id: str, body: KeyInput, request: Request):
    operator = _operator(request)
    _enabled()
    if body.key not in _KEY_CODES:
        raise HTTPException(400, "Only Enter, Tab, Escape and Backspace can be sent.")
    client = await _connect(operator)
    try:
        await client.press_key(target_id, body.key)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    log_manager.add_log("Cloud Browser", _label(operator), f"Sent {body.key}", "info")
    return {"success": True}


class OtpInput(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)


@router.post("/api/cloud-browser/pages/{target_id}/otp")
async def send_cloud_phone_otp(target_id: str, body: OtpInput, request: Request):
    """Type the Cloud Phone vault's current code into a page without echoing it."""
    from dashboard_features import _recovery_for, _totp

    operator = _operator(request)
    _enabled()
    entry = _recovery_for(operator).get(body.account_id, {})
    secret = entry.get("otp_secret") if isinstance(entry, dict) else None
    if not secret:
        raise HTTPException(404, "No OTP secret is stored for that Cloud Phone record.")
    code, remaining = _totp(secret)
    client = await _connect(operator)
    try:
        await client.type_text(target_id, code)
    except CdpError as exc:
        raise HTTPException(502, str(exc)) from None
    finally:
        await client.close()
    log_manager.add_log("Cloud Browser", _label(operator), f"Sent a Cloud Phone code to a page ({remaining}s window)", "info")
    return {"success": True, "seconds_remaining": remaining}
