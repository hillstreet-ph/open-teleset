"""Operator-built node workflows for internal Telegram actions.

A workflow is a small directed chain the operator draws on a canvas: read a
group's members, wait, join groups the account may join, or send itself a note.
It is deliberately not a general automation engine. There is no node that
creates accounts, buys numbers, adds strangers or sends cold messages, because
those stay on their own consent-gated surfaces.

Workflows are scoped to the operator and encrypted with the same store used by
data tables. Run progress lives in process memory only: it tells the operator
what a run is doing, it is not an audit log.
"""

from __future__ import annotations

import asyncio
import math
import re
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from log_manager import log_manager
from open_teleset.operator_store import read_secret, secret_key_available, write_secret

router = APIRouter(tags=["workflows"])

STORE = "workflows"
MAX_WORKFLOWS = 50
MAX_NODES = 20
MAX_NAME = 80
MAX_WAIT_STEP = 600
MAX_WAIT_TOTAL = 1800
SCRAPE_LIMIT_MAX = 1000
JOIN_LIMIT = 10
JOIN_MIN_DELAY = 35
STEP_TIMEOUT_SECONDS = 650.0
RUN_HISTORY = 10

NODE_TYPES = ("scrape", "wait", "join", "notify")
_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

CATALOG = [
    {
        "type": "scrape",
        "label": "Scrape members",
        "hint": "Reads members of a group this account already belongs to.",
        "fields": [
            {"key": "target", "label": "Group or @username", "kind": "text", "placeholder": "@group"},
            {"key": "limit", "label": "Members", "kind": "number", "min": 1, "max": SCRAPE_LIMIT_MAX},
            {"key": "filter_bots", "label": "Skip bots", "kind": "checkbox"},
        ],
    },
    {
        "type": "wait",
        "label": "Wait",
        "hint": "Pauses between actions so the cycle stays conservative.",
        "fields": [
            {"key": "seconds", "label": "Seconds", "kind": "number", "min": 1, "max": MAX_WAIT_STEP},
        ],
    },
    {
        "type": "join",
        "label": "Join groups",
        "hint": "Joins public groups or invite links as this account. Bounded and spaced out.",
        "fields": [
            {"key": "urls", "label": "One link or @username per line", "kind": "lines"},
            {"key": "delay", "label": "Seconds between joins", "kind": "number", "min": JOIN_MIN_DELAY, "max": 3600},
        ],
    },
    {
        "type": "notify",
        "label": "Note yourself",
        "hint": "Sends a note to this account's own Saved Messages. Never to other people.",
        "fields": [
            {"key": "text", "label": "Text ({scraped} and {joined} are filled in)", "kind": "textarea"},
        ],
    },
]

_LOCK = threading.RLock()
_RUNS: dict[str, deque] = {}
_RUNNING: set[str] = set()
_TASKS: dict[str, asyncio.Task] = {}


class WorkflowPayload(BaseModel):
    name: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class RunPayload(BaseModel):
    account_id: str = ""


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so nothing can be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _record(operator: str) -> list:
    entry = _all().get(operator)
    entry = entry if isinstance(entry, dict) else {}
    return [w for w in (entry.get("workflows") or []) if isinstance(w, dict)
            and w.get("id") and isinstance(w.get("nodes"), list)]


def _save(operator: str, workflows: list) -> None:
    all_records = _all()
    all_records[operator] = {"workflows": workflows[:MAX_WORKFLOWS]}
    write_secret(STORE, all_records)


