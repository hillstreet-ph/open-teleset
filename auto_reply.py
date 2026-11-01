"""Auto Reply: answer the people who message an account first.

The rule that cannot be switched off is the one that keeps this inside Telegram's terms:
a reply only ever goes to a private conversation whose newest message came from the other
side. No group, no channel, no bot, never the first message in a thread, never someone on
the opt-out list, and nothing at all until the operator switches the feature on and names
the accounts it may speak for.

Two modes, deliberately unequal. In rules mode an operator-written reply is sent
automatically - the trigger is theirs and so are the words. In AI mode a model's candidate
answer is never sent by the dashboard: it lands in an approval outbox where a human reads
it, edits it if they like, and presses Send themselves.

Everything that goes out respects the cooldown store the outreach workers already use, so
an auto-reply can never outrun an account Telegram has told us to leave alone.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import knowledge
from log_manager import log_manager
from open_teleset.operator_store import feature_enabled, read_secret, secret_key_available, write_secret
from outreach_policy import (
    OutreachDenied,
    account_cooldown_remaining,
    hash_identifier,
    outreach_policy,
    record_provider_restriction,
)

router = APIRouter(tags=["auto-reply"])
log = logging.getLogger(__name__)

STORE = "auto_reply"
MODES = ("rules", "ai_approval")
RULES_MODE = "rules"
AI_MODE = "ai_approval"

MAX_RULES = 12
MAX_KEYWORDS = 8
KEYWORD_MAX = 40
REPLY_MAX = 700
SNIPPET_MAX = 300
MAX_DIALOGS = 40
MIN_POLL_SECONDS = 30
DEFAULT_POLL_SECONDS = 90
MAX_POLL_SECONDS = 900
DEFAULT_DAILY_CAP = 20
MAX_DAILY_CAP = 200
MAX_ACCOUNTS = 12
MAX_CONVERSATIONS = 200
MAX_OUTBOX = 20
MAX_ACTIVITY = 60
NAME_MAX = 60
PEER_REF_LENGTH = 16
CHAT_COOLDOWN_SECONDS = 600
SEND_SPACING_SECONDS = 20
STALE_MESSAGE_SECONDS = 1800
OUR_IDS_PER_CHAT = 10

OFF_MESSAGE = "Auto Reply is switched off. Turn it on under Integrations > Platform switches."
NO_ACCOUNTS = "Turn Auto Reply on for at least one account first."
NO_RULE_TEXT = "Write the reply this rule should send."
TOO_MANY_RULES = f"Auto Reply keeps up to {MAX_RULES} rules."
NO_PROVIDER = "Configure an AI provider and API key in Settings → AI providers."
GONE_OUTBOX = "That reply draft is no longer in the outbox."
TAKEOVER_NOTE = "You replied here, so Auto Reply stopped in this conversation."

SYSTEM_PROMPT = (
    "You write short replies for a Telegram support inbox on behalf of the account owner. "
    "Answer only from the notes you are given. Never invent prices, dates, links, order "
    "numbers or promises. Never ask for a password, a login code or a session string. "
    "If the notes do not cover the question, reply that a person will follow up. "
    "Use plain text, at most two sentences, no emoji, no markdown."
)


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _enabled() -> None:
    if not feature_enabled("auto_reply"):
        raise HTTPException(403, OFF_MESSAGE)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clamp(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


# ============ storage ============


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _blank() -> dict:
    return {
        "enabled": False,
        "mode": RULES_MODE,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "daily_cap": DEFAULT_DAILY_CAP,
        "accounts": {},
        "rules": [],
        "conversations": {},
        "outbox": [],
        "activity": [],
        "sent": {},
        "last_send_at": 0,
        "last_pass": 0,
    }


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    record = _blank()
    if not isinstance(entry, dict):
        return record
    record["enabled"] = bool(entry.get("enabled"))
    record["mode"] = entry.get("mode") if entry.get("mode") in MODES else RULES_MODE
    record["poll_seconds"] = _clamp(entry.get("poll_seconds"), MIN_POLL_SECONDS, MAX_POLL_SECONDS,
                                    DEFAULT_POLL_SECONDS)
    record["daily_cap"] = _clamp(entry.get("daily_cap"), 0, MAX_DAILY_CAP, DEFAULT_DAILY_CAP)
    accounts = entry.get("accounts")
    if isinstance(accounts, dict):
        record["accounts"] = {str(key): {"enabled": bool((value or {}).get("enabled"))}
                             for key, value in accounts.items() if isinstance(value, dict)}
    record["rules"] = [rule for rule in (entry.get("rules") or []) if isinstance(rule, dict) and rule.get("id")]
    conversations = entry.get("conversations")
    if isinstance(conversations, dict):
        record["conversations"] = {str(key): value for key, value in conversations.items() if isinstance(value, dict)}
    record["outbox"] = [draft for draft in (entry.get("outbox") or []) if isinstance(draft, dict) and draft.get("id")]
    record["activity"] = [item for item in (entry.get("activity") or []) if isinstance(item, dict)]
    sent = entry.get("sent")
    if isinstance(sent, dict):
        record["sent"] = {str(key): int(value) for key, value in sent.items() if isinstance(value, int)}
    record["last_send_at"] = _as_float(entry.get("last_send_at"))
    record["last_pass"] = _as_float(entry.get("last_pass"))
    return record


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _save(operator: str, record: dict) -> None:
    record["conversations"] = _trim_by(record["conversations"], "seen", MAX_CONVERSATIONS)
    all_records = _all()
    all_records[operator] = {key: record[key] for key in _blank()}
    write_secret(STORE, all_records)


def _trim_by(mapping: dict, field: str, limit: int) -> dict:
    if len(mapping) <= limit:
        return mapping
    ordered = sorted(mapping.items(), key=lambda item: float(item[1].get(field) or 0), reverse=True)
    return dict(ordered[:limit])


# ============ rules ============


def _clean_rule(body: dict) -> dict:
    keywords = [str(word).strip().lower()[:KEYWORD_MAX] for word in (body.get("keywords") or []) if str(word).strip()]
    match = body.get("match") if body.get("match") in ("keyword", "any") else "keyword"
    reply = _validate_template(body.get("reply"))
    if match == "keyword" and not keywords:
        raise HTTPException(400, "Give the rule at least one keyword, or choose Match anything.")
    return {
        "id": str(body.get("id") or uuid4().hex[:12]),
        "name": str(body.get("name") or "").strip()[:NAME_MAX] or (keywords[0] if keywords else "Auto reply"),
        "match": match,
        "keywords": keywords[:MAX_KEYWORDS],
        "reply": reply,
        "enabled": bool(body.get("enabled", True)),
    }


VARIABLES = ("{name}", "{first}")


def _validate_template(text) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise HTTPException(400, NO_RULE_TEXT)
    if len(cleaned) > REPLY_MAX:
        raise HTTPException(400, f"An auto reply is limited to {REPLY_MAX} characters.")
    for token in re.findall(r"\{[a-z_]+\}", cleaned):
        if token not in VARIABLES:
            raise HTTPException(400, f"{token} is not a placeholder Auto Reply knows. Use {' or '.join(VARIABLES)}.")
    return cleaned


def render(text: str, *, name: str = "") -> str:
    first = ((name or "").strip().split(" ") or [""])[0]
    out = (text or "").replace("{first}", first or "there")
    return out.replace("{name}", (name or "").strip() or "there").strip()


def match_rule(rules: list, text: str) -> dict | None:
    lowered = (text or "").lower()
    for rule in rules or []:
        if not rule.get("enabled"):
            continue
        if rule.get("match") == "any":
            return rule
        if any(word and word in lowered for word in rule.get("keywords") or []):
            return rule
    return None


# ============ the listener ============


def _peer_ref(operator: str, account_id: str, peer) -> str:
    return hash_identifier(f"{operator}|{account_id}|{peer}")[:PEER_REF_LENGTH]


def _private(dialog) -> bool:
    """Only a 1:1 chat with a person: never a group, channel, bot or deleted account."""
    entity = getattr(dialog, "entity", None)
    if entity is None:
        return False
    marker = getattr(dialog, "is_private", None)
    if marker is False:
        return False
    if marker is not True and type(entity).__name__ not in ("User", "PeerUser"):
        return False
    return not getattr(entity, "bot", False) and not getattr(entity, "deleted", False)


def _display_name(dialog, entity) -> str:
    name = str(getattr(dialog, "name", "") or "").strip()
    if not name:
        name = " ".join(part for part in (getattr(entity, "first_name", ""), getattr(entity, "last_name", ""))
                        if part)
    return name[:NAME_MAX] or "a contact"


def _suppress_key(entity) -> str:
    username = str(getattr(entity, "username", "") or "").strip()
    return username or str(getattr(entity, "id", "") or "")


def _message_time(value) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return value.timestamp()
    return time.time()


def _client_getter():
    from account_manager import account_manager

    async def get_client(account_id: str):
        return await account_manager.get_client(account_id)

    return get_client


async def poll_operator(operator: str, *, get_client=None, now: float = None) -> dict:
    """One pass over every account this operator switched on. Returns what it did."""
    record = _record(operator)
    clock = time.time() if now is None else float(now)
    counts = {"checked": 0, "replied": 0, "drafted": 0, "paused": 0, "skipped": 0, "watching": 0, "reason": ""}
    record["last_pass"] = clock
    if not record["enabled"]:
        counts["reason"] = "off"
        _save(operator, record)
        return counts
    active = sorted(a for a, cfg in record["accounts"].items() if cfg.get("enabled"))[:MAX_ACCOUNTS]
    if not active:
        counts["reason"] = "no_accounts"
        _save(operator, record)
        return counts
    if not record["daily_cap"] or _sent_today(record) >= record["daily_cap"]:
        counts["reason"] = "daily_cap"
        _save(operator, record)
        return counts
    getter = get_client or _client_getter()
    for account_id in active:
        client = await _account_client(getter, account_id)
        if client is None:
            counts["skipped"] += 1
            continue
        counts["checked"] += 1
        dialogs = await _dialogs(client)
        for dialog in dialogs:
            outcome = await _look_at_dialog(operator, record, account_id, dialog, client, clock, counts)
            if outcome == "sent":
                record["last_send_at"] = clock
            if _sent_today(record) >= record["daily_cap"]:
                break
    _save(operator, record)
    return counts


async def _account_client(getter, account_id: str):
    try:
        if account_cooldown_remaining(account_id) > 0:
            return None
        client = await asyncio.wait_for(getter(account_id), timeout=30)
    except Exception:
        log.warning("Auto reply could not reach one account")
        return None
    return client if client else None


async def _dialogs(client) -> list:
    try:
        return list(await asyncio.wait_for(client.get_dialogs(limit=MAX_DIALOGS), timeout=30) or [])
    except Exception:
        log.warning("Auto reply could not list conversations")
        return []


async def _look_at_dialog(operator, record, account_id, dialog, client, clock, counts) -> str:
    message = getattr(dialog, "message", None)
    entity = getattr(dialog, "entity", None)
    if message is None or entity is None:
        return "none"
    peer = getattr(entity, "id", None)
    if peer is None:
        return "none"
    if not _private(dialog):
        return "none"
    ref = _peer_ref(operator, account_id, peer)
    message_id = int(getattr(message, "id", 0) or 0)
    conv = record["conversations"].get(ref)
    if not isinstance(conv, dict):
        # First sight never answers: those messages arrived before the switch, and
        # replying to them would be us opening a conversation unprompted.
        record["conversations"][ref] = {
            "account": account_id, "peer": str(peer), "name": _display_name(dialog, entity),
            "suppress_key": _suppress_key(entity), "cursor": message_id, "seen": clock,
            "status": "watching", "taken_over": False, "muted": False, "our_ids": [], "last_reply_at": 0,
        }
        counts["watching"] += 1
        return "watching"
    conv["seen"] = clock
    conv["name"] = _display_name(dialog, entity) or conv.get("name", "")
    if message_id <= int(conv.get("cursor") or 0):
        return "none"

    if getattr(message, "out", False):
        conv["cursor"] = message_id
        if message_id not in (conv.get("our_ids") or []):
            conv["taken_over"] = True
            conv["status"] = "paused"
            _note(record, account_id, ref, TAKEOVER_NOTE)
            counts["paused"] += 1
            return "paused"
        return "none"

    text = str(getattr(message, "text", "") or getattr(message, "raw_text", "") or "")
    conv["last_inbound"] = text[:SNIPPET_MAX]
    conv["last_inbound_at"] = _now_iso()
    if conv.get("taken_over") or conv.get("muted"):
        conv["cursor"] = message_id
        conv["status"] = "muted" if conv.get("muted") else "paused"
        counts["skipped"] += 1
        return "skipped"
    if clock - _message_time(getattr(message, "date", None)) > STALE_MESSAGE_SECONDS:
        conv["cursor"] = message_id
        _note(record, account_id, ref, "That message was too old for an automatic reply")
        counts["skipped"] += 1
        return "skipped"
    if clock - float(conv.get("last_reply_at") or 0) < CHAT_COOLDOWN_SECONDS:
        # Not consumed: the conversation cools down, then the next pass answers it.
        counts["skipped"] += 1
        return "cooldown"
    if clock - float(record.get("last_send_at") or 0) < SEND_SPACING_SECONDS:
        counts["skipped"] += 1
        return "spacing"
    try:
        if outreach_policy.is_suppressed(_suppress_key(entity)):
            conv["cursor"] = message_id
            conv["status"] = "opted_out"
            _note(record, account_id, ref, "Not sent: that person is on the opt-out list.")
            counts["skipped"] += 1
            return "suppressed"
    except OutreachDenied:
        conv["cursor"] = message_id
        _note(record, account_id, ref, "Not sent: the opt-out list could not be read.")
        counts["skipped"] += 1
        return "suppressed"

    if record["mode"] == AI_MODE:
        draft = await _compose(operator, text, conv.get("name") or "")
        conv["cursor"] = message_id
        if draft:
            _queue_draft(record, account_id, ref, conv, text, draft)
            _note(record, account_id, ref, "An AI draft is waiting for your approval")
            counts["drafted"] += 1
            return "drafted"
        _note(record, account_id, ref, "No AI draft: check the provider in Settings")
        counts["skipped"] += 1
        return "no_draft"

    rule = match_rule(record["rules"], text)
    if not rule:
        conv["cursor"] = message_id
        _note(record, account_id, ref, "No rule matched, so nothing was sent")
        counts["skipped"] += 1
        return "no_rule"
    reply = render(rule["reply"], name=conv.get("name") or "")
    sent_id = await _send(client, account_id, entity, reply)
    conv["cursor"] = message_id
    if sent_id is None:
        conv["status"] = "waiting"
        _note(record, account_id, ref, "Telegram did not accept the reply")
        counts["skipped"] += 1
        return "send_failed"
    conv["our_ids"] = (conv.get("our_ids") or [])[-OUR_IDS_PER_CHAT:] + [sent_id]
    conv["last_reply_at"] = clock
    conv["status"] = "replied"
    _count_sent(record)
    _note(record, account_id, ref, f"Replied with the rule “{rule['name']}”")
    _collect_reply(operator, rule, reply)
    counts["replied"] += 1
    return "sent"


async def _send(client, account_id: str, entity, text: str):
    from telethon import errors

    try:
        sent = await asyncio.wait_for(client.send_message(entity, text), timeout=60)
    except (errors.FloodWaitError, errors.PeerFloodError) as exc:
        try:
            record_provider_restriction(account_id, exc)
        except OutreachDenied:
            log.warning("Auto reply could not record the Telegram wait")
        return None
    except Exception:
        # Telegram's own text can carry identifiers, so it is never echoed or logged.
        log.warning("Auto reply send failed for one account")
        return None
    return int(getattr(sent, "id", 0) or 0) or None


async def _compose(operator: str, inbound: str, name: str) -> str:
    from dashboard_features import PROVIDERS, _ai_chat, _ai_config, _ai_keys

    config = _ai_config(operator)
    provider = str(config.get("provider") or "")
    base_url = str(config.get("base_url") or PROVIDERS.get(provider, {}).get("base_url") or "").strip()
    model = str(config.get("model") or "").strip()
    key = (_ai_keys(operator).get(provider) or "").strip()
    if not (base_url and model and key):
        return ""
    notes = knowledge.context_for(operator, inbound)
    prompt = (f"Notes:\n{notes or '(none saved)'}\n\nTheir message: {inbound[:SNIPPET_MAX]}\n"
              f"Write the reply. Greet them as {name or 'there'}.")
    try:
        answer = await _ai_chat(base_url, key, model,
                                [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": prompt}], max_tokens=200)
    except HTTPException:
        return ""
    return re.sub(r"\s+", " ", answer).strip()[:REPLY_MAX]


def _queue_draft(record, account_id, ref, conv, inbound, draft) -> None:
    record["outbox"] = ([{
        "id": uuid4().hex[:12], "account": account_id, "peer_ref": ref, "name": conv.get("name") or "",
        "suppress_key": conv.get("suppress_key") or "", "inbound": inbound[:SNIPPET_MAX], "reply": draft,
        "created": _now_iso(), "status": "pending",
    }] + record["outbox"])[:MAX_OUTBOX]


def _note(record, account_id, ref, detail: str) -> None:
    record["activity"] = ([{"time": _now_iso(), "account": account_id, "peer_ref": ref, "detail": detail[:120]}]
                          + record["activity"])[:MAX_ACTIVITY]


def _collect_reply(operator: str, rule: dict, reply: str) -> None:
    if not knowledge.settings_for(operator).get("auto_collect_replies"):
        return
    try:
        knowledge.add_document(operator, title=f"Reply: {rule.get('name') or 'auto'}", text=reply,
                               source="auto_reply", tags=["auto-reply"], dedupe=True)
    except HTTPException:
        log.warning("Auto reply could not collect the answer into the knowledge base")


def _sent_today(record) -> int:
    return int(record["sent"].get(_today()) or 0)


def _count_sent(record) -> None:
    day = _today()
    record["sent"] = {key: value for key, value in record["sent"].items() if key == day}
    record["sent"][day] = _sent_today(record) + 1


# ============ panel view ============


def public_config(operator: str, *, worker_state: dict = None) -> dict:
    record = _record(operator)
    conversations = [{
        "peer_ref": ref, "account": conv.get("account"), "name": conv.get("name") or "",
        "status": conv.get("status") or "watching", "taken_over": bool(conv.get("taken_over")),
        "muted": bool(conv.get("muted")), "last_inbound": (conv.get("last_inbound") or "")[:120],
        "last_inbound_at": conv.get("last_inbound_at"),
    } for ref, conv in record["conversations"].items()]
    conversations.sort(key=lambda item: item.get("last_inbound_at") or "", reverse=True)
    return {
        "enabled": record["enabled"],
        "mode": record["mode"],
        "poll_seconds": record["poll_seconds"],
        "daily_cap": record["daily_cap"],
        "accounts": [{"id": account, "enabled": cfg.get("enabled", False)}
                     for account, cfg in sorted(record["accounts"].items())],
        "rules": record["rules"],
        "sent_today": _sent_today(record),
        "placeholders": list(VARIABLES),
        "conversations": conversations[:40],
        "outbox": [d for d in record["outbox"] if d.get("status") == "pending"],
        "activity": record["activity"][:20],
        "feature_enabled": feature_enabled("auto_reply"),
        "provider_needed": record["mode"] == AI_MODE,
        "limits": {"max_rules": MAX_RULES, "reply_max": REPLY_MAX, "min_poll_seconds": MIN_POLL_SECONDS,
                   "max_poll_seconds": MAX_POLL_SECONDS, "max_daily_cap": MAX_DAILY_CAP,
                   "chat_cooldown_seconds": CHAT_COOLDOWN_SECONDS, "send_spacing_seconds": SEND_SPACING_SECONDS,
                   "max_accounts": MAX_ACCOUNTS},
        "worker": worker_state,
    }


def _worker_state() -> dict:
    return {"running": bool(worker.task and not worker.task.done()), "error": worker.error,
            "last_tick": worker.last_tick}


# ============ endpoints ============


class ConfigBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    poll_seconds: int | None = None
    daily_cap: int | None = None


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=NAME_MAX)
    match: str = Field(default="keyword", max_length=12)
    keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    reply: str = Field(default="", max_length=REPLY_MAX)
    enabled: bool = True


class AccountBody(BaseModel):
    enabled: bool


class TestBody(BaseModel):
    text: str = Field(default="", max_length=SNIPPET_MAX)
    name: str = Field(default="", max_length=NAME_MAX)


class OutboxEdit(BaseModel):
    reply: str = Field(min_length=1, max_length=REPLY_MAX)


@router.get("/api/auto-reply/config")
async def get_config(request: Request):
    _enabled()
    return {"success": True, "config": public_config(_operator(request), worker_state=_worker_state())}


@router.put("/api/auto-reply/config")
async def put_config(body: ConfigBody, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    updates = body.model_dump(exclude_none=True)
    if "enabled" in updates:
        record["enabled"] = bool(updates["enabled"])
    if "mode" in updates:
        if updates["mode"] not in MODES:
            raise HTTPException(400, "Choose Rules, or AI drafts for approval.")
        record["mode"] = updates["mode"]
    if "poll_seconds" in updates:
        record["poll_seconds"] = _clamp(updates["poll_seconds"], MIN_POLL_SECONDS, MAX_POLL_SECONDS,
                                        DEFAULT_POLL_SECONDS)
    if "daily_cap" in updates:
        record["daily_cap"] = _clamp(updates["daily_cap"], 0, MAX_DAILY_CAP, DEFAULT_DAILY_CAP)
    _save(operator, record)
    log_manager.add_log("Auto Reply", "-",
                        f"Saved settings ({'on' if record['enabled'] else 'off'}, {record['mode']} mode)")
    return {"success": True, "config": public_config(operator, worker_state=_worker_state())}


@router.post("/api/auto-reply/rules")
async def create_rule(body: RuleBody, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    if len(record["rules"]) >= MAX_RULES:
        raise HTTPException(409, TOO_MANY_RULES)
    rule = _clean_rule(body.model_dump())
    record["rules"].append(rule)
    _save(operator, record)
    log_manager.add_log("Auto Reply", "-", f"Added the rule “{rule['name']}”")
    return {"success": True, "rules": record["rules"]}


@router.put("/api/auto-reply/rules/{rule_id}")
async def edit_rule(rule_id: str, body: RuleBody, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    index = next((i for i, rule in enumerate(record["rules"]) if rule["id"] == rule_id), None)
    if index is None:
        raise HTTPException(404, "No rule with that id.")
    record["rules"][index] = _clean_rule({**body.model_dump(), "id": rule_id})
    _save(operator, record)
    return {"success": True, "rules": record["rules"]}


@router.delete("/api/auto-reply/rules/{rule_id}")
async def delete_rule(rule_id: str, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    kept = [rule for rule in record["rules"] if rule["id"] != rule_id]
    if len(kept) == len(record["rules"]):
        raise HTTPException(404, "No rule with that id.")
    record["rules"] = kept
    _save(operator, record)
    return {"success": True, "rules": record["rules"]}


@router.put("/api/auto-reply/accounts/{account_id}")
async def set_account(account_id: str, body: AccountBody, request: Request):
    _enabled()
    operator = _operator(request)
    from account_manager import account_manager

    if account_id not in account_manager.accounts:
        raise HTTPException(404, "No account with that id in this workspace.")
    record = _record(operator)
    if body.enabled:
        watching = [key for key, cfg in record["accounts"].items() if cfg.get("enabled")]
        if account_id not in watching and len(watching) >= MAX_ACCOUNTS:
            raise HTTPException(409, f"Auto Reply watches at most {MAX_ACCOUNTS} accounts.")
        record["accounts"][account_id] = {"enabled": True}
    else:
        record["accounts"].pop(account_id, None)
    _save(operator, record)
    log_manager.add_log("Auto Reply", account_id,
                        "Watching this account for new messages" if body.enabled
                        else "Stopped watching this account")
    return {"success": True, "config": public_config(operator, worker_state=_worker_state())}


@router.post("/api/auto-reply/test")
async def test_reply(body: TestBody, request: Request):
    """Which rule would answer this text, and what it would say. Nothing is sent."""
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Type the message you want to test.")
    if record["mode"] == AI_MODE:
        return {"success": True, "mode": AI_MODE, "would_send": False,
                "detail": "AI mode never sends by itself. A draft would wait in the approval outbox."}
    rule = match_rule(record["rules"], text)
    if not rule:
        return {"success": True, "mode": RULES_MODE, "matched": None, "would_send": False,
                "detail": "No rule matched, so nothing would be sent."}
    return {"success": True, "mode": RULES_MODE, "matched": {"id": rule["id"], "name": rule["name"]},
            "would_send": bool(rule.get("enabled")), "reply": render(rule["reply"], name=body.name)}


@router.get("/api/auto-reply/conversations")
async def conversations(request: Request):
    _enabled()
    return {"success": True, "conversations": public_config(_operator(request))["conversations"]}


@router.post("/api/auto-reply/conversations/{peer_ref}/resume")
async def resume_conversation(peer_ref: str, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    conv = record["conversations"].get(peer_ref)
    if not conv:
        raise HTTPException(404, "No conversation with that id.")
    conv["taken_over"] = False
    conv["muted"] = False
    conv["status"] = "watching"
    _save(operator, record)
    log_manager.add_log("Auto Reply", conv.get("account") or "-", "Resumed auto replies in one conversation")
    return {"success": True}


@router.post("/api/auto-reply/conversations/{peer_ref}/mute")
async def mute_conversation(peer_ref: str, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    conv = record["conversations"].get(peer_ref)
    if not conv:
        raise HTTPException(404, "No conversation with that id.")
    conv["muted"] = True
    conv["status"] = "muted"
    _save(operator, record)
    log_manager.add_log("Auto Reply", conv.get("account") or "-", "Muted auto replies in one conversation")
    return {"success": True}


@router.get("/api/auto-reply/outbox")
async def outbox(request: Request):
    _enabled()
    record = _record(_operator(request))
    return {"success": True, "outbox": [d for d in record["outbox"] if d.get("status") == "pending"]}


@router.put("/api/auto-reply/outbox/{draft_id}")
async def edit_draft(draft_id: str, body: OutboxEdit, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    draft = next((d for d in record["outbox"] if d["id"] == draft_id), None)
    if not draft:
        raise HTTPException(404, GONE_OUTBOX)
    draft["reply"] = _validate_template(body.reply)
    draft["edited"] = True
    _save(operator, record)
    return {"success": True, "outbox": [d for d in record["outbox"] if d.get("status") == "pending"]}


@router.post("/api/auto-reply/outbox/{draft_id}/send")
async def send_draft(draft_id: str, request: Request):
    """A human pressed Send. This is the only path that lets a model's words reach Telegram."""
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    draft = next((d for d in record["outbox"] if d["id"] == draft_id and d.get("status") == "pending"), None)
    if not draft:
        raise HTTPException(404, GONE_OUTBOX)
    if not record["daily_cap"] or _sent_today(record) >= record["daily_cap"]:
        raise HTTPException(409, f"The daily limit of {record['daily_cap']} replies is used up.")
    try:
        if account_cooldown_remaining(draft.get("account")) > 0:
            raise HTTPException(409, "Telegram asked us to wait before this account sends again.")
        if draft.get("suppress_key") and outreach_policy.is_suppressed(draft["suppress_key"]):
            raise HTTPException(409, "That person is on the opt-out list.")
    except OutreachDenied:
        raise HTTPException(503, "The opt-out and pacing lists could not be read, so nothing was sent.") from None
    text = _validate_template(draft["reply"])
    conv = record["conversations"].get(draft.get("peer_ref") or "")
    sent = await _deliver(record, conv, draft.get("account"), text)
    if not sent:
        raise HTTPException(502, "Telegram did not accept the reply. Nothing was marked as sent.")
    record["outbox"] = [d for d in record["outbox"] if d["id"] != draft_id]
    _count_sent(record)
    record["last_send_at"] = time.time()
    _save(operator, record)
    _collect_approved(operator, text)
    log_manager.add_log("Auto Reply", draft.get("account") or "-", "Sent a reply you approved from the outbox")
    return {"success": True, "detail": "Sent.", "outbox": [d for d in record["outbox"] if d.get("status") == "pending"]}


