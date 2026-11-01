"""Repeated-pattern detection: the dashboard notices what you keep typing.

The same three notes, phrased the same way, turn up again and again in a support inbox.
This module looks over what the operator has already saved - knowledge entries, Copilot
drafts, approved replies, templates - and proposes the repeats back as something reusable:
a template message, an Auto Reply rule, a prepared task, or the steps of a process worth
automating.

A suggestion never acts on its own. Accepting one saves a template (nothing is sent) or
writes an Auto Reply rule switched off, and for the other kinds it only prefills the form
that already asks for consent.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auto_reply
import copilot
import knowledge
from log_manager import log_manager
from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["patterns"])

STORE = "patterns"
KINDS = ("template", "reply", "task", "automation")
MIN_SUPPORT = 3
WINDOW_DAYS = 14
MAX_SUGGESTIONS = 20
MAX_EVIDENCE = 3
REFRESH_SECONDS = 3600
MIN_TOKENS = 4
SHINGLE = 3
DISMISSED_MAX = 100
MAX_ITEM_CHARS = 600
KEYWORDS_MAX = 3

NO_DATA = ("Nothing to detect yet. Save a few knowledge entries, or let Auto Reply and the "
           "Copilot collect a few exchanges, and check back.")


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
    entry = entry if isinstance(entry, dict) else {}
    return {
        "suggestions": [s for s in (entry.get("suggestions") or []) if isinstance(s, dict) and s.get("id")],
        "dismissed": [str(k) for k in (entry.get("dismissed") or [])][:DISMISSED_MAX],
        "generated_at": entry.get("generated_at") or 0,
    }


def _save(operator: str, record: dict) -> None:
    all_records = _all()
    all_records[operator] = {"suggestions": record["suggestions"][:MAX_SUGGESTIONS],
                             "dismissed": record["dismissed"][:DISMISSED_MAX],
                             "generated_at": record["generated_at"]}
    write_secret(STORE, all_records)


# ============ corpus ============


def build_corpus(operator: str) -> list:
    """Everything the detector is allowed to read, already stripped of credentials."""
    items = []
    for doc in knowledge.list_documents(operator)["documents"]:
        full = _document_text(operator, doc["id"])
        if full:
            items.append({"id": doc["id"], "text": full, "source": doc.get("source") or "manual",
                          "title": doc.get("title") or "", "time": _epoch(doc.get("updated") or doc.get("created"))})
    for turn in copilot._turns(operator):
        text = turn.get("text") or turn.get("instruction") or ""
        if text:
            items.append({"id": f"turn:{turn['id']}", "text": text, "source": "copilot",
                          "action": turn.get("action"), "title": turn.get("title") or "",
                          "time": _epoch(turn.get("time"))})
    return items


def _document_text(operator: str, doc_id: str) -> str:
    for doc in knowledge._record(operator)["docs"]:
        if doc["id"] == doc_id:
            return str(doc.get("text") or "")[:MAX_ITEM_CHARS]
    return ""


def _epoch(value) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def normalise(text: str) -> list:
    """Token sequence the shingles come from: no numbers, no handles, no punctuation."""
    cleaned = re.sub(r"\d+", " ", str(text or "").lower())
    cleaned = re.sub(r"@[a-z0-9_]+", " ", cleaned)
    return knowledge._tokens(cleaned)[:80]


def shingles(tokens: list) -> list:
    if len(tokens) < MIN_TOKENS:
        return []
    return [" ".join(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)]


def detect(items: list, *, now: float = None, min_support: int = MIN_SUPPORT) -> list:
    """Pure: repeated phrasing in, suggestions out. No I/O, no provider call."""
    moment = time.time() if now is None else float(now)
    cutoff = moment - WINDOW_DAYS * 86400
    recent = [item for item in items if item.get("text") and (not item.get("time") or item["time"] >= cutoff)]
    buckets: dict[str, list] = {}
    for item in recent:
        for key in set(shingles(normalise(item["text"]))):
            buckets.setdefault(key, []).append(item["id"])
    ordered = sorted(buckets.items(), key=lambda pair: len(set(pair[1])), reverse=True)
    by_id = {item["id"]: item for item in recent}
    covered: set = set()
    suggestions = []
    for key, ids in ordered:
        members = list(dict.fromkeys(ids))
        if len(members) < min_support:
            break
        fresh = [m for m in members if m not in covered]
        if len(fresh) < min_support:
            continue
        covered.update(members)
        group = [by_id[m] for m in members if m in by_id]
        suggestions.append(_shape(key, group))
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return suggestions


def _kind(group: list) -> str:
    sources = {item.get("source") for item in group}
    actions = {item.get("action") for item in group}
    if "auto_reply" in sources:
        return "reply"
    if actions & {"task", "schedule"}:
        return "task"
    if len(sources) >= 3:
        return "automation"
    if "copilot" in sources or "manual" in sources:
        return "template"
    return "automation"


def _shared_keywords(group: list) -> list:
    counts: dict[str, int] = {}
    for item in group:
        seen = set()
        for token in normalise(item["text"]):
            if token not in seen:
                counts[token] = counts.get(token, 0) + 1
                seen.add(token)
    ranked = sorted(counts.items(), key=lambda pair: (pair[1], len(pair[0])), reverse=True)
    return [word for word, hits in ranked[:KEYWORDS_MAX] if hits >= 2] or [w for w, _ in ranked[:KEYWORDS_MAX]]


def _title_of(group: list, text: str) -> str:
    named = next((item.get("title") for item in group if str(item.get("title") or "").strip()), "")
    if named:
        return str(named)[:70]
    return " ".join(text.split()[:6])[:70]


def _shape(key: str, group: list) -> dict:
    kind = _kind(group)
    representative = max(group, key=lambda item: len(item["text"]))
    text = re.sub(r"\s+", " ", representative["text"]).strip()[:MAX_ITEM_CHARS]
    title = _title_of(group, text)
    keywords = _shared_keywords(group)
    payload = {
        "template": {"name": str(title)[:60], "content": text, "category": "Suggested"},
        "reply": {"name": str(title)[:60], "match": "keyword", "keywords": keywords, "reply": text,
                  "enabled": False},
        "task": {"message": text},
        "automation": {"instruction": f"Create a template named {str(title)[:40]} that says: {text[:200]}",
                       "steps": [f"Someone asks: “{group[0]['text'][:80]}”",
                                 f"Answer with: “{text[:120]}”",
                                 "Save it as a template and add an Auto Reply rule for the wording above."]},
    }[kind]
    return {
        "id": uuid4().hex[:12],
        "kind": kind,
        "signature": key,
        "title": str(title)[:70],
        "text": text,
        "count": len(group),
        "sources": sorted({str(item.get("source") or "") for item in group}),
        "evidence": [re.sub(r"\s+", " ", item["text"])[:110] for item in group[:MAX_EVIDENCE]],
        "payload": payload,
        "status": "new",
        "created": datetime.now().isoformat(timespec="seconds"),
    }


def refresh(operator: str, *, force: bool = False) -> dict:
    record = _record(operator)
    corpus = build_corpus(operator)
    if not force and time.time() - float(record["generated_at"] or 0) < REFRESH_SECONDS:
        return record
    dismissed = set(record["dismissed"])
    kept = [s for s in record["suggestions"] if s.get("status") == "accepted" and s.get("signature") not in dismissed]
    seen = {s.get("signature") for s in kept}
    fresh = [s for s in detect(corpus) if s["signature"] not in dismissed and s["signature"] not in seen]
    record["suggestions"] = (kept + fresh)[:MAX_SUGGESTIONS]
    record["generated_at"] = time.time()
    _save(operator, record)
    return record


def summary(record: dict, corpus_size: int) -> dict:
    pending = [s for s in record["suggestions"] if s.get("status") != "dismissed"]
    return {
        "suggestions": [{k: s.get(k) for k in ("id", "kind", "title", "text", "count", "sources",
                                               "evidence", "payload", "status", "created")} for s in pending],
        "corpus": corpus_size,
        "detected_at": datetime.fromtimestamp(float(record["generated_at"] or 0)).isoformat(timespec="seconds")
        if record.get("generated_at") else "",
        "min_support": MIN_SUPPORT,
        "window_days": WINDOW_DAYS,
        "empty_detail": NO_DATA if not record["suggestions"] and record.get("generated_at") else "",
    }


# ============ endpoints ============


class AcceptBody(BaseModel):
    name: str = ""


@router.get("/api/patterns/suggestions")
async def suggestions(request: Request):
    operator = _operator(request)
    record = refresh(operator)
    return {"success": True, **summary(record, len(build_corpus(operator)))}


@router.post("/api/patterns/refresh")
async def refresh_now(request: Request):
    operator = _operator(request)
    record = refresh(operator, force=True)
    log_manager.add_log("Patterns", "-", f"Detected {len(record['suggestions'])} repeated wording suggestion(s)")
    return {"success": True, **summary(record, len(build_corpus(operator)))}


@router.post("/api/patterns/suggestions/{suggestion_id}/dismiss")
async def dismiss(suggestion_id: str, request: Request):
    operator = _operator(request)
    record = _record(operator)
    suggestion = next((s for s in record["suggestions"] if s["id"] == suggestion_id), None)
    if not suggestion:
        raise HTTPException(404, "No suggestion with that id.")
    suggestion["status"] = "dismissed"
    record["dismissed"] = [suggestion["signature"]] + [k for k in record["dismissed"]
                                                       if k != suggestion["signature"]]
    record["suggestions"] = [s for s in record["suggestions"] if s["id"] != suggestion_id]
    _save(operator, record)
    return {"success": True}


@router.post("/api/patterns/suggestions/{suggestion_id}/accept")
async def accept(suggestion_id: str, body: AcceptBody, request: Request):
    """Save the reusable thing the wording became. Nothing is sent from here."""
    operator = _operator(request)
    record = _record(operator)
    suggestion = next((s for s in record["suggestions"] if s["id"] == suggestion_id), None)
    if not suggestion:
        raise HTTPException(404, "No suggestion with that id.")
    if suggestion.get("status") == "accepted":
        raise HTTPException(409, "That suggestion is already saved.")
    kind = suggestion.get("kind")
    payload = dict(suggestion.get("payload") or {})
    if body.name:
        payload["name"] = body.name[:60]
    if kind == "template":
        created = _save_template(payload)
    elif kind == "reply":
        created = _save_rule(operator, payload)
    else:
        created = {"opens": "bulksend" if kind == "task" else "copilot", "payload": payload}
    suggestion["status"] = "accepted"
    _save(operator, record)
    log_manager.add_log("Patterns", "-", f"Accepted a suggested {kind}")
    return {"success": True, "kind": kind, "result": created}


def _save_template(payload: dict) -> dict:
    from template_manager import template_manager

    name = str(payload.get("name") or "Suggested template")[:60]
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "The suggestion has no message text to save.")
    template_id = f"suggested_{uuid4().hex[:8]}"
    template_manager.add_template(template_id, name, content, payload.get("category") or "Suggested")
    return {"template_id": template_id, "name": name}


def _save_rule(operator: str, payload: dict) -> dict:
    rule = auto_reply._clean_rule({**payload, "id": None})
    rule["enabled"] = False
    record = auto_reply._record(operator)
    if len(record["rules"]) >= auto_reply.MAX_RULES:
        raise HTTPException(409, auto_reply.TOO_MANY_RULES)
    record["rules"].append(rule)
    auto_reply._save(operator, record)
    return {"rule_id": rule["id"], "name": rule["name"], "enabled": False}


@router.get("/api/patterns/settings")
async def get_settings(request: Request):
    _operator(request)
    return {"success": True, "settings": {"min_support": MIN_SUPPORT, "window_days": WINDOW_DAYS,
                                         "max_suggestions": MAX_SUGGESTIONS, "refresh_seconds": REFRESH_SECONDS}}
