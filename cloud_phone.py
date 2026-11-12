"""Cloud Phone: drive an Android device through its WebDriver (Appium) endpoint.

Telegram work stays on Telethon/MTProto. This panel exists so an operator can use
the phone a cloud provider gave them the way they use a phone in their hand -
look at the screen, tap, swipe, type, press Home and open an app - without
installing anything on a desktop.

Addresses and access tokens are stored encrypted per operator. Typed text and
one-time codes are passed straight to the device and never stored, logged or
returned. The provider's error text is never echoed: it can quote the page or a
URL, so only the standard WebDriver error slug is surfaced.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from log_manager import log_manager
from open_teleset.operator_store import (
    feature_enabled,
    read_secret,
    secret_key_available,
    write_secret,
)
from webhooks import _validate_url

router = APIRouter()

CONFIG_STORE = "cloud_phone"
DEVICE_LABEL_MAX = 60
MAX_TEXT = 200
MAX_COORDINATE = 10000
MIN_SWIPE_MS = 50
MAX_SWIPE_MS = 3000
SESSION_TIMEOUT = 300
STATUS_TIMEOUT = 12.0
ACTION_TIMEOUT = 20.0
PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
# The standard Android hardware codes behind the three buttons every phone has.
KEYS = {"home": 3, "back": 4, "recents": 187, "power": 26}
# Read together with the transport: tests replace this with a mock transport and
# the harness points it at its loopback fake, so no product rule is weakened.
_TRANSPORT = None


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled("cloud_phone"):
        raise HTTPException(
            403,
            "Cloud Phone is switched off. Turn it on under Integrations > Platform switches.",
        )


def _storage_ready() -> None:
    if not secret_key_available():
        raise HTTPException(
            503,
            "SESSION_ENCRYPTION_KEY is not configured; phone addresses cannot be stored safely.",
        )


def _all_config() -> dict:
    data = read_secret(CONFIG_STORE, {})
    return data if isinstance(data, dict) else {}


def _record_for(operator: str) -> dict:
    entry = _all_config().get(operator, {})
    return entry if isinstance(entry, dict) else {}


def _store(operator: str, record: dict) -> None:
    all_config = _all_config()
    all_config[operator] = {**record, "updated_at": datetime.now(UTC).isoformat()}
    write_secret(CONFIG_STORE, all_config)


def _forget(operator: str) -> None:
    all_config = _all_config()
    all_config.pop(operator, None)
    write_secret(CONFIG_STORE, all_config)


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _clean_device(value: str, endpoint: str) -> str:
    label = re.sub(r"\s+", " ", value or "").strip()[:DEVICE_LABEL_MAX]
    return label or _host(endpoint) or "Cloud phone"


def _validate_gateway(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise HTTPException(400, "Enter the WebDriver address your phone provider gave you.")
    try:
        return _validate_url(cleaned)
    except ValueError:
        raise HTTPException(
            400,
            "Use the provider's https:// WebDriver address, with no credentials in it.",
        ) from None


# ------------------------------------------------------------------- transport


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=_TRANSPORT)


def _headers(config: dict) -> dict:
    token = str(config.get("token") or "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _slug(payload) -> str:
    """The standard WebDriver error name, never the provider's free text."""
    if isinstance(payload, dict):
        value = payload.get("value")
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            error = value["error"]
            return error[:60] if re.fullmatch(r"[a-z0-9 \-]+", error) else "provider error"
    return "provider error"


async def _call(operator: str, method: str, path: str, body: dict | None = None,
                timeout: float = ACTION_TIMEOUT) -> dict:
    config = _record_for(operator)
    endpoint = str(config.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise HTTPException(400, "Add your cloud phone's WebDriver address first.")
    try:
        async with _client(timeout) as client:
            response = await client.request(
                method, f"{endpoint}{path}", json=body, headers=_headers(config)
            )
    except httpx.HTTPError:
        raise HTTPException(502, "The phone gateway did not answer.") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code == 404 and method == "DELETE":
        # Closing a session the gateway already forgot is the desired end state.
        return {}
    if response.status_code >= 400:
        raise HTTPException(502, f"The phone gateway refused the request ({_slug(payload)}).")
    return payload if isinstance(payload, dict) else {}


def _value(payload: dict):
    return payload.get("value")


# --------------------------------------------------------------- one session
#
# A WebDriver session is stateful and expensive, so the panel keeps exactly one
# per operator and releases it when the address changes or the operator leaves.

_sessions: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(operator: str) -> asyncio.Lock:
    return _locks.setdefault(operator, asyncio.Lock())


def _session(operator: str) -> dict | None:
    return _sessions.get(operator)


def _session_id(operator: str) -> str:
    session = _session(operator)
    if not session:
        raise HTTPException(409, "Connect the phone before sending it anything.")
    return session["id"]


async def _drop(operator: str) -> None:
    session = _sessions.pop(operator, None)
    if not session:
        return
    try:
        await _call(operator, "DELETE", f"/session/{session['id']}", timeout=STATUS_TIMEOUT)
    except HTTPException:
        pass


def _capabilities(record: dict) -> dict:
    capabilities = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:deviceName": str(record.get("device") or "Android"),
        "appium:noReset": True,
        "appium:newCommandTimeout": SESSION_TIMEOUT,
    }
    udid = str(record.get("udid") or "").strip()
    if udid:
        capabilities["appium:udid"] = udid
    return capabilities


