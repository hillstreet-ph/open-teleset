"""Data tables: small, operator-owned sheets for lists that outgrow a note.

A table is the same idea as a knowledge entry, but structured: named columns of
text, number, date or checkbox cells, and rows the operator edits in the
dashboard. Everything is scoped to the operator who created it and stored
encrypted with the same cipher the Telegram sessions use, so a backup of the
JSON files never exposes it.

The store is deliberately not a credential locker. A cell that looks like a
session string or a base64 key is rejected instead of masked, because silently
altering spreadsheet data would be worse than telling the operator to keep
credentials in the account setup where they belong.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
from datetime import date, datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from log_manager import log_manager
from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["tables"])

STORE = "tables"
MAX_TABLES = 30
MAX_COLUMNS = 20
MAX_ROWS = 2000
MAX_CELL = 500
MAX_NAME = 80
MAX_IMPORT_CHARS = 600_000
COLUMN_TYPES = ("text", "number", "date", "checkbox")
TABLE_PRESETS = {
    "contacts": ("Contacts", [("Username", "text"), ("Name", "text"),
                              ("Status", "text"), ("Notes", "text")]),
}
_IMPORT_FORMATS = ("auto", "csv", "tsv", "json")
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")

_LOCK = threading.RLock()
_INTEGER = re.compile(r"^-?\d+$")
_NUMBER = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    entry = entry if isinstance(entry, dict) else {}
    return {"tables": [t for t in (entry.get("tables") or []) if isinstance(t, dict)
                       and t.get("id") and isinstance(t.get("columns"), list)]}


def _save(operator: str, record: dict) -> None:
    all_records = _all()
    all_records[operator] = {"tables": record["tables"][:MAX_TABLES]}
    write_secret(STORE, all_records)


def _clean_name(value: str, what: str) -> str:
    name = str(value or "").strip()[:MAX_NAME]
    if not name:
        raise HTTPException(400, f"Give the {what} a name first.")
    return name


def _looks_secret(text: str) -> bool:
    """A session string, authorization key or API key does not belong in a cell."""
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) >= 256 and re.fullmatch(r"[0-9a-fA-F]+", compact):
        return True
    if len(compact) >= 300 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return True
    return False


def _clean_value(column: dict, value: Any) -> Any:
    """Normalise one cell for its column. Blank clears the cell (None)."""
    kind = column.get("type")
    if kind == "checkbox":
        if value in (None, ""):
            return False
        if not isinstance(value, bool):
            raise HTTPException(400, f"Column '{column.get('name')}' holds a checkbox, so the value must be true or false.")
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if kind == "number":
        if isinstance(value, bool):
            raise HTTPException(400, f"Column '{column.get('name')}' holds numbers.")
        if isinstance(value, (int, float)):
            number = float(value)
            if not math.isfinite(number):
                raise HTTPException(400, f"Column '{column.get('name')}' holds finite numbers only.")
            return value if isinstance(value, int) else number
        raw = str(value).strip()
        if not _NUMBER.match(raw):
            raise HTTPException(400, f"Column '{column.get('name')}' holds numbers, and '{raw[:40]}' is not one.")
        return int(float(raw)) if _INTEGER.match(raw) else float(raw)
    if kind == "date":
        raw = str(value).strip()
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise HTTPException(400, f"Column '{column.get('name')}' holds dates as YYYY-MM-DD.")
        return raw
    text = str(value)
    if "\x00" in text:
        raise HTTPException(400, "A cell cannot contain NUL characters.")
    if _looks_secret(text):
        raise HTTPException(400, "Data tables hold working data only. Keep credentials and session strings "
                                 "in the account setup, not in a cell.")
    if len(text) > MAX_CELL:
        raise HTTPException(400, f"A cell is limited to {MAX_CELL} characters.")
    return text


def _find(record: dict, table_id: str) -> dict:
    table = next((t for t in record["tables"] if t["id"] == table_id), None)
    if not table:
        raise HTTPException(404, "No data table with that id.")
    return table


def _find_column(table: dict, column_id: str) -> dict:
    column = next((c for c in table["columns"] if c["id"] == column_id), None)
    if not column:
        raise HTTPException(404, "No column with that id.")
    return column


def _find_row(table: dict, row_id: str) -> dict:
    row = next((r for r in table["rows"] if r["id"] == row_id), None)
    if not row:
        raise HTTPException(404, "No row with that id.")
    return row


def _clean_cells(table: dict, cells: Dict[str, Any]) -> dict:
    known = {c["id"]: c for c in table["columns"]}
    cleaned = {}
    for column_id, value in (cells or {}).items():
        column = known.get(column_id)
        if not column:
            raise HTTPException(400, "That row mentions a column that is not in this table.")
        cleaned[column_id] = _clean_value(column, value)
    return cleaned


def _public_table(table: dict) -> dict:
    return {
        "id": table["id"],
        "name": table.get("name") or "Untitled",
        "columns": [{"id": c["id"], "name": c.get("name") or "Column", "type": c.get("type") or "text"}
                    for c in table["columns"]],
        "rows": [{"id": r["id"], "cells": dict(r.get("cells") or {}), "updated": r.get("updated")}
                 for r in table["rows"]],
        "created": table.get("created"),
        "updated": table.get("updated"),
    }


def _summary(table: dict) -> dict:
    return {
        "id": table["id"],
        "name": table.get("name") or "Untitled",
        "columns": len(table["columns"]),
        "rows": len(table["rows"]),
        "updated": table.get("updated"),
    }


def _touch(table: dict) -> None:
    table["updated"] = _now()


def _coerce_cell(column: dict, value: Any) -> Any:
    """Import files bring strings; a checkbox cell still has to end up a boolean."""
    if column.get("type") == "checkbox" and isinstance(value, str):
        low = value.strip().lower()
        if low in ("", "0", "false", "no", "off"):
            return False
        if low in ("1", "true", "yes", "on"):
            return True
        raise HTTPException(400, f"Column '{column.get('name')}' holds a checkbox, so use true or false.")
    if column.get("type") == "date" and isinstance(value, str):
        slash = _SLASH_DATE.match(value.strip())
        if slash:
            value = f"{slash.group(1)}-{int(slash.group(2)):02d}-{int(slash.group(3)):02d}"
    return _clean_value(column, value)


def _detect_format(content: str, requested: str) -> str:
    if requested in ("csv", "tsv", "json"):
        return requested
    head = content.lstrip()[:1]
    if head in ("[", "{"):
        return "json"
    first_line = content.splitlines()[0] if content.splitlines() else ""
    return "tsv" if "\t" in first_line and "," not in first_line else "csv"


def _json_rows(content: str) -> tuple[list[str], list[list]]:
    try:
        payload = json.loads(content)
    except ValueError:
        raise HTTPException(400, "That is not valid JSON.")
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        if len(rows) != len(payload):
            raise HTTPException(400, "Each JSON row must be an object of column values.")
        headers: list[str] = []
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        return headers, [[row.get(header) for header in headers] for row in rows]
    if isinstance(payload, dict):
        raw_columns = payload.get("columns") or []
        if isinstance(raw_columns, list) and all(isinstance(c, dict) for c in raw_columns):
            headers = [str(c.get("name") or "").strip() for c in raw_columns]
        elif isinstance(raw_columns, list):
            headers = [str(c).strip() for c in raw_columns]
        else:
            raise HTTPException(400, "JSON columns must be a list of names.")
        if not headers or not all(headers):
            raise HTTPException(400, "A JSON import needs a column per value.")
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            raise HTTPException(400, "JSON rows must be a list.")
        table_rows = []
        for row in rows:
            if isinstance(row, dict):
                table_rows.append([row.get(header) for header in headers])
            elif isinstance(row, list):
                if len(row) > len(headers):
                    raise HTTPException(400, "A JSON row has more values than columns.")
                table_rows.append(list(row) + [None] * (len(headers) - len(row)))
            else:
                raise HTTPException(400, "Each JSON row must be a list or an object.")
        return headers, table_rows
    raise HTTPException(400, "JSON import needs a list of rows or {columns, rows}.")


def _sheet_rows(content: str, fmt: str) -> tuple[list[str], list[list[str]]]:
    delimiter = "\t" if fmt == "tsv" else ","
    csv.field_size_limit(1_000_000)
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not rows:
        raise HTTPException(400, "The import is empty.")
    headers = [str(header).strip() for header in rows[0]]
    if not headers or not all(headers):
        raise HTTPException(400, "Every imported column needs a header.")
    return headers, [row for row in rows[1:]]


def _import_into(table: dict, headers: list[str], rows: list[list], column_map: Dict[str, str]) -> dict:
    by_name = {str(column.get("name") or "").strip().lower(): column for column in table["columns"]}
    by_id = {column["id"]: column for column in table["columns"]}
    mapping: dict[str, dict] = {}
    created: list[str] = []
    for header in headers:
        target = column_map.get(header) or header
        column = by_id.get(target) or by_name.get(str(target).strip().lower())
        if column is None and column_map.get(header):
            raise HTTPException(400, f"The import maps '{header}' to a column that is not in this table.")
        if column is None:
            if len(table["columns"]) >= MAX_COLUMNS:
                raise HTTPException(409, f"A table holds {MAX_COLUMNS} columns. Remove one before importing more.")
            column = {"id": "c_" + uuid4().hex[:8], "name": _clean_name(str(header), "column"),
                      "type": "text"}
            table["columns"].append(column)
            by_name[column["name"].lower()] = column
            created.append(column["name"])
        mapping[header] = column
    cleaned_rows = []
    for index, row in enumerate(rows, start=2):
        cells = {}
        for position, header in enumerate(headers):
            value = row[position] if position < len(row) else None
            try:
                cells[mapping[header]["id"]] = _coerce_cell(mapping[header], value)
            except HTTPException as exc:
                raise HTTPException(400, f"Row {index}, column '{header}': {exc.detail}") from exc
        cleaned_rows.append(cells)
    return {"cells": cleaned_rows, "created": created}


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(name or "table").lower()).strip("-")
    return (cleaned or "table")[:60]


def append_to_table(operator: str, table_id: str, column_id: str, values: list[str]) -> dict:
    """Append one row per distinct value. Used by workflow table nodes."""
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        column = _find_column(table, column_id)
        if column.get("type") != "text":
            raise HTTPException(400, "The workflow column must be a text column.")
        seen = {str((row.get("cells") or {}).get(column_id) or "").strip().lower()
                for row in table["rows"]}
        seen.discard("")
        added = skipped = 0
        for raw in values:
            text = str(raw or "").strip()
            if not text:
                continue
            cleaned = _clean_value(column, text)
            key = str(cleaned).lower()
            if key in seen:
                skipped += 1
                continue
            if len(table["rows"]) >= MAX_ROWS:
                break
            seen.add(key)
            table["rows"].append({"id": "r_" + uuid4().hex[:8], "cells": {column_id: cleaned},
                                  "created": _now(), "updated": _now()})
            added += 1
        _touch(table)
        _save(operator, record)
        return {"appended": added, "skipped": skipped, "total": len(table["rows"])}


def read_column(operator: str, table_id: str, column_id: str) -> dict:
    """Read one text column in row order. Used by the workflow table read node."""
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        column = _find_column(table, column_id)
        if column.get("type") != "text":
            raise HTTPException(400, "The workflow column must be a text column.")
        values = []
        for row in table["rows"]:
            value = (row.get("cells") or {}).get(column_id)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        return {"values": values, "rows": len(table["rows"]),
                "table": table.get("name") or "Untitled", "column": column.get("name") or "Column"}


# ============ endpoints ============


class TableCreate(BaseModel):
    name: str = ""
    preset: str = ""


class TableImport(BaseModel):
    format: str = "auto"
    content: str = ""
    mode: str = "append"
    column_map: Dict[str, str] = Field(default_factory=dict)


class TableRename(BaseModel):
    name: str = Field(default="")


class ColumnCreate(BaseModel):
    name: str = Field(default="")
    type: str = Field(default="text")


class ColumnUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None


class RowCreate(BaseModel):
    cells: Dict[str, Any] = Field(default_factory=dict)


class RowUpdate(BaseModel):
    cells: Dict[str, Any] = Field(default_factory=dict)


@router.get("/api/tables")
async def list_tables(request: Request):
    operator = _operator(request)
    record = _record(operator)
    return {"success": True,
            "tables": [_summary(t) for t in record["tables"]],
            "capacity": {"used": len(record["tables"]), "limit": MAX_TABLES,
                         "max_columns": MAX_COLUMNS, "max_rows": MAX_ROWS}}


@router.post("/api/tables")
async def create_table(body: TableCreate, request: Request):
    operator = _operator(request)
    record = _record(operator)
    if len(record["tables"]) >= MAX_TABLES:
        raise HTTPException(409, f"The account holds {MAX_TABLES} tables. Delete one before adding another.")
    preset = (body.preset or "").strip()
    if preset and preset not in TABLE_PRESETS:
        raise HTTPException(400, "That table preset does not exist.")
    if preset:
        default_name, columns = TABLE_PRESETS[preset]
        name = _clean_name(body.name or default_name, "table")
        column_rows = [{"id": "c_" + uuid4().hex[:8], "name": column, "type": kind}
                       for column, kind in columns]
    else:
        name = _clean_name(body.name, "table")
        column_rows = [{"id": "c_" + uuid4().hex[:8], "name": "Name", "type": "text"}]
    table = {
        "id": "t_" + uuid4().hex[:10],
        "name": name,
        "columns": column_rows,
        "rows": [],
        "created": _now(),
        "updated": _now(),
    }
    record["tables"].insert(0, table)
    _save(operator, record)
    log_manager.add_log("Tables", table["id"], f"Created data table '{table['name']}'")
    return {"success": True, "table": _public_table(table)}


@router.get("/api/tables/{table_id}")
async def get_table(table_id: str, request: Request):
    operator = _operator(request)
    return {"success": True, "table": _public_table(_find(_record(operator), table_id))}


@router.patch("/api/tables/{table_id}")
async def rename_table(table_id: str, body: TableRename, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        table["name"] = _clean_name(body.name, "table")
        _touch(table)
        _save(operator, record)
    return {"success": True, "table": _public_table(table)}


@router.delete("/api/tables/{table_id}")
async def delete_table(table_id: str, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        record["tables"] = [t for t in record["tables"] if t["id"] != table_id]
        _save(operator, record)
    log_manager.add_log("Tables", table_id, f"Deleted data table '{table.get('name')}'")
    return {"success": True}


@router.post("/api/tables/{table_id}/columns")
async def add_column(table_id: str, body: ColumnCreate, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        if len(table["columns"]) >= MAX_COLUMNS:
            raise HTTPException(409, f"A table holds {MAX_COLUMNS} columns.")
        if body.type not in COLUMN_TYPES:
            raise HTTPException(400, "A column is text, number, date or checkbox.")
        column = {"id": "c_" + uuid4().hex[:8], "name": _clean_name(body.name, "column"), "type": body.type}
        table["columns"].append(column)
        _touch(table)
        _save(operator, record)
    return {"success": True, "column": column, "table": _public_table(table)}


@router.patch("/api/tables/{table_id}/columns/{column_id}")
async def update_column(table_id: str, column_id: str, body: ColumnUpdate, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        column = _find_column(table, column_id)
        if body.type is not None and body.type not in COLUMN_TYPES:
            raise HTTPException(400, "A column is text, number, date or checkbox.")
        new_name = _clean_name(body.name, "column") if body.name is not None else column.get("name")
        new_type = body.type or column.get("type") or "text"
        if new_type != column.get("type"):
            for row in table["rows"]:
                value = (row.get("cells") or {}).get(column_id)
                if value not in (None, ""):
                    try:
                        row["cells"][column_id] = _clean_value({"name": new_name, "type": new_type}, value)
                    except HTTPException:
                        raise HTTPException(409, f"Clear or fix the existing values in '{column.get('name')}' "
                                                 f"before changing it to {new_type}.")
        column["name"] = new_name
        column["type"] = new_type
        _touch(table)
        _save(operator, record)
    return {"success": True, "column": column, "table": _public_table(table)}


@router.delete("/api/tables/{table_id}/columns/{column_id}")
async def delete_column(table_id: str, column_id: str, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        column = _find_column(table, column_id)
        if len(table["columns"]) <= 1:
            raise HTTPException(409, "A table keeps at least one column.")
        table["columns"] = [c for c in table["columns"] if c["id"] != column_id]
        for row in table["rows"]:
            (row.get("cells") or {}).pop(column_id, None)
        _touch(table)
        _save(operator, record)
    return {"success": True, "deleted": column_id, "table": _public_table(table)}


@router.post("/api/tables/{table_id}/import")
async def import_rows(table_id: str, body: TableImport, request: Request):
    operator = _operator(request)
    if body.format not in _IMPORT_FORMATS:
        raise HTTPException(400, "Import as CSV, TSV or JSON.")
    if body.mode not in ("append", "replace"):
        raise HTTPException(400, "An import either appends to the table or replaces its rows.")
    content = body.content or ""
    if not content.strip():
        raise HTTPException(400, "Paste or choose a file to import first.")
    if len(content) > MAX_IMPORT_CHARS:
        raise HTTPException(413, "An import is limited to about 600 KB.")
    fmt = _detect_format(content, body.format)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        headers, raw_rows = _json_rows(content) if fmt == "json" else _sheet_rows(content, fmt)
        if len({header.lower() for header in headers}) != len(headers):
            raise HTTPException(400, "Two imported columns share the same header.")
        if not raw_rows:
            raise HTTPException(400, "The import has a header but no rows.")
        if body.mode == "replace":
            if len(raw_rows) > MAX_ROWS:
                raise HTTPException(409, f"A table holds {MAX_ROWS} rows.")
        elif len(table["rows"]) + len(raw_rows) > MAX_ROWS:
            raise HTTPException(409, f"A table holds {MAX_ROWS} rows. Import {MAX_ROWS - len(table['rows'])} "
                                     "or replace the rows instead.")
        result = _import_into(table, headers, raw_rows, body.column_map or {})
        if body.mode == "replace":
            table["rows"] = []
        for cells in result["cells"]:
            table["rows"].append({"id": "r_" + uuid4().hex[:8], "cells": cells,
                                  "created": _now(), "updated": _now()})
        _touch(table)
        _save(operator, record)
    log_manager.add_log("Tables", table_id,
                        f"Imported {len(result['cells'])} rows ({fmt}, {body.mode})")
    return {"success": True, "imported": len(result["cells"]), "mode": body.mode,
            "created_columns": result["created"], "table": _public_table(table)}


@router.get("/api/tables/{table_id}/export")
async def export_table(table_id: str, request: Request, format: str = "csv"):
    operator = _operator(request)
    if format not in ("csv", "json"):
        raise HTTPException(400, "Export as CSV or JSON.")
    table = _find(_record(operator), table_id)
    names = [str(column.get("name") or "Column") for column in table["columns"]]
    if format == "json":
        payload = {
            "name": table.get("name"),
            "columns": [{"name": column.get("name"), "type": column.get("type")}
                        for column in table["columns"]],
            "rows": [{name: (row.get("cells") or {}).get(column["id"])
                      for name, column in zip(names, table["columns"])}
                     for row in table["rows"]],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(names)
        for row in table["rows"]:
            cells = row.get("cells") or {}
            writer.writerow(["true" if cells.get(column["id"]) is True else
                             "false" if cells.get(column["id"]) is False else
                             "" if cells.get(column["id"]) is None else cells.get(column["id"])
                             for column in table["columns"]])
        content = stream.getvalue()
    return {"success": True, "format": format, "filename": f"{_slug(table.get('name'))}.{format}",
            "rows": len(table["rows"]), "content": content}


@router.post("/api/tables/{table_id}/rows")
async def add_row(table_id: str, body: RowCreate, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        if len(table["rows"]) >= MAX_ROWS:
            raise HTTPException(409, f"A table holds {MAX_ROWS} rows.")
        row = {"id": "r_" + uuid4().hex[:8], "cells": _clean_cells(table, body.cells),
               "created": _now(), "updated": _now()}
        table["rows"].append(row)
        _touch(table)
        _save(operator, record)
    return {"success": True, "row": row, "table": _public_table(table)}


@router.patch("/api/tables/{table_id}/rows/{row_id}")
async def update_row(table_id: str, row_id: str, body: RowUpdate, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        row = _find_row(table, row_id)
        row.setdefault("cells", {}).update(_clean_cells(table, body.cells))
        row["updated"] = _now()
        _touch(table)
        _save(operator, record)
    return {"success": True, "row": row, "table": _public_table(table)}


@router.delete("/api/tables/{table_id}/rows/{row_id}")
async def delete_row(table_id: str, row_id: str, request: Request):
    operator = _operator(request)
    with _LOCK:
        record = _record(operator)
        table = _find(record, table_id)
        _find_row(table, row_id)
        table["rows"] = [r for r in table["rows"] if r["id"] != row_id]
        _touch(table)
        _save(operator, record)
    return {"success": True, "table": _public_table(table)}
