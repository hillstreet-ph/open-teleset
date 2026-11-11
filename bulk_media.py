"""Operator-owned attachments for BulkSend.

A file is uploaded once, stored under the operator store next to the sessions
and notes, and referenced from then on by an opaque id. The dashboard never
sends a filesystem path: the send route resolves ids server-side and scopes
them to the operator who uploaded them.

Only types Telegram displays naturally are accepted (images, video, audio,
documents). Script-capable formats (HTML, SVG, shell, unknown binaries) are
refused, and the bytes must match the declared type, so a disguised file never
reaches a recipient under a trusted content type.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from open_teleset.operator_store import read_json, store_dir, write_json

router = APIRouter(tags=["bulk-media"])

MANIFEST = "media_manifest"
MAX_BYTES = 100 * 1024 * 1024
MAX_ITEMS = 10
CHUNK = 64 * 1024

# mime -> (kind, extension). Anything absent is refused.
ALLOWED = {
    "image/jpeg": ("image", ".jpg"),
    "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"),
    "image/gif": ("image", ".gif"),
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
    "video/webm": ("video", ".webm"),
    "audio/mpeg": ("audio", ".mp3"),
    "audio/ogg": ("audio", ".ogg"),
    "audio/mp4": ("audio", ".m4a"),
    "audio/wav": ("audio", ".wav"),
    "application/pdf": ("document", ".pdf"),
    "application/zip": ("document", ".zip"),
    "text/plain": ("document", ".txt"),
    "application/msword": ("document", ".doc"),
    "application/vnd.ms-excel": ("document", ".xls"),
    "application/vnd.ms-powerpoint": ("document", ".ppt"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("document", ".docx"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("document", ".xlsx"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("document", ".pptx"),
}


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _manifest() -> dict:
    data = read_json(MANIFEST, {})
    return data if isinstance(data, dict) else {}


def _media_dir() -> Path:
    path = store_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owns(record: dict, operator: str) -> bool:
    return isinstance(record, dict) and record.get("operator") == operator


def _public(record: dict) -> dict:
    return {
        "id": record["id"],
        "name": record["name"],
        "mime": record["mime"],
        "kind": record["kind"],
        "size": record["size"],
        "created": record.get("created"),
        "url": f"/api/bulk/media/{record['id']}",
    }


def _signature_matches(mime: str, head: bytes) -> bool:
    """Cheap byte check so a disguised file never inherits a trusted type."""
    if mime.startswith("image/"):
        return (
            (mime == "image/png" and head.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime == "image/jpeg" and head.startswith(b"\xff\xd8\xff"))
            or (mime == "image/gif" and head.startswith(b"GIF8"))
            or (mime == "image/webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        )
    if mime == "video/mp4" or mime == "audio/mp4":
        return head[4:8] == b"ftyp"
    if mime == "video/quicktime":
        return head[4:8] in (b"ftyp", b"moov", b"mdat")
    if mime == "video/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if mime == "audio/mpeg":
        return head.startswith(b"ID3") or head[:1] == b"\xff"
    if mime == "audio/ogg":
        return head.startswith(b"OggS")
    if mime == "audio/wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if mime == "application/pdf":
        return head.startswith(b"%PDF")
    # Office documents in the OOXML family and zip archives start with PK.
    if mime == "application/zip" or mime.endswith(("wordprocessingml.document",
                                                   "spreadsheetml.sheet",
                                                   "presentationml.presentation")):
        return head.startswith(b"PK")
    # Legacy Office files are OLE compound documents; plain text has no signature.
    return True


def _clean_name(raw: str, ext: str) -> str:
    name = Path(raw or "").name.strip()[:120] or f"attachment{ext}"
    return name.replace("\x00", "")


@router.post("/api/bulk/media")
async def upload_media(request: Request, file: UploadFile):
    operator = _operator(request)
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED:
        raise HTTPException(400, "That file type is not allowed. Use an image, video, audio or document.")
    kind, ext = ALLOWED[mime]
    media_id = uuid4().hex
    stored_name = f"{media_id}{ext}"
    path = _media_dir() / stored_name

    head = b""
    size = 0
    try:
        with path.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f"That file is too large. The limit is "
                                             f"{MAX_BYTES // (1024 * 1024)} MB.")
                handle.write(chunk)
        if not size:
            raise HTTPException(400, "The uploaded file is empty.")
        if not _signature_matches(mime, head):
            raise HTTPException(400, "The file content does not match its declared type.")
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except OSError:
        path.unlink(missing_ok=True)
        raise HTTPException(500, "The attachment could not be stored.") from None

    record = {
        "id": media_id,
        "operator": operator,
        "name": _clean_name(file.filename, ext),
        "mime": mime,
        "kind": kind,
        "size": size,
        "stored_name": stored_name,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    manifest = _manifest()
    manifest[media_id] = record
    write_json(MANIFEST, manifest)
    return {"success": True, "media": _public(record)}


@router.get("/api/bulk/media")
async def list_media(request: Request):
    operator = _operator(request)
    items = [_public(record) for record in _manifest().values() if _owns(record, operator)]
    items.sort(key=lambda item: item.get("created") or "", reverse=True)
    return {"success": True, "media": items}


def _record_for(media_id: str, operator: str) -> dict:
    record = _manifest().get(media_id)
    if not _owns(record, operator):
        raise HTTPException(404, "Attachment not found.")
    if not (store_dir() / "media" / record["stored_name"]).exists():
        raise HTTPException(404, "Attachment not found.")
    return record


@router.get("/api/bulk/media/{media_id}")
async def download_media(media_id: str, request: Request):
    operator = _operator(request)
    record = _record_for(media_id, operator)
    path = store_dir() / "media" / record["stored_name"]
    inline = record["kind"] in ("image", "video", "audio")
    return FileResponse(
        path,
        media_type=record["mime"],
        filename=record["name"],
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff",
                 "Cache-Control": "private, max-age=300"},
    )


@router.delete("/api/bulk/media/{media_id}")
async def delete_media(media_id: str, request: Request):
    operator = _operator(request)
    record = _record_for(media_id, operator)
    (store_dir() / "media" / record["stored_name"]).unlink(missing_ok=True)
    manifest = _manifest()
    manifest.pop(media_id, None)
    write_json(MANIFEST, manifest)
    return {"success": True}


def resolve(ids, operator: str) -> list[dict]:
    """Server-side resolution of attachment ids for the send route."""
    unique = list(dict.fromkeys(i.strip() for i in ids or [] if i and i.strip()))
    if len(unique) > MAX_ITEMS:
        raise HTTPException(400, f"At most {MAX_ITEMS} attachments per send.")
    records = []
    for media_id in unique:
        record = _record_for(media_id, operator)
        path = store_dir() / "media" / record["stored_name"]
        records.append({"id": record["id"], "path": str(path), "mime": record["mime"],
                        "kind": record["kind"], "name": record["name"]})
    return records
