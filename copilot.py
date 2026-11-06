"""Ops Copilot for the dashboard.

Two tiers, one rule: the Copilot never reaches another person or spends money
without the operator pressing the button.

Tier 1 - internal, executable. Commands that touch only the operator's own
encrypted workspace: save or search a knowledge note, look for repeated wording,
preview which Auto Reply rule would answer a message. The server runs these and
shows the result. Nothing leaves the workspace and nothing is sent.

Tier 2 - ops, prefilled handoff. The product ships a curated, bounded preset per
surface (Scraper, Adder, Auto Join, BulkSend, Auto Reply, Account Factory,
Automation, Knowledge, Inbox). The Copilot opens that surface with the operator's
own words applied inside hard bounds and stops at the existing gate. Consent stays
unchecked, the operator's pacing defaults are re-asserted, and the agent never
carries member lists, phone numbers or message bodies to strangers.

Account creation, number purchase, automatic joining or adding, and cold or
scraped outreach stay refused. The curated presets do not change that: they open
the operator's own gated form, they do not press it.

Every exchange is kept as a conversation turn so the panel can show the history, a
turn the operator applies is offered to the knowledge base, and the presets the
operator actually applies float to the top of the runbook.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import knowledge
from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["copilot"])

HISTORY_STORE = "copilot_history"
PRESET_STORE = "copilot_presets"
USAGE_STORE = "copilot_preset_usage"
MAX_TURNS = 100
MAX_CUSTOM_PRESETS = 40
INSTRUCTION_PREVIEW = 300
OUTCOMES = ("drafted", "applied", "discarded")

# Attachments live in this process only, scoped to the operator, and never touch
# the disk. Text is read in-process; anything else is kept as a descriptor and
# named as unreadable rather than pretending it was understood.
MAX_ATTACHMENTS = 8
ATTACHMENT_FILE_BYTES = 4 * 1024 * 1024
ATTACHMENT_TOTAL_BYTES = 16 * 1024 * 1024
ATTACHMENT_CHARS = 12000
_ATTACHMENTS: dict[str, dict[str, dict]] = {}
_ATTACHMENT_LOCK = threading.Lock()

ATTACHMENT_KINDS = (
    (".zip", "archive"),
    ((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".svg"), "image"),
    ((".mp4", ".mov", ".webm", ".mkv", ".avi"), "video"),
    ((".pdf",), "pdf"),
    ((".csv", ".tsv", ".xls", ".xlsx", ".ods"), "spreadsheet"),
    ((".doc", ".docx", ".odt", ".rtf", ".pages"), "document"),
    ((".mp3", ".wav", ".ogg", ".m4a", ".opus"), "audio"),
)


PRESET_TITLE = 60
PRESET_GROUP = 40
PRESET_DESCRIPTION = 200
PRESET_INSTRUCTION = 500
PRESET_FIELDS = ("group", "title", "description", "instruction")

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
    (r"\bauto(?:matic(?:ally))?\s+(?:register|login|log\s+in)\b", "automate account registration or login"),
    (r"\bmass\s+(?:dm|dms|message|messages|send)\b", "mass-message cold audiences"),
    (r"\b(?:random|fake)\b.{0,30}\b(?:names?|usernames?|identit(?:y|ies))\b", "generate fake identities"),
    (r"\b(?:avoid|bypass|evade|dodge|beat|get\s+around)\b.{0,40}"
     r"\b(?:bans?|flags?|limits?|restrictions?|detection|moderation)\b",
     "evade Telegram's limits or bans"),
    (r"\b(?:warm[\s-]?up|warming)\b.{0,40}\baccounts?\b", "warm up accounts to avoid detection"),
    (r"\b(?:accounts?|prox(?:y|ies)|sessions?)\b.{0,50}\brotat(?:e|ion|ing)\b", "rotate accounts or proxies"),
    (r"\brotat(?:e|ion|ing)\b.{0,50}\b(?:bans?|flags?|limits?|detection)\b",
     "rotate accounts or proxies to dodge limits"),
    (r"\b(?:auto(?:matic(?:ally)?)?\s+)?switch\b.{0,40}\baccounts?\b.{0,40}\b(?:limit|ban|flag)\b",
     "switch accounts to dodge limits"),
    (r"\b(?:fingerprint|anti[\s-]?detect|stealth\s+mode)\b", "evade platform detection"),
]

# ============ the ops surfaces ============
#
# Each surface names the dashboard tab, the button the operator still has to press,
# and the fields the Copilot may prefill. Nothing here runs anything.

OPS_SURFACES: dict[str, dict[str, Any]] = {
    "scraper": {
        "tab": "scraper",
        "title": "Collect members",
        "gate": "gate-scraper",
        "gate_label": "Scrape members",
        "summary": "Opens the member collector prefilled. Nothing is collected until you press the button.",
        "note": "The collector only reads a group your account already has access to.",
        "fields": {"limit": 500},
    },
    "adder": {
        "tab": "adder",
        "title": "Add members who agreed",
        "gate": "gate-adder",
        "gate_label": "Start Member Adder",
        "summary": "Opens the member adder prefilled. Consent stays unchecked and you press the button.",
        "note": "Only recipients who agreed may be added, and the delay stays at or above 35 seconds.",
        "fields": {"delay": 38},
    },
    "autojoin": {
        "tab": "autojoin",
        "title": "Join groups",
        "gate": "gate-autojoin",
        "gate_label": "Start Auto Join",
        "summary": "Opens the group joiner prefilled. Nothing is joined until you press the button.",
        "note": "Groups are joined one at a time, with a delay between them.",
        "fields": {"limit": 24, "delay": 38},
    },
    "bulksend": {
        "tab": "bulksend",
        "title": "Prepare an opt-in send",
        "gate": "gate-bulksend",
        "gate_label": "Start BulkSend",
        "summary": "Opens BulkSend prefilled. Consent stays unchecked and you press Start.",
        "note": "Only people who agreed to be messaged may be on the list.",
        "fields": {"delay": 45, "perAccountLimit": 8},
    },
    "autoreply": {
        "tab": "autoreply",
        "title": "Set up automatic replies",
        "gate": "gate-autoreply",
        "gate_label": "Save settings",
        "summary": "Opens the Auto Reply settings prefilled. Nothing answers until you switch the listener on.",
        "note": "Replies only reach people who wrote to you first.",
        "fields": {"poll": 90, "cap": 20, "mode": "rules"},
    },
    "factory": {
        "tab": "factory",
        "title": "Run the account factory",
        "gate": "gate-factory",
        "gate_label": "Run account factory",
        "summary": "Opens the account factory prefilled. No number is bought until you press the button.",
        "note": "Buy numbers and register accounts only where you are authorised to do so.",
        "fields": {"maxPrice": 0.5, "otpAttempts": 30},
    },
    "automation": {
        "tab": "automation",
        "title": "Refresh the automation status",
        "gate": "gate-automation",
        "gate_label": "Refresh status",
        "summary": "Opens the automation panel and refreshes its status. Nothing is started.",
        "note": "The factory run itself is started by you, in the panel.",
        "fields": {},
    },
    "knowledge": {
        "tab": "knowledge",
        "title": "Export the knowledge base",
        "gate": "gate-knowledge",
        "gate_label": "Export ZIP",
        "summary": "Opens the knowledge base. The export is written by you, in the panel.",
        "note": "Credential-shaped values stay masked in the export.",
        "fields": {},
    },
    "inbox": {
        "tab": "inbox",
        "title": "Watch an account for new messages",
        "gate": "gate-inbox",
        "gate_label": "Watch account",
        "summary": "Opens the inbox prefilled. Nothing is watched until you confirm.",
        "note": "Only the accounts you opt in are watched.",
        "fields": {},
    },
}

# The agent may tune a field only inside these bounds, and only a field the
# surface already declares.
FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "limit": (1, 2000),
    "delay": (0, 300),
    "perAccountLimit": (1, 50),
    "maxPrice": (0.02, 5.0),
    "otpAttempts": (1, 120),
    "poll": (30, 900),
    "cap": (0, 200),
}
SURFACE_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("adder", "delay"): (35, 300),
    ("autojoin", "delay"): (30, 3600),
    ("autojoin", "limit"): (1, 50),
}

_FIELD_PATTERNS: dict[str, re.Pattern] = {
    "perAccountLimit": re.compile(
        r"\b(?:per[\s-]account\s+limit|limit\s+per\s+account)\b\s*(?:of|=|:|to)?\s*(\d{1,4})\b", re.I),
    "delay": re.compile(r"\bdelay\b\s*(?:of|=|:|to)?\s*(\d{1,5})\b", re.I),
    "maxPrice": re.compile(
        r"\b(?:max(?:imum)?\s*price|price\s*cap)\b\s*(?:of|=|:|to)?\s*\$?\s*(\d+(?:\.\d+)?)\b", re.I),
    "otpAttempts": re.compile(r"\botp\s*(?:attempts?|tries)\b\s*(?:of|=|:|to)?\s*(\d{1,4})\b", re.I),
    "poll": re.compile(
        r"\b(?:check\s*every|poll(?:ing)?(?:\s*interval)?)\b\s*(?:of|=|:|to)?\s*(\d{1,4})\b", re.I),
    "cap": re.compile(r"\b(?:daily\s*limit|cap)\b\s*(?:of|=|:|to)?\s*(\d{1,4})\b", re.I),
    "limit": re.compile(r"(?<!account )\blimit\b\s*(?:of|=|:|to)?\s*(\d{1,6})\b", re.I),
}

# ============ the internal commands ============
#
# These run on the server and only read or write the operator's own workspace.

INTERNAL_COMMANDS: dict[str, dict[str, str]] = {
    "knowledge-import": {
        "title": "Import the attached files",
        "summary": "Stores the readable text of the attached files in your own encrypted knowledge base.",
    },
    "knowledge-note": {
        "title": "Save a note in your knowledge base",
        "summary": "Stores the note in your own encrypted knowledge base.",
    },
    "knowledge-search": {
        "title": "Search your knowledge base",
        "summary": "Ranks your own notes against the question. Nothing leaves this workspace.",
    },
    "patterns-refresh": {
        "title": "Look for repeated wording",
        "summary": "Re-reads your notes and proposes reusable phrases from the repeated ones.",
    },
    "autoreply-test": {
        "title": "See which Auto Reply rule would answer",
        "summary": "Matches a test message against your rules. Nothing is sent.",
    },
}

_INTERNAL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("knowledge-import", (
        r"\bimport\b.*\b(?:knowledge|notes?|files?|documents?|attachments?|zip)\b",
        r"\b(?:knowledge|notes?)\b.*\bimport\b",
        r"\b(?:add|save|store|load)\b\s+(?:these|those|the|my|this)\s+"
        r"(?:files?|documents?|attachments?)\b",
    )),
    ("knowledge-search", (
        r"\bsearch\b.*\b(?:knowledge|notes?)\b",
        r"\b(?:knowledge|notes?)\b.*\bsearch\b",
        r"\blook\s*up\b.*\b(?:knowledge|notes?)\b",
        r"\bfind\b.*\b(?:knowledge|notes?)\b",
    )),
    ("knowledge-note", (
        r"\bremember\b",
        r"\bnote\s+down\b",
        r"\bnote\s+(?:this|that)\b",
        r"\b(?:add|save|write|store|put)\s+(?:a|an|the|this|that|it)?\s*note\b",
        r"\b(?:add|save|write|store|put)\s+(?:this|that|it)\s+to\s+(?:my|the)\s+(?:knowledge|notes?)\b",
    )),
    ("patterns-refresh", (
        r"\brepeated\s+wording\b",
        r"\bdetect\b.*\b(?:phrases?|patterns?|wording)\b",
        r"\bcommon\s+phrases?\b",
    )),
    ("autoreply-test", (
        r"\bwhich\s+rule\b",
        r"\btest\b.*\b(?:auto[\s-]?reply|rule)\b",
        r"\bauto[\s-]?reply\b.*\btest\b",
    )),
)

# The surfaces a free-text instruction may open. The Adder and Auto Join
# surfaces are preset-only on purpose: their prose forms stay refused.
_OPS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("factory", (r"\baccount\s+factory\b",)),
    ("autoreply", (r"\bauto[\s-]?reply\b", r"\bautomatic\s+repl(?:y|ies)\b")),
    ("scraper", (r"\bcollect\s+(?:the\s+)?members?\b", r"\bscrape\s+members?\b",
                 r"\bmember\s+scrap(?:e|er|ing)\b")),
    ("knowledge", (r"\bknowledge\s*base\b", r"\bexport\b.*\bknowledge\b")),
    ("automation", (r"\bautomation\b",)),
    ("inbox", (r"\binbox\b",)),
)

# Plain page switches. They only match behind an explicit open/show verb, so a
# sentence that merely mentions the accounts page still drafts something real.
_NAV_VERB = re.compile(r"\b(?:open|show|go\s+to|switch\s+to|take\s+me\s+to|navigate\s+to|view)\b", re.I)
_PAGE_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("accounts", "Accounts", r"\baccounts?\b"),
    ("contacts", "Contacts", r"\bcontacts?\b"),
    ("proxies", "Proxies", r"\bprox(?:y|ies)\b"),
    ("cloudphone", "Cloud Phone", r"\b(?:cloud\s*phone|recovery)\b"),
    ("cloudbrowser", "Cloud Browser", r"\bcloud\s*browser\b"),
    ("templates", "Templates", r"\btemplates?\b"),
    ("schedule", "Schedule", r"\bschedules?\b"),
    ("tasks", "Tasks", r"\btasks?\b"),
    ("health", "Health", r"\bhealth\b"),
    ("stats", "Statistics", r"\bstats\b|\bstatistics\b"),
    ("logs", "Logs", r"\blogs?\b"),
    ("integrations", "Integrations", r"\bintegrations?\b|\bconnections?\b"),
    ("copilot", "Ops Copilot", r"\bcopilot\b"),
    ("overview", "Overview", r"\boverview\b|\bdashboard\b"),
)

PRESETS: list[dict[str, Any]] = [
    {
        "id": "template-welcome",
        "group": "Templates",
        "kind": "draft",
        "title": "Welcome reply",
        "description": "A reusable greeting for people who contacted you first.",
        "instruction": "Create a template named Welcome that says: Hi {name}, thanks for reaching out. How can I help today?",
    },
    {
        "id": "template-followup",
        "group": "Templates",
        "kind": "draft",
        "title": "Follow-up",
        "description": "A short check-in for an existing conversation.",
        "instruction": "Create a template named Follow-up that says: Hi {name}, just checking in. Would you like me to send the latest details?",
    },
    {
        "id": "template-support",
        "group": "Templates",
        "kind": "draft",
        "title": "Support hours",
        "description": "Set expectations for response time.",
        "instruction": "Create a template named Support hours that says: Our team replies within one business day. Please share your question here.",
    },
    {
        "id": "schedule-daily",
        "group": "Schedules",
        "kind": "draft",
        "title": "Daily update",
        "description": "A daily message to consenting recipients. Add an approval reference and recipients in the form.",
        "instruction": "Schedule a daily message at 10:00 saying: Hi {name}, here is today's update.",
    },
    {
        "id": "schedule-weekly",
        "group": "Schedules",
        "kind": "draft",
        "title": "Weekly summary",
        "description": "A weekly message to consenting recipients. Add an approval reference and recipients in the form.",
        "instruction": "Schedule a weekly message at 09:30 saying: Hi {name}, here is this week's summary.",
    },
    {
        "id": "task-single",
        "group": "Tasks",
        "kind": "draft",
        "title": "Prepare a single send",
        "description": "Opens BulkSend prefilled. You confirm consent and press Start.",
        "instruction": "Create a task to send: Hi {name}, thanks for your message. I will follow up shortly.",
    },
    {
        "id": "task-followup",
        "group": "Tasks",
        "kind": "draft",
        "title": "Prepare a follow-up send",
        "description": "Opens BulkSend prefilled. You confirm consent and press Start.",
        "instruction": "Create a task to send: Hi {name}, our team is available this week. Would you like a call?",
    },
    {
        "id": "scraper-members",
        "group": "Outreach",
        "kind": "ops",
        "surface": "scraper",
        "title": "Collect members",
        "description": "Opens the member collector prefilled with your usual limit.",
        "instruction": "Collect the members of a group I already have access to.",
    },
    {
        "id": "adder-consent",
        "group": "Outreach",
        "kind": "ops",
        "surface": "adder",
        "title": "Add members who agreed",
        "description": "Opens the member adder prefilled. Consent stays unchecked.",
        "instruction": "Add the members who agreed to join, with the delay I use.",
    },
    {
        "id": "autojoin-links",
        "group": "Outreach",
        "kind": "ops",
        "surface": "autojoin",
        "title": "Join groups",
        "description": "Opens the group joiner prefilled with your usual pace.",
        "instruction": "Join the groups I list, one at a time.",
    },
    {
        "id": "bulksend-optin",
        "group": "Outreach",
        "kind": "ops",
        "surface": "bulksend",
        "title": "Prepare an opt-in send",
        "description": "Opens BulkSend prefilled. Consent stays unchecked and you press Start.",
        "instruction": "Send an opt-in message to the recipients who agreed.",
    },
    {
        "id": "autoreply-rules",
        "group": "Auto Reply",
        "kind": "ops",
        "surface": "autoreply",
        "title": "Set up automatic replies",
        "description": "Opens the Auto Reply settings prefilled with your usual check interval and daily limit.",
        "instruction": "Set up the automatic replies for the accounts I watch.",
    },
    {
        "id": "autoreply-test",
        "group": "Auto Reply",
        "kind": "command",
        "command": "autoreply-test",
        "title": "Test a message against the rules",
        "description": "Shows which rule would answer a test message. Nothing is sent.",
        "instruction": "Which rule would answer the message: how much does it cost?",
    },
    {
        "id": "factory-run",
        "group": "Automation",
        "kind": "ops",
        "surface": "factory",
        "title": "Run the account factory",
        "description": "Opens the account factory prefilled with your price cap. Nothing is bought until you press it.",
        "instruction": "Run the account factory with the price cap I use.",
    },
    {
        "id": "automation-status",
        "group": "Automation",
        "kind": "ops",
        "surface": "automation",
        "title": "Refresh the automation status",
        "description": "Opens the automation panel and refreshes its status. Nothing is started.",
        "instruction": "Show the automation status.",
    },
    {
        "id": "knowledge-note",
        "group": "Knowledge",
        "kind": "command",
        "command": "knowledge-note",
        "title": "Save a note",
        "description": "Stores a note in your own encrypted knowledge base.",
        "instruction": "Remember: orders leave the warehouse within two working days.",
    },
    {
        "id": "knowledge-search",
        "group": "Knowledge",
        "kind": "command",
        "command": "knowledge-search",
        "title": "Search your notes",
        "description": "Ranks your own notes against a question.",
        "instruction": "Search my knowledge base for the refund policy.",
    },
    {
        "id": "patterns-refresh",
        "group": "Knowledge",
        "kind": "command",
        "command": "patterns-refresh",
        "title": "Look for repeated wording",
        "description": "Re-reads your notes and proposes reusable phrases.",
        "instruction": "Look for repeated wording in my notes.",
    },
    {
        "id": "knowledge-export",
        "group": "Knowledge",
        "kind": "ops",
        "surface": "knowledge",
        "title": "Export the knowledge base",
        "description": "Opens the knowledge base. You write the zip file in the panel.",
        "instruction": "Export my knowledge base as a zip file.",
    },
    {
        "id": "inbox-refresh",
        "group": "Inbox",
        "kind": "ops",
        "surface": "inbox",
        "title": "Refresh the inbox",
        "description": "Opens the inbox and reloads the chats.",
        "instruction": "Refresh the inbox.",
    },
    {
        "id": "inbox-watch",
        "group": "Inbox",
        "kind": "ops",
        "surface": "inbox",
        "title": "Watch an account",
        "description": "Opens the inbox so you can watch one account for new messages.",
        "instruction": "Watch one account for new messages.",
    },
]

_HELP_EXAMPLES = [
    'Create a template named Welcome that says: Hi {name}, thanks for reaching out.',
    'Schedule a daily message at 10:00 saying: Hi {name}, here is today\'s update.',
    'Create a task to send: Hi {name}, I will follow up shortly.',
    'Remember: orders leave the warehouse within two working days.',
]


class CopilotRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)
    preset_id: str = Field(default="", max_length=64)
    mode: str = Field(default="ask", max_length=8)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class EnhanceBody(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class PresetBody(BaseModel):
    group: str = Field(default="", max_length=PRESET_GROUP)
    title: str = Field(default="", max_length=PRESET_TITLE)
    description: str = Field(default="", max_length=PRESET_DESCRIPTION)
    instruction: str = Field(default="", max_length=PRESET_INSTRUCTION)
    kind: str = Field(default="draft", max_length=16)
    command: str = Field(default="", max_length=40)


class PresetDraftBody(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)
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
        if tail.lower().startswith(prefix) and (len(tail) == len(prefix) or not tail[len(prefix)].isalnum()):
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
            "joining/adding, cold or scraped outreach, and anything that dodges Telegram's "
            "limits or bans break its terms and are not offered. It can draft templates, "
            "schedules, opt-in sends and internal documents instead."
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


# ============ attachments ============


def _attachment_kind(name: str) -> str:
    lowered = str(name or "").lower()
    for suffixes, kind in ATTACHMENT_KINDS:
        if lowered.endswith(tuple(suffixes) if isinstance(suffixes, tuple) else (suffixes,)):
            return kind
    return "text" if lowered.rsplit(".", 1)[-1] in {s.lstrip(".") for s in knowledge.IMPORT_SUFFIXES} else "file"


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _public_attachment(record: dict) -> dict:
    return {key: record.get(key) for key in
            ("id", "name", "mime", "size", "human", "kind", "characters", "readable", "reason")}


def _operator_attachments(operator: str) -> dict:
    with _ATTACHMENT_LOCK:
        return dict(_ATTACHMENTS.get(operator) or {})


def _attachments_by_ids(operator: str, ids: list) -> list:
    store = _operator_attachments(operator)
    return [store[str(item)] for item in (ids or []) if str(item) in store]


def _store_attachments(operator: str, uploads: list) -> list:
    """Add uploads to the operator's in-memory tray, enforcing every bound."""
    with _ATTACHMENT_LOCK:
        store = _ATTACHMENTS.setdefault(operator, {})
        if len(store) + len(uploads) > MAX_ATTACHMENTS:
            raise HTTPException(400, f"Attach at most {MAX_ATTACHMENTS} files to one prompt.")
        total = sum(int(record.get("size") or 0) for record in store.values())
        added = []
        for name, mime, data in uploads:
            if total + len(data) > ATTACHMENT_TOTAL_BYTES:
                raise HTTPException(400, f"Keep the attachments under "
                                         f"{ATTACHMENT_TOTAL_BYTES // (1024 * 1024)} MB in total.")
            total += len(data)
            label = knowledge.entry_name(name or "attachment")
            pairs, reason = knowledge.extract_texts(name, data)
            text = "\n\n".join(body for _, body in pairs).strip()
            record = {
                "id": uuid4().hex[:12],
                "name": label,
                "mime": mime or "",
                "size": len(data),
                "human": _human_size(len(data)),
                "kind": _attachment_kind(label),
                "characters": min(len(text), ATTACHMENT_CHARS),
                "readable": bool(text),
                "reason": reason or ("" if text else "no readable text"),
                "text": text[:ATTACHMENT_CHARS],
            }
            store[record["id"]] = record
            added.append(_public_attachment(record))
        return added


