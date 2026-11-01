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

import asyncio
import base64
import io
import json
import re
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from log_manager import log_manager
from open_teleset.cdp_client import KEY_REJECTED_MESSAGE, CdpClient, CdpError, pressable_keys
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
# The view is a login helper, not a video stream: two frames a second is enough
# to follow what the browser is doing and keeps the tunnel out of the way.
FRAME_INTERVAL = 0.5
LIVE_IDLE_SECONDS = 60
STREAM_HEARTBEAT_SECONDS = 15
MAX_SUBSCRIBERS = 4
REEL_MAX_FRAMES = 300
REEL_MAX_BYTES = 8 * 1024 * 1024
FRAME_QUALITY = 60
SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
# One operator may keep several browsers apart by name. The cap is generous for a
# login vault and small enough that the list stays readable in the panel.
MAX_PROFILES = 12
LABEL_MAX = 60


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
    """The connection the panel is driving right now."""
    record = _record_for(operator)
    profiles = _profiles(record)
    return profiles.get(_active_id(record, profiles), {})


def _record_for(operator: str) -> dict:
    entry = _all_config().get(operator, {})
    return entry if isinstance(entry, dict) else {}


def _clean_label(value: str, endpoint: str) -> str:
    label = re.sub(r"\s+", " ", value or "").strip()[:LABEL_MAX]
    return label or _host(endpoint) or "Browser"


def _profiles(record: dict) -> dict:
    """Every named browser in one operator's record.

    A record saved before named connections existed kept a single address at the top
    level. Reading that as "Default" keeps an existing vault working, and the rewrite
    on the next save retires the duplicate copy of the secret.
    """
    profiles = {}
    raw = record.get("profiles")
    if isinstance(raw, dict):
        for profile_id, item in raw.items():
            if isinstance(item, dict) and item.get("endpoint"):
                endpoint = str(item["endpoint"])
                profiles[str(profile_id)] = {
                    "id": str(profile_id),
                    "label": _clean_label(str(item.get("label") or ""), endpoint),
                    "endpoint": endpoint,
                    "token": str(item.get("token") or ""),
                    "updated_at": item.get("updated_at"),
                }
    if profiles:
        return profiles
    if record.get("endpoint"):
        endpoint = str(record["endpoint"])
        return {
            "default": {
                "id": "default",
                "label": _clean_label(str(record.get("label") or ""), endpoint),
                "endpoint": endpoint,
                "token": str(record.get("token") or ""),
                "updated_at": record.get("updated_at"),
            }
        }
    return {}


def _active_id(record: dict, profiles: dict) -> str:
    wanted = str(record.get("active") or "")
    if wanted in profiles:
        return wanted
    # Nothing selected: the browser touched most recently is the one in use.
    ordered = sorted(profiles.values(), key=lambda item: item.get("updated_at") or "", reverse=True)
    return ordered[0]["id"] if ordered else ""


def _store_profiles(operator: str, profiles: dict, active: str) -> None:
    """Write the whole record, which also drops the legacy top-level address."""
    all_config = _all_config()
    all_config[operator] = {
        "active": active,
        "updated_at": datetime.now(UTC).isoformat(),
        "profiles": {
            profile_id: {
                "label": item["label"],
                "endpoint": item["endpoint"],
                "token": item.get("token") or "",
                "updated_at": item.get("updated_at"),
            }
            for profile_id, item in profiles.items()
        },
    }
    write_secret(CONFIG_STORE, all_config)


def _forget_every_profile(operator: str) -> None:
    all_config = _all_config()
    all_config.pop(operator, None)
    write_secret(CONFIG_STORE, all_config)


def _profile_public(profiles: dict, active: str) -> list[dict]:
    # Only the host of each address: a DevTools URL can carry a session path, and
    # the label is operator-written so it may repeat a host the panel already shows.
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "host": _host(item["endpoint"]),
            "has_token": bool(item.get("token")),
            "active": item["id"] == active,
            "updated_at": item.get("updated_at"),
        }
        for item in sorted(profiles.values(), key=lambda entry: entry["label"].lower())
    ]


