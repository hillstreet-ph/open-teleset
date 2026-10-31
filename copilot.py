"""Draft-only natural-language copilot for the dashboard.

The copilot never performs an action. It turns an operator instruction into a
draft that the UI prefills into the existing, approval-gated forms: templates,
schedules (approval reference + 1-20 consenting recipients) and BulkSend
(explicit consent checkbox). Account creation, number purchase, group joining,
member adding and messaging scraped/cold audiences are intentionally refused.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(tags=["copilot"])

_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_NUMERIC_ID_RE = re.compile(r"^\d{5,20}$")
_TIME_RE = re.compile(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?\b", re.I)
_HOUR_RE = re.compile(r"\bat\s+(\d{1,2})\s*(am|pm)\b", re.I)
_QUOTED_RE = re.compile(r"[\"“”]([^\"“”]{2,})[\"“”]")
_NAME_RE = re.compile(
    r"\b(?:named|called|name)\b[^A-Za-z0-9]{0,8}([^\"“”'\n,;.:]{1,60})", re.I
)
_RECIPIENTS_RE = re.compile(r"\brecipients?\b\s*(?:are|is|:|-)?\s*", re.I)
_CONTENT_RE = re.compile(
    r"\b(?:message|saying|that says|content|text|body|send)\b\s*[:=-]\s*", re.I
)
_REPEAT_RE = re.compile(r"\b(daily|every\s+day|weekly|every\s+week|workdays?|weekdays?|once)\b", re.I)

_BLOCKED: list[tuple] = [
    (r"\b(?:buy|purchase|rent)\b.{0,40}\b(?:number|numbers|phone|sim)\b", "buy phone numbers"),
    (r"\b(?:auto(?:matic(?:ally)?)?\s+)?(?:register|sign\s*up)\b.{0,40}\b(?:account|accounts)\b",
     "register Telegram accounts"),
    (r"\bcreate\b.{0,30}\bTelegram\s+accounts?\b", "create Telegram accounts"),
    (r"\b(?:auto(?:matic(?:ally)?)?\s+)?join\b.{0,40}\b(?:group|groups|channel|channels)\b",
     "join groups or channels automatically"),
    (r"\badd\b.{0,30}\bmembers?\b", "add members to groups"),
    (r"\bscrap(?:e|ed|ing)\b.{0,60}\b(?:send|message|dm)\b", "message scraped people"),
    (r"\b(?:send|message|dm)\b.{0,60}\bscrap(?:e|ed|ing)\b", "message scraped people"),
    (r"\bauto(?:matic(?:ally)?)?\s+(?:register|login|log\s+in)\b", "automate account registration or login"),
    (r"\bmass\s+(?:dm|dms|message|messages|send)\b", "mass-message cold audiences"),
    (r"\b(?:random|fake)\b.{0,30}\b(?:names?|usernames?|identit(?:y|ies))\b", "generate fake identities"),
]

PRESETS: list[dict[str, str]] = [
    {
        "id": "template-welcome",
        "group": "Templates",
        "title": "Welcome reply",
        "description": "A reusable greeting for people who contacted you first.",
        "instruction": "Create a template named Welcome that says: Hi {name}, thanks for reaching out. How can I help today?",
    },
    {
        "id": "template-followup",
        "group": "Templates",
        "title": "Follow-up",
        "description": "A short check-in for an existing conversation.",
        "instruction": "Create a template named Follow-up that says: Hi {name}, just checking in. Would you like me to send the latest details?",
    },
    {
        "id": "template-support",
        "group": "Templates",
        "title": "Support hours",
        "description": "Set expectations for response time.",
        "instruction": "Create a template named Support hours that says: Our team replies within one business day. Please share your question here.",
    },
    {
        "id": "schedule-daily",
        "group": "Schedules",
        "title": "Daily update",
        "description": "A daily message to consenting recipients. Add an approval reference and recipients in the form.",
        "instruction": "Schedule a daily message at 10:00 saying: Hi {name}, here is today's update.",
    },
    {
        "id": "schedule-weekly",
        "group": "Schedules",
        "title": "Weekly summary",
        "description": "A weekly message to consenting recipients. Add an approval reference and recipients in the form.",
        "instruction": "Schedule a weekly message at 09:30 saying: Hi {name}, here is this week's summary.",
    },
    {
        "id": "task-single",
        "group": "Tasks",
        "title": "Prepare a single send",
        "description": "Opens BulkSend prefilled. You confirm consent and press Start.",
        "instruction": "Create a task to send: Hi {name}, thanks for your message. I will follow up shortly.",
    },
    {
        "id": "task-followup",
        "group": "Tasks",
        "title": "Prepare a follow-up send",
        "description": "Opens BulkSend prefilled. You confirm consent and press Start.",
        "instruction": "Create a task to send: Hi {name}, our team is available this week. Would you like a call?",
    },
]

_HELP_EXAMPLES = [
    'Create a template named Welcome that says: Hi {name}, thanks for reaching out.',
    'Schedule a daily message at 10:00 saying: Hi {name}, here is today\'s update.',
    'Create a task to send: Hi {name}, I will follow up shortly.',
]


class CopilotRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)


def _blocked_reason(text: str) -> str | None:
    lowered = text.lower()
    for pattern, label in _BLOCKED:
        if re.search(pattern, lowered, re.I):
            return label
    return None


def _command_region(text: str) -> str:
    """Text before the message/recipient body, where intent words live."""
    cut = len(text)
    for marker in (_CONTENT_RE, _RECIPIENTS_RE):
        match = marker.search(text)
        if match and match.start() < cut:
            cut = match.start()
    return text[:cut]


def _recipients(text: str) -> list[str]:
    match = _RECIPIENTS_RE.search(text)
    if not match:
        return []
    tail = text[match.end():]
    stop = len(tail)
    for marker in (_CONTENT_RE,):
        found = marker.search(tail)
        if found:
            stop = min(stop, found.start())
    tail = tail[:stop].split("\n", 1)[0]
    found: list[str] = []
    for raw in re.split(r"[,;\s]+", tail):
        token = raw.strip().strip("()[]<>.")
        if not token:
            continue
        username = _USERNAME_RE.match(token)
        if username:
            value = username.group(1)
        elif _NUMERIC_ID_RE.match(token):
            value = token
        else:
            continue
        if value.lower() not in {item.lower() for item in found}:
            found.append(value)
    return found


def _message(text: str) -> str:
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    match = _CONTENT_RE.search(text)
    if not match:
        return ""
    body = text[match.end():]
    body = _RECIPIENTS_RE.split(body, maxsplit=1)[0]
    return body.strip().strip("\"'“”").strip()


def _name(text: str, fallback: str) -> str:
    match = _NAME_RE.search(text)
    if not match:
        return fallback
    name = match.group(1).strip().strip("\"'“”").strip()
    words = name.split()
    for index, word in enumerate(words):
        if index and word.lower() in {"that", "which", "saying", "with", "message", "content", "text"}:
            name = " ".join(words[:index])
            break
    return name.strip() or fallback


def _approval(text: str) -> str:
    """Read an approval reference without a backtracking-prone regex."""
    match = re.search(r"\bapproval\b", text, re.I)
    if not match:
        return ""
    tail = text[match.end():].lstrip()
    for prefix in ("id", "reference"):
        if tail.lower().startswith(prefix):
            tail = tail[len(prefix):].lstrip()
            break
    if tail[:1] in (":", "=", "-"):
        tail = tail[1:].lstrip()
    token = re.match(r"[A-Za-z0-9_-]{2,64}", tail)
    return token.group(0) if token else ""


def _time(text: str) -> tuple | None:
    match = _TIME_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        suffix = (match.group(3) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    match = _HOUR_RE.search(text)
    if match:
        hour = int(match.group(1))
        if (match.group(2) or "").lower() == "pm" and hour < 12:
            hour += 12
        if 0 <= hour <= 23:
            return hour, 0
    return None


def _repeat(text: str) -> str:
    match = _REPEAT_RE.search(text)
    if not match:
        return "once"
    word = match.group(1).lower()
    if word.startswith("week") and "day" not in word:
        return "weekly"
    if word.startswith("week"):
        return "workday"
    if word.startswith("daily") or word.startswith("every"):
        return "daily"
    return "once"


def _draft_template(text: str) -> dict[str, Any]:
    content = _message(text)
    name = _name(text, "Copilot template")
    warnings = []
    if not content:
        warnings.append("Add the message text before saving this template.")
    return {
        "action": "template",
        "title": f"Create template: {name}",
        "summary": "Adds a reusable message template. Nothing is sent.",
        "payload": {"name": name, "content": content, "category": "Copilot", "variables": []},
        "warnings": warnings,
        "requires_confirmation": True,
    }


def _draft_schedule(text: str) -> dict[str, Any]:
    command = _command_region(text)
    message = _message(text)
    recipients = _recipients(text)
    hour, minute = _time(command) or (10, 0)
    approval = _approval(command)
    warnings = []
    if not message:
        warnings.append("Add the message text before creating this schedule.")
    if not approval:
        warnings.append("An approval reference is required before the schedule can run.")
    if not recipients:
        warnings.append("Add 1-20 consenting recipients in the schedule form.")
    elif len(recipients) > 20:
        warnings.append("The schedule form accepts up to 20 recipients; use the first 20 shown.")
        recipients = recipients[:20]
    name = _name(command, "Copilot schedule")
    return {
        "action": "schedule",
        "title": f"Schedule: {name}",
        "summary": "Opens the schedule form prefilled. Review it and press Create there.",
        "payload": {
            "name": name,
            "action": "send_message",
            "message": message,
            "recipients": recipients,
            "repeat": _repeat(command),
            "hour": hour,
            "minute": minute,
            "interval": 3000,
            "approvalId": approval if approval else "",
            "autoDedup": True,
            "validateUsernames": True,
        },
        "warnings": warnings,
        "requires_confirmation": True,
    }


def _draft_task(text: str) -> dict[str, Any]:
    message = _message(text)
    recipients = _recipients(text)
    warnings = [
        "This only opens BulkSend prefilled. Confirm consent and press Start BulkSend yourself."
    ]
    if not message:
        warnings.append("Add the message text before sending.")
    if not recipients:
        warnings.append("Add consenting recipients in BulkSend.")
    return {
        "action": "task",
        "title": "Prepare a BulkSend task",
        "summary": "Opens BulkSend prefilled for review. Nothing is sent from the Copilot.",
        "payload": {
            "message": message,
            "targets": recipients,
            "delay": 0,
            "consentConfirmed": False,
        },
        "warnings": warnings,
        "requires_confirmation": True,
    }


def _unsupported(label: str) -> dict[str, Any]:
    return {
        "action": "unsupported",
        "title": "Not available",
        "summary": (
            f"The Copilot cannot {label}. Account creation, number purchase, automatic "
            "joining/adding and cold or scraped outreach violate Telegram's terms and are "
            "not offered. It can draft templates, schedules and opt-in sends instead."
        ),
        "payload": {},
        "warnings": [],
        "requires_confirmation": False,
    }


def _help() -> dict[str, Any]:
    return {
        "action": "help",
        "title": "Draft a template, schedule or opt-in send",
        "summary": (
            "Describe what you want in plain language. The Copilot returns a draft; the "
            "existing approval and consent gates still apply before anything runs."
        ),
        "payload": {},
        "warnings": [],
        "requires_confirmation": False,
        "examples": _HELP_EXAMPLES,
    }


def build_draft(instruction: str) -> dict[str, Any]:
    """Parse an instruction into a draft. Pure: no I/O and no side effects."""
    text = (instruction or "").strip()
    if not text:
        return _help()

    lowered = text.lower()
    is_schedule = bool(re.search(r"\b(schedule|daily|weekly|every\s+day|every\s+week|workday)\b", lowered))
    is_template = "template" in lowered and not is_schedule
    is_task = bool(re.search(r"\b(task|bulk\s*send|send\s+(?:a\s+)?message)\b", lowered)) and not is_schedule

    if not (is_schedule or is_template or is_task):
        reason = _blocked_reason(text)
        if reason:
            return _unsupported(reason)
        return _help()

    reason = _blocked_reason(_command_region(text))
    if reason:
        return _unsupported(reason)

    if is_schedule:
        return _draft_schedule(text)
    if is_template:
        return _draft_template(text)
    return _draft_task(text)


@router.post("/api/copilot/draft")
async def draft_command(request: CopilotRequest):
    return {"success": True, "draft": build_draft(request.instruction), "presets": PRESETS}