def _drop_attachments(operator: str, ids: list | None = None) -> int:
    with _ATTACHMENT_LOCK:
        store = _ATTACHMENTS.get(operator) or {}
        targets = [str(i) for i in ids] if ids else list(store)
        removed = sum(1 for item in targets if store.pop(item, None) is not None)
        if not store:
            _ATTACHMENTS.pop(operator, None)
        return removed


# ============ navigation and follow-ups ============


def _navigate_draft(page: str, label: str) -> dict[str, Any]:
    return {
        "action": "navigate",
        "title": f"Open {label}",
        "summary": f"Switches the dashboard to {label}. Nothing else runs.",
        "payload": {"tab": page, "label": label},
        "warnings": [],
        "requires_confirmation": False,
    }


def _navigate_intent(text: str) -> tuple[str, str] | None:
    if not _NAV_VERB.search(text):
        return None
    for page, label, pattern in _PAGE_ALIASES:
        if re.search(pattern, text, re.I):
            return page, label
    return None


def _next_steps(draft: dict) -> list[dict[str, str]]:
    """Up to three concrete things to do after this draft. Every one is a prompt."""
    action = draft.get("action")
    payload = draft.get("payload") or {}
    steps: list[tuple[str, str]] = []
    if action == "template":
        steps = [
            ("Schedule a weekly message with it",
             "Schedule a weekly message at 10:00 saying: Hi {name}, here is this week's update."),
            ("Turn it into an opt-in send",
             "Send an opt-in message to the recipients who agreed."),
            ("Look for repeated wording", "Look for repeated wording in my notes."),
        ]
    elif action == "schedule":
        steps = [
            ("Prepare the same message in BulkSend",
             "Send an opt-in message to the recipients who agreed."),
            ("Check the automation status", "Show the automation status."),
        ]
    elif action == "task":
        steps = [
            ("Save the wording as a template",
             "Create a template named Follow-up that says: Hi {name}, checking in."),
            ("Test a reply rule", "Which rule would answer the message: how much does it cost?"),
        ]
    elif action == "navigate":
        steps = [("Refresh the automation status", "Show the automation status.")]
    elif action == "run":
        command = str(payload.get("command") or "")
        result = draft.get("result") or {}
        if command == "knowledge-import":
            imported = int(result.get("imported") or 0)
            steps = [("Search what you imported", "Search my knowledge base for the refund policy."),
                     ("Look for repeated wording", "Look for repeated wording in my notes.")]
            if not imported:
                steps.insert(0, ("Attach a readable text file",
                                 "Import these files into my knowledge base."))
        elif command == "knowledge-search":
            steps = [("Save the answer as a note", "Remember: the answer above is the current policy."),
                     ("Look for repeated wording", "Look for repeated wording in my notes.")]
        elif command == "knowledge-note":
            steps = [("Search your notes", "Search my knowledge base for the note I just saved."),
                     ("Look for repeated wording", "Look for repeated wording in my notes.")]
        elif command == "patterns-refresh":
            steps = [("Search the wording you kept", "Search my knowledge base for our reply wording."),
                     ("Set up automatic replies", "Set up the automatic replies for the accounts I watch.")]
        elif command == "autoreply-test":
            steps = [("Open the inbox", "Refresh the inbox."),
                     ("Set up automatic replies", "Set up the automatic replies for the accounts I watch.")]
    elif action == "ops":
        surface = str(payload.get("surface") or "")
        steps = {
            "scraper": [("Add the members who agreed", "Add the members who agreed to join.")],
            "adder": [("Refresh the inbox", "Refresh the inbox.")],
            "autojoin": [("Refresh the automation status", "Show the automation status.")],
            "bulksend": [("Save the wording as a template",
                          "Create a template named Welcome that says: Hi {name}, thanks for reaching out.")],
            "autoreply": [("Test a message against the rules",
                           "Which rule would answer the message: how much does it cost?")],
            "knowledge": [("Search your notes", "Search my knowledge base for the refund policy.")],
            "factory": [("Refresh the automation status", "Show the automation status.")],
            "automation": [("Open the inbox", "Refresh the inbox.")],
            "inbox": [("Set up automatic replies", "Set up the automatic replies for the accounts I watch.")],
        }.get(surface, [])
    if not steps:
        steps = [(example.split(" says:")[0], example) for example in _HELP_EXAMPLES[:2]]
    seen, out = set(), []
    for label, instruction in steps:
        if instruction.lower() in seen:
            continue
        seen.add(instruction.lower())
        out.append({"label": label, "instruction": instruction})
        if len(out) == 3:
            break
    return out