def _as_int(value, low: int, high: int, message: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(message)
    return value


def _join_targets(raw_urls):
    if not isinstance(raw_urls, list) or not 1 <= len(raw_urls) <= JOIN_LIMIT:
        raise ValueError(f"Join needs between 1 and {JOIN_LIMIT} links.")
    targets = []
    for raw in raw_urls:
        if not isinstance(raw, str) or len(raw) > 256:
            raise ValueError("Every join link must be a short piece of text.")
        cleaned = raw.strip()
        if not cleaned:
            raise ValueError("Every join link must be filled in.")
        targets.append(cleaned)
    return targets


def _clean_config(node_type: str, raw) -> dict:
    config = raw if isinstance(raw, dict) else {}
    if node_type == "scrape":
        target = str(config.get("target") or "").strip()
        if not target or len(target) > 256:
            raise ValueError("A scrape node needs the group it should read.")
        limit = _as_int(config.get("limit", 200), 1, SCRAPE_LIMIT_MAX, "Scrape members must be 1 to 1000.")
        filter_bots = config.get("filter_bots", True)
        if not isinstance(filter_bots, bool):
            raise ValueError("Skip bots is either on or off.")
        return {"target": target, "limit": limit, "filter_bots": filter_bots}
    if node_type == "wait":
        seconds = _as_int(config.get("seconds", 30), 1, MAX_WAIT_STEP, "Wait must be 1 to 600 seconds.")
        return {"seconds": seconds}
    if node_type == "join":
        urls = _join_targets(config.get("urls"))
        delay = config.get("delay", 35)
        if type(delay) not in (int, float) or isinstance(delay, bool) \
                or not math.isfinite(delay) or not JOIN_MIN_DELAY <= delay <= 3600:
            raise ValueError("Seconds between joins must be 35 or more.")
        return {"urls": urls, "delay": delay}
    if node_type == "notify":
        text = str(config.get("text") or "").strip()
        if not text or len(text) > 1000:
            raise ValueError("A note needs between 1 and 1000 characters.")
        return {"text": text}
    raise ValueError("Unknown node type.")


def validate_workflow(payload: dict) -> dict:
    """Return a cleaned workflow or raise ValueError with an operator-facing reason."""
    name = str(payload.get("name") or "").strip()[:MAX_NAME]
    if not name:
        raise ValueError("Give the workflow a name first.")
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_NODES:
        raise ValueError(f"A workflow needs between 1 and {MAX_NODES} nodes.")

    nodes: list[dict] = []
    seen_ids: set[str] = set()
    wait_total = 0
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ValueError("Each node must be an object.")
        node_id = str(raw.get("id") or "").strip()
        if not _NODE_ID.match(node_id):
            raise ValueError("Every node needs a short id.")
        if node_id in seen_ids:
            raise ValueError("Two nodes share the same id.")
        seen_ids.add(node_id)
        node_type = str(raw.get("type") or "").strip()
        if node_type not in NODE_TYPES:
            raise ValueError("That node type is not one of the supported internal actions.")
        config = _clean_config(node_type, raw.get("config"))
        if node_type == "wait":
            wait_total += config["seconds"]
        position = {}
        for axis, fallback in (("x", 40 + (index % 5) * 240), ("y", 40 + (index // 5) * 200)):
            value = raw.get(axis, fallback)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or not 0 <= value <= 4000:
                raise ValueError("A node sits outside the canvas.")
            position[axis] = int(round(value))
        nodes.append({"id": node_id, "type": node_type, "config": config,
                      "x": position["x"], "y": position["y"]})
    if wait_total > MAX_WAIT_TOTAL:
        raise ValueError(f"Waiting for more than {MAX_WAIT_TOTAL} seconds in one workflow is too long.")

    raw_edges = payload.get("edges")
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("Connections must be a list.")
    edges: list[dict] = []
    pairs: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise ValueError("Each connection must be an object.")
        source, target = raw.get("from"), raw.get("to")
        if source not in seen_ids or target not in seen_ids:
            raise ValueError("A connection points at a node that is not on the canvas.")
        if source == target or (source, target) in pairs:
            raise ValueError("A connection must join two different nodes once.")
        pairs.add((source, target))
        edges.append({"from": source, "to": target})

    outgoing: dict[str, str] = {}
    incoming: dict[str, str] = {}
    for source, target in pairs:
        if source in outgoing:
            raise ValueError("A node can lead to only one next node.")
        if target in incoming:
            raise ValueError("A node can only be reached from one previous node.")
        outgoing[source] = target
        incoming[target] = source
    heads = [node["id"] for node in nodes if node["id"] not in incoming]
    tails = [node["id"] for node in nodes if node["id"] not in outgoing]
    if len(heads) != 1:
        raise ValueError("Connect every node into one chain with a single starting node.")
    if len(tails) != 1:
        raise ValueError("Connect every node into one chain with a single ending node.")
    visited: list[str] = []
    cursor = heads[0]
    while cursor and cursor not in visited:
        visited.append(cursor)
        cursor = outgoing.get(cursor)
    if cursor is not None:
        raise ValueError("Connections cannot loop back to an earlier node.")
    if len(visited) != len(nodes):
        raise ValueError("Every node must be part of the chain, not floating on its own.")

    return {"name": name, "nodes": nodes, "edges": edges}


def _ordered_nodes(workflow: dict) -> list[dict]:
    nodes = {node["id"]: node for node in workflow.get("nodes", [])}
    outgoing = {edge["from"]: edge["to"] for edge in workflow.get("edges", [])}
    incoming = {edge["to"] for edge in workflow.get("edges", [])}
    heads = [node_id for node_id in nodes if node_id not in incoming]
    ordered, cursor = [], heads[0] if heads else None
    while cursor and cursor in nodes and cursor not in ordered:
        ordered.append(nodes[cursor])
        cursor = outgoing.get(cursor)
    return ordered


def _new_run(workflow: dict) -> dict:
    return {
        "id": "run_" + uuid4().hex[:10],
        "workflow_id": workflow["id"],
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "stopped": None,
        "steps": [
            {"node_id": node["id"], "type": node["type"], "status": "pending",
             "detail": {}, "started_at": None, "finished_at": None}
            for node in _ordered_nodes(workflow)
        ],
    }


def _history(workflow_id: str) -> deque:
    with _LOCK:
        return _RUNS.setdefault(workflow_id, deque(maxlen=RUN_HISTORY))


def _summary(workflow: dict) -> dict:
    history = list(_history(workflow["id"]))
    return {**workflow, "last_run": history[0] if history else None, "running": workflow["id"] in _RUNNING}


async def _client(account_id: str):
    import account_manager as accounts_module

    client = await accounts_module.account_manager.get_client(account_id)
    if not client:
        raise RuntimeError("account_not_connected")
    return client


async def _scrape_members(config: dict, context: dict, account_id: str) -> dict:
    client = await _client(account_id)
    entity = await client.get_entity(config["target"])
    participants = await client.get_participants(entity, limit=config["limit"])
    members = [p for p in participants if not (config["filter_bots"] and getattr(p, "bot", False))]
    context["scraped"] = len(members)
    return {"scraped": len(members)}


async def _join_groups(config: dict, context: dict, account_id: str) -> dict:
    from telethon import errors, functions as tl_functions

    from telegram_ops import _parse_join_target

    client = await _client(account_id)
    joined = skipped = failed = 0
    for index, raw in enumerate(config["urls"]):
        target = _parse_join_target(raw)
        try:
            if target and target[0] == "invite":
                await client(tl_functions.messages.ImportChatInviteRequest(target[1]))
            elif target:
                entity = await client.get_entity(target[1])
                await client(tl_functions.channels.JoinChannelRequest(entity))
            else:
                failed += 1
        except errors.UserAlreadyParticipantError:
            skipped += 1
        except errors.FloodWaitError as exc:
            context["joined"] = joined
            return {"joined": joined, "skipped": skipped, "failed": failed,
                    "stopped": "telegram_cooldown", "wait_seconds": int(exc.seconds)}
        except Exception:
            failed += 1
        if index < len(config["urls"]) - 1:
            await asyncio.sleep(config["delay"])
    context["joined"] = joined
    return {"joined": joined, "skipped": skipped, "failed": failed}


async def _wait(config: dict, context: dict, account_id: str) -> dict:
    await asyncio.sleep(config["seconds"])
    return {"waited_seconds": config["seconds"]}


async def _notify_self(config: dict, context: dict, account_id: str) -> dict:
    text = config["text"]
    for token, value in (("{scraped}", context.get("scraped", "?")),
                         ("{joined}", context.get("joined", "?"))):
        text = text.replace(token, str(value))
    client = await _client(account_id)
    await client.send_message("me", text[:1000])
    return {"sent": True, "characters": len(text)}


HANDLERS: dict[str, Callable[[dict, dict, str], Awaitable[dict]]] = {
    "scrape": _scrape_members,
    "wait": _wait,
    "join": _join_groups,
    "notify": _notify_self,
}


async def execute(workflow: dict, account_id: str, run: dict, handlers: dict | None = None) -> dict:
    """Run a validated workflow, updating ``run`` in place. Never raises."""
    handlers = handlers or HANDLERS
    nodes = {node["id"]: node for node in workflow["nodes"]}
    context: dict = {}
    try:
        for step in run["steps"]:
            node = nodes[step["node_id"]]
            step["status"] = "running"
            step["started_at"] = _now()
            handler = handlers.get(node["type"])
            if handler is None:
                step.update(status="failed", detail={"error": "unsupported_node"})
                run["status"] = "failed"
                break
            try:
                detail = await asyncio.wait_for(
                    handler(node["config"], context, account_id), timeout=STEP_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never echo exception text: it can carry tokens or peer data.
                step.update(status="failed", detail={"error": "step_failed"}, finished_at=_now())
                run["status"] = "failed"
                break
            step.update(status="ok", detail=detail if isinstance(detail, dict) else {}, finished_at=_now())
            if isinstance(detail, dict) and detail.get("stopped"):
                run["status"] = "stopped"
                run["stopped"] = detail["stopped"]
                break
        else:
            run["status"] = "succeeded"
    except asyncio.CancelledError:
        run["status"] = "stopped"
        run["stopped"] = "cancelled_by_operator"
        for step in run["steps"]:
            if step["status"] in {"pending", "running"}:
                step.update(status="stopped", finished_at=_now())
        raise
    finally:
        if run["status"] == "running":
            run["status"] = "failed"
        run["finished_at"] = _now()
    return run


async def _run_and_record(workflow: dict, account_id: str, run: dict, handlers=None) -> None:
    workflow_id = workflow["id"]
    try:
        await execute(workflow, account_id, run, handlers)
    except asyncio.CancelledError:
        pass
    finally:
        with _LOCK:
            _RUNNING.discard(workflow_id)
            _TASKS.pop(workflow_id, None)
        log_manager.add_log("Workflow", workflow_id,
                            f"Run {run['id']} finished: {run['status']}", "info")


def start_run(workflow: dict, account_id: str, handlers=None) -> dict:
    """Queue a run and return its record before any step has executed."""
    if workflow["id"] in _RUNNING:
        raise HTTPException(409, "This workflow is already running.")
    run = _new_run(workflow)
    with _LOCK:
        _RUNNING.add(workflow["id"])
        # The run is recorded before it starts so polling clients can watch
        # step progress; the same dict is mutated as steps execute.
        _history(workflow["id"]).appendleft(run)
    task = asyncio.get_running_loop().create_task(
        _run_and_record(workflow, account_id, run, handlers))
    with _LOCK:
        _TASKS[workflow["id"]] = task
    return run


@router.get("/api/workflows")
async def list_workflows(request: Request):
    operator = _operator(request)
    items = _record(operator)
    return {
        "workflows": [_summary(workflow) for workflow in items],
        "capacity": {"used": len(items), "limit": MAX_WORKFLOWS, "nodes": MAX_NODES},
        "catalog": CATALOG,
    }


@router.post("/api/workflows")
async def create_workflow(request: Request, payload: WorkflowPayload):
    operator = _operator(request)
    items = _record(operator)
    if len(items) >= MAX_WORKFLOWS:
        raise HTTPException(400, f"Keep at most {MAX_WORKFLOWS} workflows.")
    try:
        cleaned = validate_workflow(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    workflow = {"id": "wf_" + uuid4().hex[:12], **cleaned,
                "created_at": _now(), "updated_at": _now()}
    _save(operator, [workflow, *items])
    return {"workflow": _summary(workflow)}


@router.put("/api/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, request: Request, payload: WorkflowPayload):
    operator = _operator(request)
    items = _record(operator)
    current = next((item for item in items if item["id"] == workflow_id), None)
    if current is None:
        raise HTTPException(404, "That workflow does not exist.")
    if workflow_id in _RUNNING:
        raise HTTPException(409, "Stop the running workflow before changing it.")
    try:
        cleaned = validate_workflow(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated = {**current, **cleaned, "updated_at": _now()}
    _save(operator, [updated if item["id"] == workflow_id else item for item in items])
    return {"workflow": _summary(updated)}


@router.delete("/api/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, request: Request):
    operator = _operator(request)
    items = _record(operator)
    if not any(item["id"] == workflow_id for item in items):
        raise HTTPException(404, "That workflow does not exist.")
    task = _TASKS.get(workflow_id)
    if task is not None:
        task.cancel()
    _save(operator, [item for item in items if item["id"] != workflow_id])
    with _LOCK:
        _RUNS.pop(workflow_id, None)
    return {"success": True}


@router.post("/api/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, request: Request, payload: RunPayload):
    operator = _operator(request)
    workflow = next((item for item in _record(operator) if item["id"] == workflow_id), None)
    if workflow is None:
        raise HTTPException(404, "That workflow does not exist.")
    import account_manager as accounts_module

    if payload.account_id not in accounts_module.account_manager.accounts:
        raise HTTPException(400, "Choose a signed-in account to run this workflow.")
    run = start_run(workflow, payload.account_id)
    return {"run": run}


@router.post("/api/workflows/{workflow_id}/stop")
async def stop_workflow(workflow_id: str, request: Request):
    operator = _operator(request)
    if not any(item["id"] == workflow_id for item in _record(operator)):
        raise HTTPException(404, "That workflow does not exist.")
    task = _TASKS.get(workflow_id)
    if task is None:
        raise HTTPException(409, "That workflow is not running.")
    task.cancel()
    return {"success": True}


@router.get("/api/workflows/{workflow_id}/runs")
async def workflow_runs(workflow_id: str, request: Request):
    operator = _operator(request)
    if not any(item["id"] == workflow_id for item in _record(operator)):
        raise HTTPException(404, "That workflow does not exist.")
    return {"runs": list(_history(workflow_id))}