def _profile_or_404(profiles: dict, profile_id: str) -> dict:
    item = profiles.get(profile_id)
    if item is None:
        raise HTTPException(404, "No named browser with that id.")
    return item


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


# ------------------------------------------------------------------ live session
#
# A screencast needs one DevTools connection to stay open, so the operator's
# cockpit holds a single session and every control command rides on it. The
# session is closed when nothing is watching it for LIVE_IDLE_SECONDS.


class _Session:
    def __init__(self, operator: str, client: CdpClient, target_id: str, session_id: str) -> None:
        self.operator = operator
        self.client = client
        self.target_id = target_id
        self.session_id = session_id
        self.subscribers: list[asyncio.Queue] = []
        self.frames = 0
        self.reel: list[bytes] = []
        self.reel_bytes = 0
        self.recording = False
        self.closed = False
        self._last_frame = 0.0
        self._idle: asyncio.Task | None = None

    def publish(self, data: str, metadata: dict) -> None:
        """Called from the DevTools event pump for every repainted frame."""
        now = time.monotonic()
        if now - self._last_frame < FRAME_INTERVAL:
            return
        self._last_frame = now
        raw = _decode_frame(data)
        if raw is None:
            return
        self.frames += 1
        if self.recording:
            self._append_reel(raw)
        payload = {"type": "frame", "image": data, "at": _frame_clock(metadata)}
        for queue in list(self.subscribers):
            # A viewer that cannot keep up sees the newest frame, not a backlog.
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(payload)

    def _append_reel(self, raw: bytes) -> None:
        self.reel.append(raw)
        self.reel_bytes += len(raw)
        while len(self.reel) > REEL_MAX_FRAMES or self.reel_bytes > REEL_MAX_BYTES:
            self.reel_bytes -= len(self.reel.pop(0))

    def add_subscriber(self) -> asyncio.Queue:
        if len(self.subscribers) >= MAX_SUBSCRIBERS:
            raise HTTPException(429, "This browser view already has the maximum number of watchers.")
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.subscribers.append(queue)
        if self._idle:
            self._idle.cancel()
            self._idle = None
        return queue

    def drop_subscriber(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)
        if not self.subscribers and self._idle is None and not self.closed:
            self._idle = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        await asyncio.sleep(LIVE_IDLE_SECONDS)
        if not self.subscribers:
            await _drop_session(self)

    async def teardown(self) -> None:
        await _drop_session(self)


_sessions: dict[str, _Session] = {}
_session_lock = asyncio.Lock()


def _decode_frame(data: str) -> bytes | None:
    try:
        return base64.b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return None


def _frame_clock(metadata: dict) -> str:
    stamp = metadata.get("timestamp")
    return datetime.fromtimestamp(stamp, UTC).isoformat() if isinstance(stamp, (int, float)) else ""


async def _drop_session(session: _Session) -> None:
    """Close one live connection, once, whichever path noticed it was finished with."""
    if _sessions.get(session.operator) is session:
        _sessions.pop(session.operator, None)
    session.closed = True
    if session._idle:
        session._idle.cancel()
        session._idle = None
    try:
        await session.client.stop_screencast(session.session_id)
    except CdpError:
        pass
    await session.client.close()
    for queue in session.subscribers:
        queue.put_nowait({"type": "closed"})
    session.subscribers.clear()


@asynccontextmanager
async def _client_for(operator: str):
    """Use the live cockpit connection when there is one, else a short-lived one."""
    session = _sessions.get(operator)
    if session is not None and not session.client.is_closed:
        yield session.client
        return
    client = await _connect(operator)
    try:
        yield client
    finally:
        await client.close()


