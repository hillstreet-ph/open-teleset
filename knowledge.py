"""Knowledge base: the notes the dashboard's own AI is allowed to read.

Everything here is written by the operator, collected from the Copilot, or copied
from a template. It is stored encrypted with the same cipher the Telegram sessions
use, scoped to the operator who wrote it, and it never holds a credential: a value
that looks like a key, session string, phone number or password is masked on the way
in, before it touches the disk.

Retrieval is keyword scoring in Python, not an embedding service. Nothing leaves this
container to answer a search; only the snippets a later AI call is asked to use.
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["knowledge"])

STORE = "knowledge"
MAX_DOCS = 200
MAX_DOC_CHARS = 4000
MAX_TITLE = 80
MAX_TAGS = 8
TOP_K_MAX = 8
PREVIEW_CHARS = 180
MIN_SNIPPET_CHARS = 40

SOURCES = ("manual", "copilot", "auto_reply", "template", "import")

DEFAULT_SETTINGS = {
    "auto_collect_copilot": True,
    "auto_collect_replies": True,
    "include_templates": True,
}

# A credential-shaped run of characters, however it is written. Matched before the
# text is stored, so a pasted session string never reaches the file.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_ -]?(?:id|hash)|app[_ -]?api[_ -]?hash|session[_ -]?string|password|passcode|"
               r"two[_ -]?step[_ -]?password|verification[_ -]?code|device[_ -]?code|token|secret|key)"
               r"\s*(?:is|:|=)\s*\S+"),
    re.compile(r"\+\d[\d\s().-]{6,}\d"),
    re.compile(r"t\.me/\+[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b[A-Za-z0-9/+_-]{28,}={0,2}\b"),
]
_STOPWORDS = frozenset("""a an and are as at be but by for from has have how i in is it its of on or our
so that the their them then there these this to us was we were what when where which who why will with you
your can do not just about all more some please thanks thank hi hello""".split())


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def scrub(text: str) -> tuple[str, int]:
    """Mask anything that looks like a credential. Returns the safe text and the count."""
    masked = 0
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned, hits = pattern.subn("[masked]", cleaned)
        masked += hits
    return cleaned, masked


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    if not isinstance(entry, dict):
        entry = {"docs": [], "settings": {}}
    return {
        "docs": [d for d in entry.get("docs", []) if isinstance(d, dict) and d.get("id")],
        "settings": {**DEFAULT_SETTINGS, **{k: bool(v) for k, v in (entry.get("settings") or {}).items()
                                            if k in DEFAULT_SETTINGS}},
    }


def _save(operator: str, record: dict) -> None:
    all_records = _all()
    all_records[operator] = {"docs": record["docs"][:MAX_DOCS], "settings": record["settings"]}
    write_secret(STORE, all_records)


def _public(doc: dict) -> dict:
    text = doc.get("text") or ""
    return {
        "id": doc["id"],
        "title": doc.get("title") or "Untitled",
        "preview": text[:PREVIEW_CHARS],
        "characters": len(text),
        "source": doc.get("source") or "manual",
        "tags": doc.get("tags") or [],
        "masked": bool(doc.get("masked")),
        "created": doc.get("created"),
        "updated": doc.get("updated"),
        "hits": doc.get("hits", 0),
    }


def _tokens(text: str) -> list:
    words = re.findall(r"[a-z0-9\u4e00-\u9fff]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def list_documents(operator: str) -> dict:
    record = _record(operator)
    return {"documents": [_public(d) for d in record["docs"]], "settings": record["settings"],
            "capacity": {"used": len(record["docs"]), "limit": MAX_DOCS}}


def settings_for(operator: str) -> dict:
    return _record(operator)["settings"]


def get_document(operator: str, doc_id: str) -> dict:
    """One note with the full body, so the edit form can show what is actually stored."""
    doc = next((d for d in _record(operator)["docs"] if d["id"] == doc_id), None)
    if not doc:
        raise HTTPException(404, "No knowledge entry with that id.")
    return {**_public(doc), "text": doc.get("text") or ""}


def add_document(operator: str, *, title: str, text: str, source: str = "manual",
                 tags=None, dedupe: bool = False) -> dict:
    """Store one note. Returns the public shape, or None when an identical note exists."""
    body = (text or "").strip()
    if not body:
        raise HTTPException(400, "Write something before saving to the knowledge base.")
    if len(body) > MAX_DOC_CHARS:
        raise HTTPException(400, f"A knowledge entry is limited to {MAX_DOC_CHARS} characters.")
    clean, masked = scrub(body)
    record = _record(operator)
    if dedupe:
        for existing in record["docs"]:
            if (existing.get("text") or "") == clean:
                return None
    if len(record["docs"]) >= MAX_DOCS:
        raise HTTPException(409, f"The knowledge base holds {MAX_DOCS} entries. Delete one before adding another.")
    doc = {
        "id": uuid4().hex[:12],
        "title": (title or "").strip()[:MAX_TITLE] or clean.splitlines()[0][:MAX_TITLE],
        "text": clean,
        "source": source if source in SOURCES else "manual",
        "tags": [str(t).strip()[:24] for t in (tags or []) if str(t).strip()][:MAX_TAGS],
        "masked": masked,
        "created": _now(),
        "updated": _now(),
        "hits": 0,
    }
    record["docs"].insert(0, doc)
    _save(operator, record)
    return _public(doc)


def update_document(operator: str, doc_id: str, *, title: str = None, text: str = None,
                    tags=None) -> dict:
    record = _record(operator)
    doc = next((d for d in record["docs"] if d["id"] == doc_id), None)
    if not doc:
        raise HTTPException(404, "No knowledge entry with that id.")
    if text is not None:
        body = text.strip()
        if not body:
            raise HTTPException(400, "Write something before saving to the knowledge base.")
        if len(body) > MAX_DOC_CHARS:
            raise HTTPException(400, f"A knowledge entry is limited to {MAX_DOC_CHARS} characters.")
        clean, masked = scrub(body)
        doc["text"] = clean
        doc["masked"] = bool(masked)
    if title is not None:
        doc["title"] = title.strip()[:MAX_TITLE] or doc["title"]
    if tags is not None:
        doc["tags"] = [str(t).strip()[:24] for t in tags if str(t).strip()][:MAX_TAGS]
    doc["updated"] = _now()
    _save(operator, record)
    return _public(doc)


def delete_document(operator: str, doc_id: str) -> bool:
    record = _record(operator)
    kept = [d for d in record["docs"] if d["id"] != doc_id]
    if len(kept) == len(record["docs"]):
        return False
    record["docs"] = kept
    _save(operator, record)
    return True


def clear_documents(operator: str, *, source: str = "") -> int:
    record = _record(operator)
    kept = [d for d in record["docs"] if source and d.get("source") != source]
    removed = len(record["docs"]) - len(kept)
    record["docs"] = kept
    _save(operator, record)
    return removed


def _template_documents(operator: str) -> list:
    """Templates are read live so the base cannot drift from what the forms send."""
    try:
        from template_manager import template_manager

        return [{"id": f"template:{key}", "title": value.get("name") or key,
                 "text": value.get("content") or "", "source": "template", "tags": [], "live": True}
                for key, value in (template_manager.templates or {}).items()]
    except Exception:
        return []


def search(operator: str, query: str, *, k: int = 5, include_templates: bool = None) -> list:
    """Rank stored notes against a query. Pure keyword scoring - no provider call."""
    wanted = _tokens(query)
    if not wanted:
        return []
    record = _record(operator)
    candidates = list(record["docs"])
    if include_templates is None:
        include_templates = record["settings"].get("include_templates", True)
    if include_templates:
        candidates += _template_documents(operator)
    scored = []
    for doc in candidates:
        tokens = _tokens(doc.get("text") or "")
        title_tokens = _tokens(doc.get("title") or "")
        if not tokens and not title_tokens:
            continue
        overlaps = {w for w in wanted}
        body_hits = sum(1 for t in tokens if t in overlaps)
        title_hits = sum(1 for t in title_tokens if t in overlaps)
        if not (body_hits or title_hits):
            continue
        score = (title_hits * 3 + min(body_hits, 8)) / (1 + len(tokens) ** 0.5)
        scored.append((score, doc))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[: max(1, min(k, TOP_K_MAX))]
    _count_hits(operator, [d["id"] for _, d in top])
    return [{"id": doc["id"], "title": doc.get("title") or "Untitled",
             "source": doc.get("source") or "manual", "score": round(score, 3),
             "text": doc.get("text") or "", "preview": (doc.get("text") or "")[:PREVIEW_CHARS]}
            for score, doc in top]


def context_for(operator: str, query: str, *, characters: int = 1200) -> str:
    """The grounded snippet block an AI call is allowed to see."""
    parts = []
    used = 0
    for hit in search(operator, query, k=TOP_K_MAX, include_templates=None):
        remaining = characters - used
        if remaining < MIN_SNIPPET_CHARS:
            break
        block = f"- {hit['title']}: {hit['text']}"
        if len(block) > remaining:
            block = block[: max(0, remaining - 1)].rstrip() + "…"
        parts.append(block)
        used += len(block) + 1
    return "\n".join(parts)


def _count_hits(operator: str, ids: list) -> None:
    record = _record(operator)
    changed = False
    for doc in record["docs"]:
        if doc["id"] in ids:
            doc["hits"] = int(doc.get("hits", 0)) + 1
            changed = True
    if changed:
        _save(operator, record)


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


class DocumentBody(BaseModel):
    title: str = Field(default="", max_length=MAX_TITLE)
    text: str = Field(default="", max_length=MAX_DOC_CHARS)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=500)
    k: int = Field(default=5, ge=1, le=TOP_K_MAX)


class SettingsBody(BaseModel):
    auto_collect_copilot: bool | None = None
    auto_collect_replies: bool | None = None
    include_templates: bool | None = None


@router.get("/api/knowledge/documents")
async def documents(request: Request):
    return {"success": True, **list_documents(_operator(request))}


@router.post("/api/knowledge/documents")
async def create_document(body: DocumentBody, request: Request):
    doc = add_document(_operator(request), title=body.title, text=body.text, tags=body.tags)
    return {"success": True, "document": doc}


@router.get("/api/knowledge/documents/{doc_id}")
async def document(doc_id: str, request: Request):
    return {"success": True, "document": get_document(_operator(request), doc_id)}


@router.put("/api/knowledge/documents/{doc_id}")
async def edit_document(doc_id: str, body: dict, request: Request):
    allowed = {k: v for k, v in (body or {}).items() if k in {"title", "text", "tags"}}
    return {"success": True, "document": update_document(_operator(request), doc_id, **allowed)}


@router.delete("/api/knowledge/documents/{doc_id}")
async def remove_document(doc_id: str, request: Request):
    if not delete_document(_operator(request), doc_id):
        raise HTTPException(404, "No knowledge entry with that id.")
    return {"success": True}


@router.post("/api/knowledge/search")
async def search_documents(body: SearchBody, request: Request):
    return {"success": True, "matches": search(_operator(request), body.query, k=body.k)}


@router.get("/api/knowledge/settings")
async def knowledge_settings(request: Request):
    return {"success": True, "settings": settings_for(_operator(request))}


@router.put("/api/knowledge/settings")
async def update_knowledge_settings(body: SettingsBody, request: Request):
    operator = _operator(request)
    record = _record(operator)
    updates = body.model_dump(exclude_none=True)
    record["settings"] = {**record["settings"], **{k: bool(v) for k, v in updates.items()
                                                   if k in DEFAULT_SETTINGS}}
    _save(operator, record)
    return {"success": True, "settings": record["settings"]}


@router.post("/api/knowledge/clear")
async def clear(request: Request, body: dict = None):
    source = str((body or {}).get("source") or "")
    removed = clear_documents(_operator(request), source=source)
    return {"success": True, "removed": removed}