# ============ enhance and suggestions ============


def enhance_instruction(instruction: str) -> dict[str, Any]:
    """Rewrite a prompt into the explicit form the deterministic drafter reads best.

    The rewrite never invents a recipient, a message body or an approval id. It only
    names the fields the draft already inferred and adds the missing operational
    detail the operator can still change.
    """
    text = (instruction or "").strip()
    if not text:
        raise HTTPException(400, "Write a prompt before asking for a more detailed one.")
    draft = build_draft(text)
    action = draft.get("action")
    payload = draft.get("payload") or {}
    added: list[str] = []
    if action == "template" and (payload.get("content") or "").strip():
        name = str(payload.get("name") or "").strip()
        enhanced = f"Create a template named {name} that says: {payload['content']}"
        if name:
            added.append("named the template")
        added.append("quoted the exact message")
    elif action == "template":
        name = str(payload.get("name") or "").strip()
        enhanced = (f"Create a template named {name}." if name else "Create a template.")
        enhanced += " Add the exact message text you want stored, so nothing is guessed."
        if name:
            added.append("named the template")
        added.append("asked for the exact message text")
    elif action == "schedule" and (payload.get("message") or "").strip():
        repeat = str(payload.get("repeat") or "once")
        if repeat not in {"daily", "weekly", "workday"}:
            repeat = "daily"
            added.append("made it a daily schedule")
        when = f"{int(payload.get('hour') or 10):02d}:{int(payload.get('minute') or 0):02d}"
        enhanced = f"Schedule a {repeat} message at {when} saying: {payload['message']}"
        added.append(f"set the send time to {when}")
        recipients = payload.get("recipients") or []
        if recipients:
            enhanced += " Recipients: " + ", ".join(recipients)
            added.append("listed the consenting recipients")
        if payload.get("approvalId"):
            enhanced += f" Approval id: {payload['approvalId']}"
            added.append("carried the approval reference")
    elif action == "task" and (payload.get("message") or "").strip():
        enhanced = f"Create a task to send: {payload['message']}"
        added.append("quoted the exact message")
        targets = payload.get("targets") or []
        if targets:
            enhanced += " Recipients: " + ", ".join(targets)
            added.append("listed the consenting recipients")
    elif action == "ops":
        surface = str(payload.get("surface") or "")
        fields = payload.get("fields") or {}
        label = OPS_SURFACES.get(surface, {}).get("title") or "the surface"
        enhanced = text.rstrip(".") + ". "
        enhanced += f"Open {label} with " + ", ".join(f"{k}={v}" for k, v in fields.items()) + "."
        added.append("named the surface and its bounded fields")
        added.append("kept the consent gate for you to press")
    elif action == "run":
        enhanced = text.rstrip(".") + "."
        added.append("kept the command and made it a complete sentence")
    elif action == "navigate":
        enhanced = text.rstrip(".") + "."
        added.append("made the page switch explicit")
    else:
        enhanced = (text.rstrip(".") + ". Give the exact message text, the recipients who agreed, "
                    "and any send time or approval reference.")
        added.append("asked for the message, recipients, time and approval")
    return {"instruction": enhanced.strip(), "added": added, "action": action,
            "warnings": draft.get("warnings") or []}