async def _live_session(operator: str, target_id: str) -> _Session:
    async with _session_lock:
        session = _sessions.get(operator)
        if session and session.client.is_closed:
            await _drop_session(session)
            session = None
        if session and session.target_id != target_id:
            await _drop_session(session)
            session = None
        if session is None:
            client = await _connect(operator)
            try:
                session_id = await client.start_screencast(
                    target_id, _publisher(operator), quality=FRAME_QUALITY
                )
            except CdpError as exc:
                await client.close()
                raise HTTPException(502, str(exc)) from None
            session = _Session(operator, client, target_id, session_id)
            _sessions[operator] = session
        return session


def _publisher(operator: str):
    def publish(data: str, metadata: dict) -> None:
        session = _sessions.get(operator)
        if session is not None:
            session.publish(data, metadata)

    return publish


def _live_state(operator: str) -> dict:
    session = _sessions.get(operator)
    return {
        "live": bool(session and not session.client.is_closed),
        "watchers": len(session.subscribers) if session else 0,
        "frames": session.frames if session else 0,
        "recording": bool(session and session.recording),
        "reel_frames": len(session.reel) if session else 0,
        "reel_bytes": session.reel_bytes if session else 0,
    }



async def _drop_existing(operator: str) -> None:
    """Forget an open cockpit after the address it belongs to has changed."""
    session = _sessions.pop(operator, None)
    if session:
        await session.teardown()


def _label(operator: str) -> str:
    return _host(_config_for(operator).get("endpoint", "")) or "cloud browser"


def _public_config(operator: str) -> dict:
    record = _record_for(operator)
    profiles = _profiles(record)
    active = _active_id(record, profiles)
    config = profiles.get(active, {})
    return {
        "configured": bool(config.get("endpoint")),
        "host": _host(config.get("endpoint", "")),
        "has_token": bool(config.get("token")),
        "updated_at": config.get("updated_at"),
        "enabled": feature_enabled("cloud_browser"),
        "encryption_ready": secret_key_available(),
        "keys": pressable_keys(),
        "max_profiles": MAX_PROFILES,
        "active": active,
        "profiles": _profile_public(profiles, active),
    }


class ConfigInput(BaseModel):
    endpoint: str = Field(default="", max_length=600)
    token: str | None = Field(default=None, max_length=600)
    label: str = Field(default="", max_length=LABEL_MAX)
    clear: bool = False

    @field_validator("endpoint", "label")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


@router.get("/api/cloud-browser/config")
async def get_config(request: Request):
    operator = _operator(request)
    return _public_config(operator)


