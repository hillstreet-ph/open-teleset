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

import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from log_manager import log_manager
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

# Import bounds. A zip is unpacked defensively: only text or readable documents,
# never a path.
IMPORT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json",
                             ".log", ".yml", ".yaml", ".xml", ".html", ".htm"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp", ".rtf"})
LEGACY_SUFFIXES = frozenset({".doc", ".xls", ".ppt"})
MAX_IMPORT_FILES = 25
MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ENTRY_BYTES = 512 * 1024
MAX_DOCUMENT_ENTRY_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ENTRIES = 80
MAX_IMPORT_BYTES = 6 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000

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


def _zip_name(value: str, fallback: str) -> str:
    """A title becomes a file name, never a path."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return cleaned[:60] or fallback


def entry_name(value: str, fallback: str = "upload") -> str:
    """An uploaded name reduced to its basename, so a zip member cannot escape."""
    base = str(value or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[\x00-\x1f]", "", base)
    return base[:120] or fallback


def _decode(data: bytes) -> str | None:
    """Text bytes to a string. None when the payload is binary rather than a note."""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        probe = text[:2000]
        readable = sum(1 for ch in probe if ch.isprintable() or ch in "\r\n\t")
        if readable / max(1, len(probe)) < 0.85:
            continue
        return text
    return None


def split_chunks(text: str, limit: int = MAX_DOC_CHARS) -> list:
    """Break a long note on blank lines so each stored entry fits the doc limit."""
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks: list = []
    current = ""
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(block), limit):
                chunks.append(block[start:start + limit])
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _tag(node) -> str:
    """An ElementTree tag without its namespace."""
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _capped(text: str) -> str:
    return (text or "")[:MAX_EXTRACTED_CHARS]


def _parse_xml(raw: bytes):
    """Parse an Office XML part, refusing any DTD or entity declaration."""
    if b"<!doctype" in raw[:65536].lower() or b"<!entity" in raw[:65536].lower():
        raise ET.ParseError("DTD or entity declaration refused")
    return ET.fromstring(raw)


def _pdf_text(data: bytes) -> tuple:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF reading is not enabled on this server yet"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list = []
        total = 0
        for page in reader.pages[:300]:
            page_text = page.extract_text() or ""
            pages.append(page_text)
            total += len(page_text)
            if total >= MAX_EXTRACTED_CHARS:
                break
    except Exception:
        return "", "that PDF could not be read"
    return _capped("\n\n".join(pages)), None


def _xml_paragraphs(root) -> str:
    paragraphs = []
    for node in root.iter():
        if _tag(node) != "p":
            continue
        text = "".join(child.text or "" for child in node.iter()
                       if _tag(child) == "t" and child.text)
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _ooxml_text(data: bytes, member: str) -> tuple:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            raw = archive.read(member)
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return "", "that file could not be opened"
    try:
        return _xml_paragraphs(_parse_xml(raw)), None
    except ET.ParseError:
        return "", "that file could not be read"


def _pptx_text(data: bytes) -> tuple:
    chunks: list = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            slides = sorted((name for name in archive.namelist()
                             if re.match(r"ppt/slides/slide\d+\.xml$", name)),
                            key=lambda name: int(re.search(r"(\d+)", name).group(1)))
            for name in slides[:200]:
                chunk = _xml_paragraphs(_parse_xml(archive.read(name)))
                if chunk.strip():
                    chunks.append(chunk)
                    total += len(chunk)
                if total >= MAX_EXTRACTED_CHARS:
                    break
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return "", "that PowerPoint file could not be opened"
    except ET.ParseError:
        return "", "that PowerPoint file could not be read"
    return _capped("\n\n".join(chunks)), None


def _xlsx_text(data: bytes) -> tuple:
    lines: list = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            shared: list = []
            if "xl/sharedStrings.xml" in names:
                root = _parse_xml(archive.read("xl/sharedStrings.xml"))
                for item in root.iter():
                    if _tag(item) == "si":
                        shared.append("".join(node.text or "" for node in item.iter()
                                              if _tag(node) == "t"))
            sheets = sorted((name for name in names
                             if re.match(r"xl/worksheets/sheet\d+\.xml$", name)),
                            key=lambda name: int(re.search(r"(\d+)", name).group(1)))
            total = 0
            for name in sheets:
                root = _parse_xml(archive.read(name))
                for row in root.iter():
                    if _tag(row) != "row":
                        continue
                    cells = []
                    for cell in row:
                        if _tag(cell) != "c":
                            continue
                        value = ""
                        for node in cell.iter():
                            if _tag(node) == "v" and node.text is not None:
                                value = node.text
                                break
                            if _tag(node) == "t" and node.text is not None:
                                value = node.text
                                break
                        if value and cell.get("t") == "s":
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(value)
                    if any(item.strip() for item in cells):
                        line = "\t".join(cells)
                        lines.append(line)
                        total += len(line)
                        if total >= MAX_EXTRACTED_CHARS:
                            break
                if total >= MAX_EXTRACTED_CHARS:
                    break
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return "", "that spreadsheet could not be opened"
    except ET.ParseError:
        return "", "that spreadsheet could not be read"
    return _capped("\n".join(lines)), None


def _odf_text(data: bytes) -> tuple:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            raw = archive.read("content.xml")
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return "", "that OpenDocument file could not be opened"
    try:
        root = _parse_xml(raw)
    except ET.ParseError:
        return "", "that OpenDocument file could not be read"
    paragraphs = []
    for node in root.iter():
        if _tag(node) not in {"p", "h"}:
            continue
        text = "".join(child.text or "" for child in node.iter() if child.text)
        if text.strip():
            paragraphs.append(text)
    return _capped("\n".join(paragraphs)), None


def _rtf_text(data: bytes) -> tuple:
    raw = _decode(data)
    if raw is None:
        return "", "that RTF file is not readable text"
    text = re.sub(r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|pict|object|header|footer|"
                  r"themedata|colorschememapping|datastore|latentstyles|rsidtbl|generator)\b"
                  r"[^{}]*\}", " ", raw)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
    text = re.sub(r"\\u-?\d+\??", "", text)
    text = re.sub(r"\\(?:par|line)\b ?", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return _capped(re.sub(r"[ \t]{2,}", " ", text).strip()), None


def _read_document(label: str, suffix: str, data: bytes) -> tuple:
    """A PDF or Office/OpenDocument file to plain text, plus a reason when it fails."""
    if suffix == ".pdf":
        text, reason = _pdf_text(data)
    elif suffix == ".docx":
        text, reason = _ooxml_text(data, "word/document.xml")
    elif suffix == ".pptx":
        text, reason = _pptx_text(data)
    elif suffix == ".xlsx":
        text, reason = _xlsx_text(data)
    elif suffix in {".odt", ".ods", ".odp"}:
        text, reason = _odf_text(data)
    else:
        text, reason = _rtf_text(data)
    if reason:
        return "", f"{label}: {reason}"
    if not text or not text.strip():
        return "", f"{label}: no readable text was found"
    return _capped(text), None


def extract_texts(name: str, data: bytes) -> tuple:
    """Read one upload into (entry_name, text) pairs, plus a reason when unreadable.

    A zip is walked in memory: no extraction to disk, no directory entries, no
    member larger than the entry cap, and only suffixes that carry readable text.
    PDF, Word, PowerPoint, spreadsheet and OpenDocument files are read too.
    """
    label = entry_name(name)
    suffix = ("." + label.rsplit(".", 1)[-1].lower()) if "." in label else ""
    if suffix != ".zip":
        if suffix in IMPORT_SUFFIXES:
            text = _decode(data)
            if text is None:
                return [], f"{label}: that file is not text"
            if not text.strip():
                return [], f"{label}: the file is empty"
            return [(label, _capped(text))], None
        if suffix in DOCUMENT_SUFFIXES:
            text, reason = _read_document(label, suffix, data)
            return ([(label, text)], None) if text else ([], reason)
        if suffix in LEGACY_SUFFIXES:
            return [], f"{label}: save legacy Office files as PDF, DOCX, XLSX or PPTX first"
        return [], (f"{label}: only text notes, PDF, Word, PowerPoint, spreadsheet, "
                    "OpenDocument or a zip of them can be read")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return [], f"{label}: that zip could not be opened"
    entries: list = []
    skipped: list = []
    with archive:
        for info in archive.infolist():
            if len(entries) >= MAX_IMPORT_ENTRIES:
                skipped.append(f"{label}: stopped at {MAX_IMPORT_ENTRIES} members")
                break
            if info.is_dir():
                continue
            member = entry_name(info.filename)
            member_suffix = ("." + member.rsplit(".", 1)[-1].lower()) if "." in member else ""
            is_document = member_suffix in DOCUMENT_SUFFIXES
            if member_suffix not in IMPORT_SUFFIXES and not is_document:
                continue
            cap = MAX_DOCUMENT_ENTRY_BYTES if is_document else MAX_IMPORT_ENTRY_BYTES
            if info.file_size > cap:
                skipped.append(f"{label}/{member}: larger than {cap // 1024} KB")
                continue
            try:
                with archive.open(info) as handle:
                    raw = handle.read(cap + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                skipped.append(f"{label}/{member}: could not be read")
                continue
            if len(raw) > cap:
                skipped.append(f"{label}/{member}: larger than {cap // 1024} KB")
                continue
            if is_document:
                text, reason = _read_document(f"{label}/{member}", member_suffix, raw)
                if not text:
                    if reason:
                        skipped.append(reason)
                    continue
            else:
                text = _decode(raw)
                if text is None or not text.strip():
                    continue
                text = _capped(text)
            entries.append((f"{label}/{member}", text))
    if not entries:
        detail = skipped[0] if skipped else f"{label}: no readable text member"
        return [], detail
    return entries, (skipped[0] if skipped else None)


def import_files(operator: str, files: list) -> dict:
    """Store each readable upload, chunked and masked, skipping duplicates."""
    summary = {"imported": 0, "duplicates": 0, "masked": 0, "characters": 0,
               "files": [], "entries": [], "skipped_files": [], "errors": []}
    capacity = MAX_DOCS - len(_record(operator)["docs"])
    for item in files:
        name = entry_name(item.get("name") or "upload")
        data = item.get("data")
        supplied = item.get("text")
        if supplied is not None:
            pairs = [(name, str(supplied))] if str(supplied).strip() else []
            reason = None if pairs else f"{name}: nothing to read"
            data = b""
        elif data is None:
            reason = item.get("reason") or "could not be read"
            summary["skipped_files"].append({"name": name, "reason": reason})
            summary["files"].append({"name": name, "stored": 0, "duplicates": 0,
                                     "chunks": 0, "reason": reason})
            continue
        else:
            pairs, reason = extract_texts(name, data)
        if not pairs:
            reason = reason or f"{name}: nothing to import"
            summary["skipped_files"].append({"name": name, "reason": reason})
            summary["files"].append({"name": name, "stored": 0, "duplicates": 0,
                                     "chunks": 0, "reason": reason})
            continue
        stored = duplicates = chunks = 0
        for source_name, text in pairs:
            chunks += 1
            parts = split_chunks(text)
            for index, chunk in enumerate(parts):
                if capacity <= 0:
                    summary["errors"].append(
                        f"The knowledge base is full at {MAX_DOCS} entries; the rest was not imported.")
                    break
                title = f"{source_name} ({index + 1}/{len(parts)})" if len(parts) > 1 else source_name
                try:
                    doc = add_document(operator, title=title, text=chunk, source="import",
                                       tags=["import"], dedupe=True)
                except HTTPException as exc:
                    summary["errors"].append(str(exc.detail))
                    capacity = 0
                    break
                if doc is None:
                    duplicates += 1
                    continue
                stored += 1
                capacity -= 1
                summary["masked"] += 1 if doc.get("masked") else 0
                summary["characters"] += doc.get("characters") or 0
                summary["entries"].append(doc)
            if capacity <= 0:
                break
        summary["imported"] += stored
        summary["duplicates"] += duplicates
        summary["files"].append({"name": name, "stored": stored, "duplicates": duplicates,
                                 "chunks": chunks, **({"note": reason} if reason else {})})
    return summary


def build_zip(operator: str, *, include_templates: bool = None) -> tuple[bytes, int]:
    """The operator's own notes as a zip of markdown files plus an index.

    Credential-shaped values are scrubbed again on the way out, so an export never
    carries a secret even if a note predates the masking rule.
    """
    record = _record(operator)
    if include_templates is None:
        include_templates = record["settings"].get("include_templates", True)
    templates = _template_documents(operator) if include_templates else []
    buffer = io.BytesIO()
    entries = []
    used = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, docs in (("documents", record["docs"]), ("templates", templates)):
            for doc in docs:
                title = doc.get("title") or "Untitled"
                base = _zip_name(title, str(doc.get("id") or "note"))
                name, counter = base, 2
                while f"{folder}/{name}.md" in used:
                    name = f"{base}-{counter}"
                    counter += 1
                path = f"{folder}/{name}.md"
                used.add(path)
                clean, masked = scrub(doc.get("text") or "")
                archive.writestr(path, f"# {title}\n\n{clean}\n")
                entries.append({"path": path, "id": doc.get("id"), "title": title,
                                "source": doc.get("source") or "manual",
                                "tags": doc.get("tags") or [], "masked": masked,
                                "characters": len(clean)})
        index = {"exported": _now(), "documents": len(record["docs"]),
                 "templates": len(templates), "files": entries}
        archive.writestr("index.json", json.dumps(index, indent=2, ensure_ascii=False))
    return buffer.getvalue(), len(entries)


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


@router.post("/api/knowledge/import")
async def import_knowledge(request: Request, files: list[UploadFile] = File(...)):
    """Add text notes from uploads: plain text formats, or a zip of them."""
    operator = _operator(request)
    if not files:
        raise HTTPException(400, "Choose at least one file to import.")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(400, f"Import at most {MAX_IMPORT_FILES} files at a time.")
    payloads = []
    total = 0
    for upload in files:
        label = entry_name(upload.filename or "upload")
        data = await upload.read(MAX_IMPORT_FILE_BYTES + 1)
        if len(data) > MAX_IMPORT_FILE_BYTES:
            payloads.append({"name": label, "data": None,
                             "reason": f"larger than {MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB"})
            continue
        total += len(data)
        if total > MAX_IMPORT_BYTES:
            raise HTTPException(400, f"Keep one import under {MAX_IMPORT_BYTES // (1024 * 1024)} MB in total.")
        payloads.append({"name": label, "data": data})
    result = import_files(operator, payloads)
    log_manager.add_log("Knowledge", "-",
                        f"Imported {result['imported']} knowledge entries from {len(files)} file(s)")
    return {"success": True, **result}


@router.get("/api/knowledge/export")
async def export_knowledge(request: Request, include_templates: bool = None):
    """Download the operator's own knowledge base as a zip of markdown files."""
    operator = _operator(request)
    payload, count = build_zip(operator, include_templates=include_templates)
    stamp = _now().replace(":", "-")
    log_manager.add_log("Knowledge", "-", f"Exported {count} knowledge entries as a zip")
    return Response(content=payload, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="knowledge-{stamp}.zip"'})