def _session_public(operator: str) -> dict:
    session = _session(operator)
    return {
        "connected": bool(session),
        "device": session.get("device", "") if session else "",
        "since": session.get("since") if session else None,
    }


def _public_config(operator: str) -> dict:
    record = _record_for(operator)
    return {
        "configured": bool(record.get("endpoint")),
        "host": _host(str(record.get("endpoint") or "")),
        "has_token": bool(record.get("token")),
        "device": str(record.get("device") or ""),
        "udid": str(record.get("udid") or ""),
        "package": str(record.get("package") or ""),
        "updated_at": record.get("updated_at"),
        "enabled": feature_enabled("cloud_phone"),
        "encryption_ready": secret_key_available(),
        "keys": list(KEYS),
        "max_text": MAX_TEXT,
        "session": _session_public(operator),
    }


class ConfigInput(BaseModel):
    endpoint: str = Field(default="", max_length=600)
    token: str | None = Field(default=None, max_length=600)
    device: str = Field(default="", max_length=DEVICE_LABEL_MAX)
    udid: str = Field(default="", max_length=120)
    package: str = Field(default="", max_length=120)
    clear: bool = False

    @field_validator("endpoint", "device", "udid", "package")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


@router.get("/api/cloud-phone/config")
async def get_config(request: Request):
    return _public_config(_operator(request))


@router.put("/api/cloud-phone/config")
async def save_config(body: ConfigInput, request: Request):
    operator = _operator(request)
    _enabled()
    _storage_ready()
    if body.clear:
        _forget(operator)
        await _drop(operator)
        log_manager.add_log("Cloud Phone", operator, "Phone address removed", "info")
        return {"success": True, **_public_config(operator)}
    endpoint = _validate_gateway(body.endpoint)
    if body.package and not PACKAGE.fullmatch(body.package):
        raise HTTPException(400, "Use the app's package name, like com.telegram.messenger.")
    record = _record_for(operator)
    token = (body.token or "").strip()
    record.update({
        "endpoint": endpoint,
        "device": _clean_device(body.device, endpoint),
        "udid": body.udid,
        "package": body.package,
    })
    if token:
        record["token"] = token
    _store(operator, record)
    # The live session belongs to the address that was just replaced.
    await _drop(operator)
    log_manager.add_log("Cloud Phone", operator, f"Phone address set to {_host(endpoint)}", "info")
    return {"success": True, **_public_config(operator)}


@router.post("/api/cloud-phone/check")
async def check_gateway(request: Request):
    """Read the gateway's readiness; it creates no session and touches no device."""
    operator = _operator(request)
    _enabled()
    payload = await _call(operator, "GET", "/status", timeout=STATUS_TIMEOUT)
    value = _value(payload)
    value = value if isinstance(value, dict) else {}
    return {
        "success": True,
        "ready": bool(value.get("ready")),
        "build": str(value.get("build") or "")[:80],
    }


@router.post("/api/cloud-phone/connect")
async def connect(request: Request):
    operator = _operator(request)
    _enabled()
    async with _lock(operator):
        existing = _session(operator)
        if existing:
            return {"success": True, "reused": True, **_public_config(operator)}
        record = _record_for(operator)
        payload = await _call(
            operator,
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": _capabilities(record)}},
        )
        value = _value(payload)
        session_id = value.get("sessionId") if isinstance(value, dict) else None
        if not session_id:
            raise HTTPException(502, "The phone gateway opened no session.")
        _sessions[operator] = {
            "id": str(session_id),
            "device": str(record.get("device") or ""),
            "since": datetime.now(UTC).isoformat(),
        }
    log_manager.add_log("Cloud Phone", operator, "Connected the phone", "info")
    return {"success": True, "reused": False, **_public_config(operator)}