def suggestions(operator: str, limit: int = 6) -> list[dict[str, str]]:
    """What to offer when the panel opens: the operator's own notes, then real usage."""
    out: list[dict[str, str]] = []
    seen: set = set()

    def add(label: str, instruction: str, source: str, detail: str = "") -> None:
        key = instruction.lower().strip()
        if not key or key in seen or len(out) >= limit:
            return
        seen.add(key)
        out.append({"label": label, "instruction": instruction, "source": source, "detail": detail})

    try:
        documents = knowledge.list_documents(operator).get("documents") or []
    except HTTPException:
        documents = []
    for doc in documents[:3]:
        title = str(doc.get("title") or "").strip()
        if title:
            add(f"Search “{title[:40]}”", f"Search my knowledge base for {title}", "knowledge",
                doc.get("preview") or "")
    for preset in list_presets(operator):
        if int(preset.get("uses") or 0) > 0:
            add(str(preset.get("title") or ""), str(preset.get("instruction") or ""), "preset",
                "You applied this before.")
    applied = next((turn for turn in _turns(operator)
                    if turn.get("outcome") == "applied" and turn.get("instruction")), None)
    if applied:
        add("Run that again", str(applied["instruction"]), "history", "Your last applied prompt.")
    defaults = [
        ("Import a knowledge file", "Import these files into my knowledge base."),
        ("Save a note", _HELP_EXAMPLES[3]),
        ("Create a welcome template", _HELP_EXAMPLES[0]),
        ("Prepare an opt-in send", "Send an opt-in message to the recipients who agreed."),
    ]
    for label, instruction in defaults:
        add(label, instruction, "starter")
    return out