@router.post("/api/auto-reply/outbox/{draft_id}/discard")
async def discard_draft(draft_id: str, request: Request):
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    kept = [d for d in record["outbox"] if d["id"] != draft_id]
    if len(kept) == len(record["outbox"]):
        raise HTTPException(404, GONE_OUTBOX)
    record["outbox"] = kept
    _save(operator, record)
    return {"success": True, "outbox": [d for d in kept if d.get("status") == "pending"]}


def _collect_approved(operator: str, text: str) -> None:
    if not knowledge.settings_for(operator).get("auto_collect_replies"):
        return
    try:
        knowledge.add_document(operator, title="Approved reply", text=text,
                               source="auto_reply", tags=["approved-reply"], dedupe=True)
    except HTTPException:
        log.warning("The approved reply could not be collected into the knowledge base")


async def _deliver(record, conv, account_id, text: str) -> bool:
    if not isinstance(conv, dict) or not conv.get("peer") or not account_id:
        return False
    try:
        client = await _client_getter()(account_id)
    except Exception:
        return False
    if not client:
        return False
    entity = await _resolve(client, conv)
    if entity is None:
        return False
    sent_id = await _send(client, account_id, entity, text)
    if sent_id is None:
        return False
    conv["our_ids"] = (conv.get("our_ids") or [])[-OUR_IDS_PER_CHAT:] + [sent_id]
    conv["last_reply_at"] = time.time()
    conv["status"] = "replied"
    return True


