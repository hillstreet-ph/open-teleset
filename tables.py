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
COLUMN_TYPES = ("text", "number", "date", "checkbox")

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


# ============ endpoints ============


class TableCreate(BaseModel):
    name: str = Field(default="")


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
    table = {
        "id": "t_" + uuid4().hex[:10],
        "name": _clean_name(body.name, "table"),
        "columns": [{"id": "c_" + uuid4().hex[:8], "name": "Name", "type": "text"}],
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