# ============ the ops and internal tiers ============


def _apply_instruction(fields: dict, instruction: str, surface: str) -> dict:
    """Tune the declared fields from the operator's own words, inside the bounds."""
    out = dict(fields)
    text = instruction or ""
    for name in list(out):
        pattern = _FIELD_PATTERNS.get(name)
        if not pattern:
            continue
        bounds = SURFACE_BOUNDS.get((surface, name)) or FIELD_BOUNDS.get(name)
        if not bounds:
            continue
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        try:
            number = float(raw) if "." in raw else int(raw)
        except ValueError:
            continue
        low, high = bounds
        out[name] = max(low, min(high, number))
    return out


def _ops_draft(surface: str, fields: dict, *, title: str = "") -> dict[str, Any]:
    spec = OPS_SURFACES.get(surface)
    if not spec:
        return _help()
    return {
        "action": "ops",
        "title": title or spec["title"],
        "summary": spec["summary"],
        "payload": {
            "surface": surface,
            "tab": spec["tab"],
            "gate": spec["gate"],
            "gate_label": spec["gate_label"],
            "fields": fields,
        },
        "warnings": [spec["note"]],
        "requires_confirmation": True,
    }


def _internal_draft(command: str, params: dict) -> dict[str, Any]:
    spec = INTERNAL_COMMANDS.get(command)
    if not spec:
        return _help()
    warnings = []
    if command == "knowledge-note" and not str(params.get("text") or "").strip():
        warnings.append("Write the note text before it can be saved.")
    if command == "knowledge-search" and not str(params.get("query") or "").strip():
        warnings.append("Name what to look for.")
    if command == "autoreply-test" and not str(params.get("text") or "").strip():
        warnings.append("Type the message you want to test.")
    return {
        "action": "run",
        "title": spec["title"],
        "summary": spec["summary"],
        "payload": {"command": command, "params": params},
        "warnings": warnings,
        "requires_confirmation": False,
    }


def _internal_intent(text: str) -> str:
    for command, patterns in _INTERNAL_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                return command
    return ""