@router.put("/api/cloud-browser/config")
async def save_config(body: ConfigInput, request: Request):
    operator = _operator(request)
    _enabled()
    _storage_ready()
    if body.clear:
        _forget_every_profile(operator)
        await _drop_existing(operator)
        log_manager.add_log("Cloud Browser", operator, "Hosted browser address removed", "info")
        # Same shape as the save branch: the panel keeps its switch and key state.
        return {"success": True, **_public_config(operator)}
    endpoint = _validate_endpoint(body.endpoint)
    record = _record_for(operator)
    profiles = _profiles(record)
    active = _active_id(record, profiles)
    token = (body.token or "").strip()
    entry = profiles.get(active)
    if entry is None:
        active = "default"
        profiles[active] = {
            "id": active,
            "label": _clean_label(body.label, endpoint),
            "endpoint": endpoint,
            "token": token,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    else:
        entry["endpoint"] = endpoint
        if body.label:
            entry["label"] = _clean_label(body.label, endpoint)
        if token:
            entry["token"] = token
        entry["updated_at"] = datetime.now(UTC).isoformat()
    _store_profiles(operator, profiles, active)
    await _drop_existing(operator)
    log_manager.add_log("Cloud Browser", operator, f"Hosted browser address set to {_host(endpoint)}", "info")
    return {"success": True, **_public_config(operator)}


def _storage_ready() -> None:
    if not secret_key_available():
        raise HTTPException(503, "SESSION_ENCRYPTION_KEY is not configured; browser addresses cannot be stored safely.")


class ProfileInput(BaseModel):
    label: str = Field(default="", max_length=LABEL_MAX)
    endpoint: str = Field(default="", max_length=600)
    token: str | None = Field(default=None, max_length=600)

    @field_validator("label", "endpoint")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


@router.post("/api/cloud-browser/profiles")
async def add_profile(body: ProfileInput, request: Request):
    """Name a browser. Each one is a separate browser with its own cookies."""
    operator = _operator(request)
    _enabled()
    _storage_ready()
    record = _record_for(operator)
    profiles = _profiles(record)
    if len(profiles) >= MAX_PROFILES:
        raise HTTPException(409, f"Keep at most {MAX_PROFILES} named browsers - forget one first.")
    endpoint = _validate_endpoint(body.endpoint)
    profile_id = f"browser_{uuid4().hex[:8]}"
    profiles[profile_id] = {
        "id": profile_id,
        "label": _clean_label(body.label, endpoint),
        "endpoint": endpoint,
        "token": (body.token or "").strip(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    # The address just typed is the one to drive, so a new browser becomes active and
    # any live view of the previous one is released rather than silently re-pointed.
    _store_profiles(operator, profiles, profile_id)
    await _drop_existing(operator)
    log_manager.add_log("Cloud Browser", _label(operator), f"Added the named browser {profiles[profile_id]['label']}", "info")
    return {"success": True, **_public_config(operator)}


@router.put("/api/cloud-browser/profiles/{profile_id}")
async def update_profile(profile_id: str, body: ProfileInput, request: Request):
    """Change one browser; a blank address or token keeps what is stored."""
    operator = _operator(request)
    _enabled()
    _storage_ready()
    record = _record_for(operator)
    profiles = _profiles(record)
    entry = _profile_or_404(profiles, profile_id)
    was_active = profile_id == _active_id(record, profiles)
    if body.endpoint:
        entry["endpoint"] = _validate_endpoint(body.endpoint)
    if body.label:
        entry["label"] = _clean_label(body.label, entry["endpoint"])
    if (body.token or "").strip():
        entry["token"] = body.token.strip()
    entry["updated_at"] = datetime.now(UTC).isoformat()
    _store_profiles(operator, profiles, _active_id(record, profiles))
    if was_active:
        # The cockpit is looking at the old address.
        await _drop_existing(operator)
    log_manager.add_log("Cloud Browser", _label(operator), f"Updated the named browser {entry['label']}", "info")
    return {"success": True, **_public_config(operator)}


@router.post("/api/cloud-browser/profiles/{profile_id}/activate")
async def use_profile(profile_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    record = _record_for(operator)
    profiles = _profiles(record)
    entry = _profile_or_404(profiles, profile_id)
    was_active = profile_id == _active_id(record, profiles)
    _store_profiles(operator, profiles, profile_id)
    if not was_active:
        await _drop_existing(operator)
    log_manager.add_log("Cloud Browser", _label(operator), f"Switched to the named browser {entry['label']}", "info")
    return {"success": True, **_public_config(operator)}


@router.delete("/api/cloud-browser/profiles/{profile_id}")
async def forget_profile(profile_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    record = _record_for(operator)
    profiles = _profiles(record)
    entry = _profile_or_404(profiles, profile_id)
    was_active = profile_id == _active_id(record, profiles)
    label = entry["label"]
    profiles.pop(profile_id)
    if profiles:
        ordered = sorted(profiles.values(), key=lambda item: item.get("updated_at") or "", reverse=True)
        _store_profiles(operator, profiles, ordered[0]["id"])
    else:
        _forget_every_profile(operator)
    if was_active:
        await _drop_existing(operator)
    log_manager.add_log("Cloud Browser", _label(operator), f"Forgot the named browser {label}", "info")
    return {"success": True, **_public_config(operator)}


@router.post("/api/cloud-browser/check")
async def check_connection(request: Request):
    operator = _operator(request)
    _enabled()
    async with _client_for(operator) as client:
        try:
            version = await client.version()
            pages = await client.pages()
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
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
    async with _client_for(operator) as client:
        try:
            pages = await client.pages()
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    return {"pages": pages[:MAX_PAGES]}


class OpenPageInput(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@router.post("/api/cloud-browser/pages")
async def open_page(body: OpenPageInput, request: Request):
    operator = _operator(request)
    _enabled()
    url = _validate_page_url(body.url)
    async with _client_for(operator) as client:
        try:
            opened = await client.open_page(url)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    log_manager.add_log("Cloud Browser", _label(operator), f"Opened a page on {_host(url) or 'an internal address'}", "info")
    return {"success": True, "target_id": opened["target_id"]}


@router.delete("/api/cloud-browser/pages/{target_id}")
async def close_page(target_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    session = _sessions.get(operator)
    async with _client_for(operator) as client:
        try:
            await client.close_page(target_id)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    # The page a live view is streaming is gone; drop the stream rather than
    # leave the cockpit showing a frame of a tab that no longer exists.
    if session and session.target_id == target_id:
        await session.teardown()
    log_manager.add_log("Cloud Browser", _label(operator), "Closed a page", "info")
    return {"success": True, "live": _live_state(operator)}


class NavigateInput(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@router.post("/api/cloud-browser/pages/{target_id}/navigate")
async def navigate_page(target_id: str, body: NavigateInput, request: Request):
    operator = _operator(request)
    _enabled()
    url = _validate_page_url(body.url)
    async with _client_for(operator) as client:
        try:
            await client.navigate(target_id, url)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    log_manager.add_log("Cloud Browser", _label(operator), f"Navigated to {_host(url) or 'an internal address'}", "info")
    return {"success": True}


@router.post("/api/cloud-browser/pages/{target_id}/screenshot")
async def page_screenshot(target_id: str, request: Request):
    """A single frame, for when the live view is not open."""
    operator = _operator(request)
    _enabled()
    async with _client_for(operator) as client:
        try:
            image = await client.screenshot(target_id)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    if not image:
        raise HTTPException(502, "The browser endpoint returned no view.")
    return {"image": image}


class TypeInput(BaseModel):
    text: str = Field(min_length=1, max_length=400)


@router.post("/api/cloud-browser/pages/{target_id}/type")
async def type_into_page(target_id: str, body: TypeInput, request: Request):
    operator = _operator(request)
    _enabled()
    async with _client_for(operator) as client:
        try:
            await client.type_text(target_id, body.text)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    # The text is a credential being handed to a login form: count it, never record it.
    log_manager.add_log("Cloud Browser", _label(operator), f"Typed {len(body.text)} characters into a page", "info")
    return {"success": True, "characters": len(body.text)}


class KeyInput(BaseModel):
    key: str = Field(min_length=1, max_length=10)


@router.post("/api/cloud-browser/pages/{target_id}/key")
async def press_key(target_id: str, body: KeyInput, request: Request):
    operator = _operator(request)
    _enabled()
    if body.key not in pressable_keys():
        raise HTTPException(400, KEY_REJECTED_MESSAGE)
    async with _client_for(operator) as client:
        try:
            await client.press_key(target_id, body.key)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
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
    async with _client_for(operator) as client:
        try:
            await client.type_text(target_id, code)
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    log_manager.add_log("Cloud Browser", _label(operator), f"Sent a Cloud Phone code to a page ({remaining}s window)", "info")
    return {"success": True, "seconds_remaining": remaining}


# ------------------------------------------------------------------- live view


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


@router.get("/api/cloud-browser/pages/{target_id}/stream")
async def stream_page(target_id: str, request: Request):
    """Frames from the hosted browser as server-sent events, paced for a login flow."""
    operator = _operator(request)
    _enabled()
    session = await _live_session(operator, target_id)
    queue = session.add_subscriber()
    log_manager.add_log("Cloud Browser", _label(operator), "Started a live browser view", "info")

    async def events():
        try:
            yield _sse({
                "type": "ready",
                "target_id": target_id,
                "session": session.frames,
                "watchers": len(session.subscribers),
            })
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=STREAM_HEARTBEAT_SECONDS)
                except TimeoutError:
                    # A static page repaints rarely; the heartbeat is what keeps the
                    # Worker and the container from treating an idle view as dead.
                    payload = {"type": "ping", "frames": session.frames}
                yield _sse(payload)
                if payload.get("type") == "closed":
                    return
        finally:
            session.drop_subscriber(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class MouseInput(BaseModel):
    # Positions are fractions of the visible page, so the view works at any size
    # and the browser, not the dashboard, decides where the pointer lands.
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    action: Literal["click", "double", "right", "move", "scroll"] = "click"
    delta_x: float = Field(default=0.0, ge=-4000, le=4000)
    delta_y: float = Field(default=0.0, ge=-4000, le=4000)


_MOUSE_BUTTON = {"click": "left", "double": "left", "right": "right", "move": "left", "scroll": "left"}


@router.post("/api/cloud-browser/pages/{target_id}/mouse")
async def control_mouse(target_id: str, body: MouseInput, request: Request):
    operator = _operator(request)
    _enabled()
    async with _client_for(operator) as client:
        try:
            width, height = await client.viewport_size(target_id)
            x, y = round(body.x * width, 1), round(body.y * height, 1)
            if body.action == "move":
                await client.move_mouse(target_id, x, y)
            elif body.action == "scroll":
                await client.scroll_page(target_id, x, y, body.delta_x, body.delta_y)
            else:
                await client.click_mouse(
                    target_id, x, y, button=_MOUSE_BUTTON[body.action],
                    click_count=2 if body.action == "double" else 1,
                )
        except CdpError as exc:
            raise HTTPException(502, str(exc)) from None
    log_manager.add_log("Cloud Browser", _label(operator), f"Sent a {body.action} to a page", "info")
    return {"success": True, "viewport": {"width": width, "height": height}}


class ReelInput(BaseModel):
    recording: bool


@router.post("/api/cloud-browser/pages/{target_id}/reel")
async def set_reel(target_id: str, body: ReelInput, request: Request):
    """Keep recent frames in memory while the live view is open."""
    operator = _operator(request)
    _enabled()
    session = _sessions.get(operator)
    if session is None or session.client.is_closed:
        raise HTTPException(409, "Open the live view before recording.")
    if session.target_id != target_id:
        raise HTTPException(409, "That page is not the one being viewed.")
    session.recording = body.recording
    if body.recording:
        session.reel.clear()
        session.reel_bytes = 0
    log_manager.add_log("Cloud Browser", _label(operator), "Recording started" if body.recording else "Recording stopped", "info")
    return {"success": True, **_live_state(operator)}


@router.get("/api/cloud-browser/pages/{target_id}/reel")
async def download_reel(target_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    session = _sessions.get(operator)
    if session is None or session.target_id != target_id:
        raise HTTPException(409, "Open the live view before downloading a recording.")
    if not session.reel:
        raise HTTPException(409, "No frames have been recorded yet.")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for position, raw in enumerate(session.reel, start=1):
            bundle.writestr(f"frame-{position:05d}.jpg", raw)
    data = archive.getvalue()
    safe_target = SAFE_NAME.sub("", target_id)[:40] or "page"
    log_manager.add_log("Cloud Browser", _label(operator), f"Downloaded {len(session.reel)} recorded frames", "info")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="cloud-browser-{safe_target}.zip"'},
    )


@router.delete("/api/cloud-browser/pages/{target_id}/reel")
async def discard_reel(target_id: str, request: Request):
    operator = _operator(request)
    _enabled()
    session = _sessions.get(operator)
    if session and session.target_id == target_id:
        session.reel.clear()
        session.reel_bytes = 0
        session.recording = False
    return {"success": True, **_live_state(operator)}


@router.post("/api/cloud-browser/live/close")
async def close_live_view(request: Request):
    """Stop streaming and release the connection without closing the browser page."""
    operator = _operator(request)
    _enabled()
    session = _sessions.get(operator)
    if session:
        await session.teardown()
    log_manager.add_log("Cloud Browser", _label(operator), "Closed the live browser view", "info")
    return {"success": True, **_live_state(operator)}
