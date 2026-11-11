"""Notes: a private scratchpad the operator keeps for later.

A note is a message they want to reuse, a snippet, a reminder - text they would
otherwise leave in a chat with themselves. It is stored encrypted with the
session cipher, scoped to the operator who wrote it, and never read by the
Copilot or any automation. A credential-shaped value is masked on the way in,
exactly as the knowledge base does it, so a pasted key never reaches the file.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from knowledge import scrub
from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["notes"])

STORE = "notes"
MAX_NOTES = 2000
MAX_NOTE_CHARS = 20000
MAX_TITLE = 120
PREVIEW_CHARS = 180


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
    notes = entry.get("notes") if isinstance(entry, dict) else None
    return {"notes": [n for n in (notes or []) if isinstance(n, dict) and n.get("id")]}


def _save(operator: str, record: dict) -> None:
    all_records = _all()
    all_records[operator] = {"notes": record["notes"][:MAX_NOTES]}
    write_secret(STORE, all_records)


def _public(note: dict) -> dict:
    text = note.get("text") or ""
    return {
        "id": note["id"],
        "title": note.get("title") or "Untitled",
        "preview": text[:PREVIEW_CHARS],
        "characters": len(text),
        "masked": bool(note.get("masked")),
        "created": note.get("created"),
        "updated": note.get("updated"),
    }


def _full(note: dict) -> dict:
    return {**_public(note), "text": note.get("text") or ""}


def _clean_body(text: str) -> tuple[str, int]:
    body = (text or "").strip()
    if not body:
        raise HTTPException(400, "Write something before saving the note.")
    if len(body) > MAX_NOTE_CHARS:
        raise HTTPException(400, f"A note is limited to {MAX_NOTE_CHARS} characters.")
    return scrub(body)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_notes(operator: str) -> dict:
    record = _record(operator)
    return {"notes": [_public(n) for n in record["notes"]],
            "capacity": {"used": len(record["notes"]), "limit": MAX_NOTES}}


def get_note(operator: str, note_id: str) -> dict:
    note = next((n for n in _record(operator)["notes"] if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, "No note with that id.")
    return _full(note)


def add_note(operator: str, *, title: str = "", text: str) -> dict:
    clean, masked = _clean_body(text)
    record = _record(operator)
    if len(record["notes"]) >= MAX_NOTES:
        raise HTTPException(409, f"Notes hold {MAX_NOTES} entries. Delete one before adding another.")
    note = {
        "id": uuid4().hex[:12],
        "title": (title or "").strip()[:MAX_TITLE] or clean.splitlines()[0][:MAX_TITLE],
        "text": clean,
        "masked": bool(masked),
        "created": _now(),
        "updated": _now(),
    }
    record["notes"].insert(0, note)
    _save(operator, record)
    return _full(note)


def update_note(operator: str, note_id: str, *, title: str = None, text: str = None) -> dict:
    record = _record(operator)
    note = next((n for n in record["notes"] if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, "No note with that id.")
    if text is not None:
        clean, masked = _clean_body(text)
        note["text"] = clean
        note["masked"] = bool(masked)
    if title is not None:
        note["title"] = title.strip()[:MAX_TITLE] or note["title"]
    note["updated"] = _now()
    _save(operator, record)
    return _full(note)


def delete_note(operator: str, note_id: str) -> bool:
    record = _record(operator)
    kept = [n for n in record["notes"] if n["id"] != note_id]
    if len(kept) == len(record["notes"]):
        return False
    record["notes"] = kept
    _save(operator, record)
    return True


class NoteBody(BaseModel):
    title: str = Field(default="", max_length=MAX_TITLE)
    text: str = Field(default="", max_length=MAX_NOTE_CHARS)


@router.get("/api/notes")
async def notes_list(request: Request):
    return {"success": True, **list_notes(_operator(request))}


@router.post("/api/notes")
async def note_create(body: NoteBody, request: Request):
    return {"success": True, "note": add_note(_operator(request), title=body.title, text=body.text)}


@router.get("/api/notes/{note_id}")
async def note_read(note_id: str, request: Request):
    return {"success": True, "note": get_note(_operator(request), note_id)}


@router.put("/api/notes/{note_id}")
async def note_edit(note_id: str, body: dict, request: Request):
    allowed = {k: v for k, v in (body or {}).items() if k in {"title", "text"}}
    return {"success": True, "note": update_note(_operator(request), note_id, **allowed)}


@router.delete("/api/notes/{note_id}")
async def note_delete(note_id: str, request: Request):
    if not delete_note(_operator(request), note_id):
        raise HTTPException(404, "No note with that id.")
    return {"success": True}