def _internal_params(command: str, text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if command == "knowledge-import":
        return {"note": body}
    if command == "knowledge-note":
        cleaned = body
        head = re.match(r"^\s*([^:：]{0,60})[:：]\s*(.+)$", body, re.S)
        if head and re.match(r"^\s*(?:please\s+)?(?:remember|note|save|write|store|add|put)\b",
                             head.group(1), re.I):
            cleaned = head.group(2).strip()
        cleaned = re.sub(r"\s+(?:to|in|into)\s+(?:my|the)\s+(?:knowledge\s*base|notes?)\s*$", "",
                         cleaned, flags=re.I).strip()
        title = re.split(r"[.\n]", cleaned)[0][:PRESET_TITLE].strip()
        return {"text": cleaned, "title": title}
    if command == "knowledge-search":
        cleaned = re.sub(r"^\s*(?:please\s+)?(?:search|find|look\s*up)\s+"
                         r"(?:my\s+|the\s+)?(?:knowledge\s*base|notes?)\s*(?:for|about)?\s*",
                         "", body, flags=re.I)
        cleaned = re.sub(r"^\s*(?:please\s+)?(?:search|find|look\s*up)\b\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^\s*for\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+(?:in|from|inside)\s+(?:my|the)\s+(?:knowledge\s*base|notes?)\s*$", "",
                         cleaned, flags=re.I)
        return {"query": cleaned.strip()}
    if command == "autoreply-test":
        quoted = _QUOTED_RE.search(body)
        if quoted:
            message = quoted.group(1).strip()
        else:
            message = re.sub(r"^.*?\b(?:with|using|say|says|message|answer)\b\s*", "", body,
                             flags=re.I).strip() or body
        return {"text": message, "name": ""}
    return {}


def _ops_surface(text: str) -> str:
    for surface, patterns in _OPS_KEYWORDS:
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                return surface
    return ""


def _run_internal(operator: str, draft: dict) -> dict:
    """Run one internal command against the operator's own workspace."""
    payload = draft.get("payload") or {}
    command = str(payload.get("command") or "")
    params = payload.get("params") or {}
    if command == "knowledge-import":
        files = _attachments_by_ids(operator, params.get("attachment_ids") or [])
        readable = [record for record in files if record.get("readable") and record.get("text")]
        if not files:
            raise HTTPException(400, "Attach a text file before importing.")
        if not readable:
            raise HTTPException(400, "None of the attached files could be read as text.")
        summary = knowledge.import_files(operator, [{"name": record["name"], "text": record["text"]}
                                                    for record in readable])
        summary["attachments"] = [_public_attachment(record) for record in files]
        return summary
    if command == "knowledge-note":
        body = str(params.get("text") or "").strip()
        if not body:
            raise HTTPException(400, "Write something before saving to the knowledge base.")
        document = knowledge.add_document(operator, title=str(params.get("title") or "")[:PRESET_TITLE],
                                          text=body, source="copilot", tags=["copilot"], dedupe=True)
        return {"stored": document is not None, "document": document}
    if command == "knowledge-search":
        query = str(params.get("query") or "")
        matches = knowledge.search(operator, query, k=5)
        return {"query": query, "count": len(matches), "matches": matches}
    if command == "patterns-refresh":
        import patterns
        record = patterns.refresh(operator, force=True)
        summary = patterns.summary(record, len(patterns.build_corpus(operator)))
        return {"suggestions": summary.get("suggestions", []), "corpus": summary.get("corpus", 0)}
    if command == "autoreply-test":
        import auto_reply
        return auto_reply.preview_reply(operator, str(params.get("text") or ""),
                                        name=str(params.get("name") or ""))
    raise HTTPException(400, "That is not an internal command the Copilot can run.")


def _with_files_only(draft: dict, files: list) -> dict:
    """Show the tray on a draft that is not an import, without claiming it was read."""
    if not files:
        return draft
    payload = draft.setdefault("payload", {})
    payload["attachments"] = [_public_attachment(record) for record in files]
    unreadable = [record["name"] for record in files if not record.get("readable")]
    if unreadable:
        draft.setdefault("warnings", []).append(
            "Only text documents are read: " + ", ".join(unreadable[:4]) + ".")
    return draft


def _with_attachments(draft: dict, files: list) -> dict:
    """Record the tray on the draft and hand the ids to the import command."""
    _with_files_only(draft, files)
    payload = draft.setdefault("payload", {})
    if payload.get("command") == "knowledge-import":
        params = payload.setdefault("params", {})
        params["attachment_ids"] = [record["id"] for record in files]
        if not files:
            draft.setdefault("warnings", []).append("Attach at least one text file before importing.")
    return draft


def _import_draft(files: list) -> dict[str, Any]:
    readable = [record for record in files if record.get("readable")]
    if not readable:
        draft = _help()
        draft["title"] = "Attach a text document to import"
        draft["summary"] = ("Images, video, PDFs, Word files and spreadsheets cannot be read here. "
                            "Attach markdown, text, csv, json, yaml, html or a zip of them.")
        draft["warnings"] = [f"{record['name']}: {record.get('reason') or 'not readable'}"
                             for record in files[:4]]
        return draft
    return _with_attachments(_internal_draft("knowledge-import", {}), files)


def build_draft(instruction: str, attachments: list | None = None) -> dict[str, Any]:
    """Parse an instruction into a draft. Pure: no I/O and no side effects."""
    files = list(attachments or [])
    text = (instruction or "").strip()
    if files and (not text or _import_intent(text)):
        return _import_draft(files)
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
        command = _internal_intent(text)
        if command:
            draft = _internal_draft(command, _internal_params(command, text))
            return _with_attachments(draft, files) if command == "knowledge-import" else _with_files_only(draft, files)
        surface = _ops_surface(text)
        if surface:
            spec = OPS_SURFACES[surface]
            fields = _apply_instruction(dict(spec["fields"]), text, surface)
            return _with_files_only(_ops_draft(surface, fields, title=spec["title"]), files)
        page = _navigate_intent(text)
        if page:
            return _navigate_draft(*page)
        return _with_files_only(_help(), files)

    reason = _blocked_reason(_command_region(text))
    if reason:
        return _unsupported(reason)

    if is_schedule:
        draft = _draft_schedule(text)
    elif is_template:
        draft = _draft_template(text)
    else:
        draft = _draft_task(text)
    return _with_files_only(draft, files)


def _import_intent(text: str) -> bool:
    return bool(re.search(r"\b(?:import|ingest|upload)\b", text, re.I)
                or re.search(r"\b(?:add|save|store|load)\b\s+(?:these|those|the|my|this)\s+"
                             r"(?:files?|documents?|attachments?)\b", text, re.I))


def _preset_draft(preset: dict, instruction: str = "") -> dict[str, Any]:
    """Build the draft a curated preset describes."""
    kind = str(preset.get("kind") or "draft")
    if kind == "command":
        command = str(preset.get("command") or "")
        if command not in INTERNAL_COMMANDS:
            return _help()
        source = str(instruction or preset.get("instruction") or "")
        return _internal_draft(command, _internal_params(command, source))
    if kind == "ops":
        surface = str(preset.get("surface") or "")
        spec = OPS_SURFACES.get(surface)
        if not spec:
            return _help()
        source = str(instruction or preset.get("instruction") or "")
        fields = _apply_instruction(dict(spec["fields"]), source, surface)
        return _ops_draft(surface, fields, title=str(preset.get("title") or spec["title"]))
    return build_draft(str(instruction or preset.get("instruction") or ""))


def _safe_run(operator: str, draft: dict) -> dict:
    """An internal command that cannot run reports why instead of failing the request."""
    try:
        return _run_internal(operator, draft)
    except HTTPException as exc:
        return {"error": str(exc.detail)}


@router.post("/api/copilot/draft")
async def draft_command(body: CopilotRequest, http_request: Request):
    operator = _operator(http_request)
    files = _attachments_by_ids(operator, body.attachment_ids)
    draft = build_draft(body.instruction, files)
    turn = _remember(operator, body.instruction, draft, body.preset_id)
    if draft.get("action") == "run":
        draft["result"] = _safe_run(operator, draft)
    draft["mode"] = "work" if str(body.mode or "").lower() == "work" else "ask"
    draft["next"] = _next_steps(draft)
    return {"success": True, "draft": draft, "presets": list_presets(operator),
            "attachments": [_public_attachment(record) for record in _operator_attachments(operator).values()],
            "turn_id": turn["id"] if turn else ""}


@router.post("/api/copilot/enhance")
async def enhance_command(body: EnhanceBody, http_request: Request):
    """Rewrite a prompt into the explicit form the drafter reads best. No model call."""
    _operator(http_request)
    return {"success": True, **enhance_instruction(body.instruction)}


@router.get("/api/copilot/suggest")
async def suggest_command(http_request: Request, limit: int = 6):
    operator = _operator(http_request)
    return {"success": True, "suggestions": suggestions(operator, max(1, min(int(limit), 8)))}


@router.get("/api/copilot/attachments")
async def list_attachments(http_request: Request):
    operator = _operator(http_request)
    return {"success": True,
            "attachments": [_public_attachment(record)
                            for record in _operator_attachments(operator).values()]}


@router.post("/api/copilot/attachments")
async def add_attachments(http_request: Request, files: list[UploadFile] = File(...)):
    operator = _operator(http_request)
    if not files:
        raise HTTPException(400, "Choose at least one file to attach.")
    if len(files) > MAX_ATTACHMENTS:
        raise HTTPException(400, f"Attach at most {MAX_ATTACHMENTS} files to one prompt.")
    uploads = []
    for upload in files:
        data = await upload.read(ATTACHMENT_FILE_BYTES + 1)
        if len(data) > ATTACHMENT_FILE_BYTES:
            raise HTTPException(400, f"{knowledge.entry_name(upload.filename or 'attachment')}: "
                                     f"keep each attachment under {ATTACHMENT_FILE_BYTES // (1024 * 1024)} MB.")
        uploads.append((upload.filename or "attachment", upload.content_type or "", data))
    added = _store_attachments(operator, uploads)
    return {"success": True, "added": added,
            "attachments": [_public_attachment(record)
                            for record in _operator_attachments(operator).values()]}


@router.delete("/api/copilot/attachments/{attachment_id}")
async def remove_attachment(attachment_id: str, http_request: Request):
    if not _drop_attachments(_operator(http_request), [attachment_id]):
        raise HTTPException(404, "No attachment with that id.")
    return {"success": True}


@router.delete("/api/copilot/attachments")
async def clear_attachments(http_request: Request):
    return {"success": True, "removed": _drop_attachments(_operator(http_request))}


@router.post("/api/copilot/preset-draft")
async def preset_draft_command(body: PresetDraftBody, http_request: Request):
    """Run a curated preset. The draft is built from the preset, then the operator's own
    words may tune the numbers inside the bounds."""
    operator = _operator(http_request)
    preset = _preset_by_id(operator, body.preset_id)
    if not preset:
        raise HTTPException(404, "No preset with that id.")
    draft = _preset_draft(preset, body.instruction)
    turn = _remember(operator, body.instruction or str(preset.get("instruction") or ""),
                     draft, preset["id"])
    if draft.get("action") == "run":
        draft["result"] = _safe_run(operator, draft)
    draft["next"] = _next_steps(draft)
    return {"success": True, "draft": draft, "presets": list_presets(operator),
            "turn_id": turn["id"] if turn else ""}


@router.get("/api/copilot/history")
async def history(http_request: Request):
    return {"success": True, "turns": _turns(_operator(http_request))}


class OutcomeBody(BaseModel):
    outcome: str = Field(min_length=1, max_length=16)


@router.post("/api/copilot/history/{turn_id}/outcome")
async def mark_outcome(turn_id: str, body: OutcomeBody, http_request: Request):
    """The panel says what it did with the draft; applying one offers it to the knowledge base."""
    operator = _operator(http_request)
    if body.outcome not in OUTCOMES:
        raise HTTPException(400, "Choose applied, discarded or drafted.")
    turns = _turns(operator)
    turn = next((item for item in turns if item.get("id") == turn_id), None)
    if not turn:
        raise HTTPException(404, "No Copilot turn with that id.")
    turn["outcome"] = body.outcome
    _write_turns(operator, turns)
    if body.outcome == "applied":
        _collect(operator, turn)
        _bump_usage(operator, str(turn.get("preset_id") or ""))
    return {"success": True, "collected": body.outcome == "applied"}


@router.delete("/api/copilot/history")
async def clear_history(http_request: Request):
    operator = _operator(http_request)
    _write_turns(operator, [])
    return {"success": True, "cleared": True}


@router.get("/api/copilot/presets")
async def presets(http_request: Request):
    return {"success": True, "presets": list_presets(_operator(http_request))}


@router.post("/api/copilot/presets")
async def create_preset(body: PresetBody, http_request: Request):
    operator = _operator(http_request)
    preset = add_preset(operator, body.model_dump())
    return {"success": True, "preset": preset, "presets": list_presets(operator)}


@router.put("/api/copilot/presets/{preset_id}")
async def edit_preset(preset_id: str, body: PresetBody, http_request: Request):
    operator = _operator(http_request)
    preset = save_preset(operator, preset_id, body.model_dump())
    return {"success": True, "preset": preset, "presets": list_presets(operator)}


@router.delete("/api/copilot/presets/{preset_id}")
async def delete_preset(preset_id: str, http_request: Request):
    operator = _operator(http_request)
    if not remove_preset(operator, preset_id):
        raise HTTPException(404, "No preset with that id.")
    return {"success": True, "presets": list_presets(operator)}


@router.post("/api/copilot/presets/reset")
async def reset_presets_command(http_request: Request):
    """Put the runbook back the way it shipped. Usage counters stay: they describe what
    the operator actually did, not how the catalogue is configured."""
    operator = _operator(http_request)
    reset_presets(operator)
    return {"success": True, "presets": list_presets(operator)}


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(HISTORY_STORE, {})
    return data if isinstance(data, dict) else {}


def _turns(operator: str) -> list:
    entry = _all().get(operator)
    turns = entry.get("turns") if isinstance(entry, dict) else None
    return [t for t in (turns or []) if isinstance(t, dict) and t.get("id")]


def _write_turns(operator: str, turns: list) -> None:
    records = _all()
    records[operator] = {"turns": turns[:MAX_TURNS]}
    write_secret(HISTORY_STORE, records)


def _remember(operator: str, instruction: str, draft: dict, preset_id: str = "") -> dict | None:
    """Keep the exchange so the panel can show the thread. A failed save never blocks a draft."""
    text = (instruction or "").strip()
    if not text:
        return None
    turn = {
        "id": uuid4().hex[:12],
        "time": _now(),
        "instruction": text[:INSTRUCTION_PREVIEW],
        "action": draft.get("action"),
        "title": draft.get("title"),
        "summary": draft.get("summary"),
        "outcome": "drafted",
    }
    if preset_id:
        turn["preset_id"] = preset_id
    payload = draft.get("payload") or {}
    for key in ("content", "message", "name"):
        if isinstance(payload.get(key), str) and payload[key]:
            turn["text"] = payload[key][:INSTRUCTION_PREVIEW]
            break
    try:
        _write_turns(operator, [turn] + _turns(operator))
    except Exception:
        return None
    return turn


def _collect(operator: str, turn: dict) -> None:
    if not knowledge.settings_for(operator).get("auto_collect_copilot"):
        return
    body = turn.get("text") or turn.get("instruction") or ""
    if not body:
        return
    try:
        knowledge.add_document(operator, title=turn.get("title") or "Copilot draft", text=body,
                               source="copilot", tags=["copilot", str(turn.get("action") or "")], dedupe=True)
    except HTTPException:
        pass


# ============ the per-operator preset catalogue ============


def _preset_all() -> dict:
    if not secret_key_available():
        return {}
    data = read_secret(PRESET_STORE, {})
    return data if isinstance(data, dict) else {}


def _preset_record(operator: str) -> dict:
    entry = _preset_all().get(operator)
    entry = entry if isinstance(entry, dict) else {}
    overrides = {k: v for k, v in (entry.get("overrides") or {}).items() if isinstance(v, dict)}
    return {
        "overrides": overrides,
        "removed": [str(x) for x in (entry.get("removed") or [])],
        "custom": [c for c in (entry.get("custom") or []) if isinstance(c, dict) and c.get("id")],
    }


def _preset_save(operator: str, record: dict) -> None:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    records = _preset_all()
    records[operator] = {
        "overrides": record["overrides"],
        "removed": record["removed"][:MAX_CUSTOM_PRESETS],
        "custom": record["custom"][:MAX_CUSTOM_PRESETS],
    }
    write_secret(PRESET_STORE, records)


def _usage_all() -> dict:
    if not secret_key_available():
        return {}
    data = read_secret(USAGE_STORE, {})
    return data if isinstance(data, dict) else {}


def _usage(operator: str) -> dict:
    entry = _usage_all().get(operator)
    if not isinstance(entry, dict):
        return {}
    return {str(k): int(v) for k, v in entry.items()}


def _bump_usage(operator: str, preset_id: str) -> None:
    """A preset the operator actually applied earns its place at the top of the runbook."""
    if not preset_id or not secret_key_available():
        return
    records = _usage_all()
    usage = records.get(operator) if isinstance(records.get(operator), dict) else {}
    usage[preset_id] = int(usage.get(preset_id, 0)) + 1
    records[operator] = usage
    write_secret(USAGE_STORE, records)


def list_presets(operator: str) -> list[dict[str, Any]]:
    """The operator's runbook: the shipped catalogue plus their own, ordered by real use."""
    record = _preset_record(operator)
    usage = _usage(operator)
    removed = set(record["removed"])
    out: list[dict[str, Any]] = []
    for index, preset in enumerate(PRESETS):
        if preset["id"] in removed:
            continue
        override = record["overrides"].get(preset["id"]) or {}
        out.append({**preset, **override, "built_in": True, "modified": bool(override),
                    "uses": int(usage.get(preset["id"], 0)), "order": index})
    for index, item in enumerate(record["custom"]):
        out.append({**item, "built_in": False, "modified": True,
                    "uses": int(usage.get(item["id"], 0)), "order": 1000 + index})
    out.sort(key=lambda item: (-int(item.get("uses") or 0), int(item.get("order") or 0)))
    return out


def _preset_by_id(operator: str, preset_id: str) -> dict[str, Any] | None:
    return next((item for item in list_presets(operator) if item["id"] == preset_id), None)


def _clean_fields(data: dict) -> dict[str, str]:
    limits = {"group": PRESET_GROUP, "title": PRESET_TITLE,
              "description": PRESET_DESCRIPTION, "instruction": PRESET_INSTRUCTION}
    out: dict[str, str] = {}
    for key in PRESET_FIELDS:
        value = str((data or {}).get(key) or "").strip()
        if value:
            out[key] = value[:limits[key]]
    return out


def save_preset(operator: str, preset_id: str, data: dict) -> dict[str, Any]:
    """Change a built-in preset's wording, or edit one of the operator's own."""
    record = _preset_record(operator)
    changes = _clean_fields(data)
    for preset in PRESETS:
        if preset["id"] == preset_id:
            if changes:
                merged = {**(record["overrides"].get(preset_id) or {}), **changes}
                record["overrides"][preset_id] = merged
            _preset_save(operator, record)
            return {**preset, **record["overrides"].get(preset_id, {}), "built_in": True,
                    "modified": bool(record["overrides"].get(preset_id))}
    for preset in record["custom"]:
        if preset["id"] == preset_id:
            preset.update(changes)
            _preset_save(operator, record)
            return preset
    raise HTTPException(404, "No preset with that id.")


def add_preset(operator: str, data: dict) -> dict[str, Any]:
    """Add one of the operator's own presets to the runbook."""
    record = _preset_record(operator)
    if len(record["custom"]) >= MAX_CUSTOM_PRESETS:
        raise HTTPException(409, f"You already have {MAX_CUSTOM_PRESETS} custom presets. "
                                 "Delete one before adding another.")
    fields = _clean_fields(data)
    if not fields.get("title") or not fields.get("instruction"):
        raise HTTPException(400, "A preset needs a title and an instruction.")
    kind = str((data or {}).get("kind") or "draft")
    command = str((data or {}).get("command") or "")
    if kind == "command" and command not in INTERNAL_COMMANDS:
        raise HTTPException(400, "Choose an internal command the Copilot can run.")
    if kind != "command":
        kind, command = "draft", ""
    preset = {
        "id": f"custom-{uuid4().hex[:8]}",
        "group": fields.get("group") or "Custom",
        "title": fields["title"],
        "description": fields.get("description", ""),
        "instruction": fields["instruction"],
        "kind": kind,
        "command": command,
    }
    record["custom"].append(preset)
    _preset_save(operator, record)
    return preset


def remove_preset(operator: str, preset_id: str) -> bool:
    """Take a preset out of the runbook. A shipped one is hidden, not deleted."""
    record = _preset_record(operator)
    if any(preset["id"] == preset_id for preset in record["custom"]):
        record["custom"] = [p for p in record["custom"] if p["id"] != preset_id]
        _preset_save(operator, record)
        return True
    if any(preset["id"] == preset_id for preset in PRESETS):
        if preset_id not in record["removed"]:
            record["removed"].append(preset_id)
        _preset_save(operator, record)
        return True
    return False


def reset_presets(operator: str) -> None:
    record = _preset_record(operator)
    record["overrides"] = {}
    record["removed"] = []
    record["custom"] = []
    _preset_save(operator, record)


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