async def _resolve(client, conv):
    peer = str(conv.get("peer"))
    try:
        return await asyncio.wait_for(client.get_entity(int(peer)), timeout=30)
    except (ValueError, TypeError):
        pass
    except Exception:
        pass
    for dialog in await _dialogs(client):
        entity = getattr(dialog, "entity", None)
        if entity is not None and str(getattr(entity, "id", "")) == peer:
            return entity
    return None


@router.post("/api/auto-reply/run-now")
async def run_now(request: Request):
    """One pass, on the operator's own press. The same code the background listener runs."""
    _enabled()
    operator = _operator(request)
    record = _record(operator)
    if not record["enabled"]:
        raise HTTPException(409, "Switch Auto Reply on first.")
    if not [cfg for cfg in record["accounts"].values() if cfg.get("enabled")]:
        raise HTTPException(409, NO_ACCOUNTS)
    result = await poll_operator(operator)
    log_manager.add_log("Auto Reply", "-",
                        f"Checked {result['checked']} account(s): {result['replied']} replied, "
                        f"{result['drafted']} drafted, {result['paused']} paused")
    return {"success": True, "result": result,
            "config": public_config(operator, worker_state=_worker_state())}


# ============ background listener ============


class AutoReplyWorker:
    """One loop for every operator; a bad pass never stops the next one."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.last_tick: str | None = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = type(exc).__name__
                log.exception("Auto reply pass failed")
            await asyncio.sleep(MIN_POLL_SECONDS)

    async def tick(self):
        if not feature_enabled("auto_reply"):
            return
        for operator in _due_operators():
            await poll_operator(operator)
        self.last_tick = _now_iso()


def _due_operators() -> list:
    """Operators whose own poll interval has elapsed."""
    try:
        data = read_secret(STORE, {})
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    now = time.time()
    due = []
    for operator, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        accounts = entry.get("accounts") or {}
        if not any(isinstance(cfg, dict) and cfg.get("enabled") for cfg in accounts.values()):
            continue
        interval = _clamp(entry.get("poll_seconds"), MIN_POLL_SECONDS, MAX_POLL_SECONDS, DEFAULT_POLL_SECONDS)
        if now - _as_float(entry.get("last_pass")) >= interval:
            due.append(operator)
    return due[:MAX_ACCOUNTS]


worker = AutoReplyWorker()