@router.post("/api/cloud-phone/disconnect")
async def disconnect(request: Request):
    operator = _operator(request)
    _enabled()
    async with _lock(operator):
        await _drop(operator)
    log_manager.add_log("Cloud Phone", operator, "Released the phone", "info")
    return {"success": True, **_public_config(operator)}


@router.get("/api/cloud-phone/screen")
async def screen(request: Request):
    """One frame as a data URL the panel can show as an image."""
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    payload = await _call(operator, "GET", f"/session/{session_id}/screenshot")
    image = _value(payload)
    if not isinstance(image, str) or not image:
        raise HTTPException(502, "The phone returned no screen.")
    return {"image": image, "at": datetime.now(UTC).isoformat()}


class TapInput(BaseModel):
    x: int = Field(ge=0, le=MAX_COORDINATE)
    y: int = Field(ge=0, le=MAX_COORDINATE)


def _pointer(x: int, y: int) -> dict:
    return {"type": "pointerMove", "duration": 0, "x": x, "y": y}


@router.post("/api/cloud-phone/tap")
async def tap(body: TapInput, request: Request):
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    await _call(operator, "POST", f"/session/{session_id}/actions", {"actions": [{
        "type": "pointer",
        "id": "finger1",
        "parameters": {"pointerType": "touch"},
        "actions": [
            _pointer(body.x, body.y),
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": 60},
            {"type": "pointerUp", "button": 0},
        ],
    }]})
    return {"success": True}


class SwipeInput(BaseModel):
    x1: int = Field(ge=0, le=MAX_COORDINATE)
    y1: int = Field(ge=0, le=MAX_COORDINATE)
    x2: int = Field(ge=0, le=MAX_COORDINATE)
    y2: int = Field(ge=0, le=MAX_COORDINATE)
    duration_ms: int = Field(default=300, ge=MIN_SWIPE_MS, le=MAX_SWIPE_MS)


@router.post("/api/cloud-phone/swipe")
async def swipe(body: SwipeInput, request: Request):
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    await _call(operator, "POST", f"/session/{session_id}/actions", {"actions": [{
        "type": "pointer",
        "id": "finger1",
        "parameters": {"pointerType": "touch"},
        "actions": [
            _pointer(body.x1, body.y1),
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": 40},
            {"type": "pointerMove", "duration": body.duration_ms, "x": body.x2, "y": body.y2},
            {"type": "pointerUp", "button": 0},
        ],
    }]})
    return {"success": True}


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@router.post("/api/cloud-phone/text")
async def type_text(body: TextInput, request: Request):
    """Type into whatever field the device has focused, one key at a time."""
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    actions = []
    for character in body.text:
        actions.append({"type": "keyDown", "value": character})
        actions.append({"type": "keyUp", "value": character})
    await _call(operator, "POST", f"/session/{session_id}/actions", {"actions": [{
        "type": "key",
        "id": "keyboard",
        "actions": actions,
    }]})
    # The text may be a credential: count it, never record it.
    log_manager.add_log("Cloud Phone", operator, f"Typed {len(body.text)} characters", "info")
    return {"success": True, "characters": len(body.text)}


class KeyInput(BaseModel):
    key: Literal["home", "back", "recents", "power"]


@router.post("/api/cloud-phone/key")
async def press_key(body: KeyInput, request: Request):
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    await _call(
        operator,
        "POST",
        f"/session/{session_id}/appium/device/press_keycode",
        {"keycode": KEYS[body.key]},
    )
    log_manager.add_log("Cloud Phone", operator, f"Pressed {body.key}", "info")
    return {"success": True}


class AppInput(BaseModel):
    package: str = Field(min_length=3, max_length=120)

    @field_validator("package")
    @classmethod
    def _package(cls, value: str) -> str:
        cleaned = value.strip()
        if not PACKAGE.fullmatch(cleaned):
            raise ValueError("Use the app's package name, like com.telegram.messenger.")
        return cleaned


@router.post("/api/cloud-phone/app/launch")
async def launch_app(body: AppInput, request: Request):
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    await _call(
        operator,
        "POST",
        f"/session/{session_id}/appium/device/activate_app",
        {"appId": body.package},
    )
    log_manager.add_log("Cloud Phone", operator, f"Launched {body.package}", "info")
    return {"success": True}


@router.post("/api/cloud-phone/app/stop")
async def stop_app(body: AppInput, request: Request):
    operator = _operator(request)
    _enabled()
    session_id = _session_id(operator)
    await _call(
        operator,
        "POST",
        f"/session/{session_id}/appium/device/terminate_app",
        {"appId": body.package},
    )
    log_manager.add_log("Cloud Phone", operator, f"Stopped {body.package}", "info")
    return {"success": True}
