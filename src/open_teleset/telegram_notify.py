"""Send internal alerts to a personal Telegram chat, group or forum topic.

The bot token is encrypted at rest and never returned by the API. Delivery is
best effort: a notification failure never changes the outcome of the job that
triggered it.
"""

from __future__ import annotations

import asyncio
import html
import re

import httpx

from open_teleset.operator_store import read_json, read_secret, write_json, write_secret

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TOKEN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def _config(operator: str) -> dict:
    return read_json("notifications", {}).get(operator, {})


def get_settings(operator: str) -> dict:
    config = _config(operator)
    return {
        "enabled": bool(config.get("enabled")),
        "chat_id": config.get("chat_id", ""),
        "topic_id": config.get("topic_id", ""),
        "events": config.get("events", []),
        "has_token": bool(config.get("has_token")),
    }


def save_settings(operator: str, *, enabled=None, chat_id=None, topic_id=None,
                  events=None, bot_token=None) -> dict:
    all_config = read_json("notifications", {})
    config = all_config.get(operator, {})
    if enabled is not None:
        config["enabled"] = bool(enabled)
    if chat_id is not None:
        config["chat_id"] = str(chat_id).strip()
    if topic_id is not None:
        config["topic_id"] = str(topic_id).strip()
    if events is not None:
        config["events"] = [str(event) for event in events][:20]
    if bot_token is not None:
        token = str(bot_token).strip()
        if token and not _TOKEN.match(token):
            raise ValueError("That does not look like a Telegram bot token")
        secrets = read_secret("notification_tokens", {}) or {}
        if token:
            secrets[operator] = token
            config["has_token"] = True
        else:
            secrets.pop(operator, None)
            config["has_token"] = False
        write_secret("notification_tokens", secrets)
    all_config[operator] = config
    write_json("notifications", all_config)
    return get_settings(operator)


def _token(operator: str) -> str:
    return (read_secret("notification_tokens", {}) or {}).get(operator, "")


async def send_message(operator: str, text: str, *, chat_id=None, topic_id=None) -> dict:
    config = _config(operator)
    token = _token(operator)
    target = (chat_id if chat_id is not None else config.get("chat_id", "")).strip()
    thread = (topic_id if topic_id is not None else config.get("topic_id", "")).strip()
    if not token:
        return {"success": False, "detail": "Add a bot token in Settings → Notifications."}
    if not target:
        return {"success": False, "detail": "Add a chat ID in Settings → Notifications."}
    payload = {
        "chat_id": target,
        "text": html.escape(text)[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread:
        if not thread.isdigit():
            return {"success": False, "detail": "Topic ID must be numeric."}
        payload["message_thread_id"] = int(thread)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(_API.format(token=token), json=payload)
    except httpx.HTTPError as exc:
        return {"success": False, "detail": f"Telegram unreachable ({type(exc).__name__})"}
    if response.status_code == 200 and response.json().get("ok"):
        return {"success": True, "detail": "Test message delivered."}
    description = ""
    try:
        description = str(response.json().get("description") or "")
    except ValueError:
        description = ""
    return {"success": False, "detail": description or f"Telegram returned HTTP {response.status_code}."}


async def notify_event(operator: str, event: str, text: str) -> None:
    """Send an enabled event notification without ever raising into a worker."""
    try:
        config = _config(operator)
        if not config.get("enabled") or not config.get("has_token"):
            return
        subscribed = config.get("events") or []
        if subscribed and event not in subscribed:
            return
        await send_message(operator, text)
    except Exception:
        return


def queue_event(operator: str, event: str, text: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(notify_event(operator, event, text))
