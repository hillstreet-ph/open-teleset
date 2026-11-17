"""Unified automation engine: one durable model for every automated action.

Definitions are operator-scoped and encrypted (trigger + ordered steps). A run
is written to a sqlite ledger before any step starts, so an operator can see
what ran, what was dispatched and why it failed. Every action reuses the
component that already owns it:

- workflow / scrape / forward  -> existing runners
- auto join / bulk send / member add / transfer -> durable jobs
- notify / webhook / MCP / AI  -> integration surface
- schedule                     -> the shared TaskScheduler clock
- event                        -> terminal outreach jobs

Nothing here invents a second clock or a second send path. Steps that hand
work to the durable job queue record the job id and finish immediately; the
job itself keeps its own receipts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx
from croniter import croniter
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from open_teleset.crypto.cipher import SessionCipher
from open_teleset.operator_store import (
    feature_enabled,
    read_secret,
    secret_key_available,
    write_secret,
)
from open_teleset.telegram_notify import queue_event
from outreach_policy import hash_identifier

from log_manager import log_manager

router = APIRouter(tags=["automations"])

STORE = "automations"
MAX_AUTOMATIONS = 40
MAX_STEPS = 30
RUNS_LIMIT = 100
# A run detail can carry scraped names or an AI answer; it is encrypted at rest
# and never returned unbounded.
DETAIL_LIMIT = 4000

TRIGGER_TYPES = ("manual", "schedule", "event")
JOB_EVENTS = ("job.terminal", "job.completed", "job.failed", "job.stopped")


def _runs_db() -> Path:
    return Path(os.getenv("AUTOMATION_RUNS_DB", "./accounts/automation_runs.sqlite3"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Encryption helpers


def _seal(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)[:DETAIL_LIMIT]
    if secret_key_available():
        return SessionCipher().encrypt(text)
    # Definitions already require the key; a degraded ledger must still be
    # readable rather than silently vanish.
    return "plain:" + text


def _open(value: str | None) -> Any:
    if not value:
        return None
    try:
        text = SessionCipher().decrypt(value) if not value.startswith("plain:") else value[6:]
        return json.loads(text)
    except (ValueError, RuntimeError, TypeError):
        return {"unreadable": True}


# ---------------------------------------------------------------------------
# Definition store


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(403, "Authenticated operator required")
    return str(user.id)


def _all() -> dict:
    if not secret_key_available():
        raise HTTPException(500, "SESSION_ENCRYPTION_KEY is not configured, so automations cannot be stored.")
    data = read_secret(STORE, {})
    return data if isinstance(data, dict) else {}


def _record(operator: str) -> dict:
    entry = _all().get(operator)
    automations = entry.get("automations") if isinstance(entry, dict) else None
    return {"automations": [a for a in (automations or [])
                            if isinstance(a, dict) and a.get("id")]}


def _save(operator: str, automations: list) -> None:
    everything = _all()
    everything[operator] = {"automations": automations[:MAX_AUTOMATIONS]}
    write_secret(STORE, everything)


def _new_id() -> str:
    return "auto_" + uuid4().hex[:10]


def _find(operator: str, automation_id: str) -> dict:
    entry = next((a for a in _record(operator)["automations"] if a["id"] == automation_id), None)
    if entry is None:
        raise HTTPException(404, "Automation does not exist")
    return entry


# ---------------------------------------------------------------------------
# Run ledger


@contextmanager
def _db():
    path = _runs_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS runs ("
                   "run_id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, operator TEXT NOT NULL,"
                   "status TEXT NOT NULL, trigger TEXT NOT NULL, trigger_key TEXT NOT NULL DEFAULT '',"
                   "trigger_detail TEXT, started REAL NOT NULL, finished REAL NOT NULL DEFAULT 0,"
                   "reason TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_trigger "
                   "ON runs(automation_id, operator, trigger_key) WHERE trigger_key!=''")
        db.execute("CREATE TABLE IF NOT EXISTS run_steps ("
                   "run_id TEXT NOT NULL, seq INTEGER NOT NULL, action TEXT NOT NULL,"
                   "status TEXT NOT NULL, detail TEXT, started REAL NOT NULL,"
                   "finished REAL NOT NULL DEFAULT 0, PRIMARY KEY(run_id, seq))")
        db.commit()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()


def _run_rows(operator: str, automation_id: Optional[str] = None, limit: int = 20) -> list:
    owner = hash_identifier(operator)
    with _db() as db:
        if automation_id:
            rows = db.execute("SELECT * FROM runs WHERE operator=? AND automation_id=? "
                              "ORDER BY started DESC LIMIT ?", (owner, automation_id, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM runs WHERE operator=? ORDER BY started DESC LIMIT ?",
                              (owner, limit)).fetchall()
    return [_public_run(row) for row in rows]


def _public_run(row) -> dict:
    return {
        "run_id": row["run_id"],
        "automation_id": row["automation_id"],
        "status": row["status"],
        "trigger": row["trigger"],
        "trigger_detail": _open(row["trigger_detail"]),
        "started": row["started"],
        "finished": row["finished"],
        "reason": row["reason"],
    }


def _run_detail(operator: str, run_id: str) -> dict:
    owner = hash_identifier(operator)
    with _db() as db:
        row = db.execute("SELECT * FROM runs WHERE run_id=? AND operator=?", (run_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        steps = db.execute("SELECT * FROM run_steps WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    return {
        **_public_run(row),
        "steps": [{"seq": s["seq"], "action": s["action"], "status": s["status"],
                   "detail": _open(s["detail"]), "started": s["started"], "finished": s["finished"]}
                  for s in steps],
    }


class Run:
    """A durable handle on one execution. Each step is written before it runs."""

    def __init__(self, operator: str, automation_id: str, trigger: str,
                 trigger_key: str = "", trigger_detail: Any = None):
        self.operator = operator
        self.automation_id = automation_id
        self.run_id = "run_" + uuid4().hex[:12]
        self.status = "running"
        with _db() as db:
            db.execute("INSERT INTO runs(run_id,automation_id,operator,status,trigger,trigger_key,"
                       "trigger_detail,started) VALUES(?,?,?,?,?,?,?,?)",
                       (self.run_id, automation_id, hash_identifier(operator), "running", trigger,
                        trigger_key, _seal(trigger_detail) if trigger_detail is not None else None,
                        time.time()))

    def step(self, seq: int, action: str) -> None:
        with _db() as db:
            db.execute("INSERT OR REPLACE INTO run_steps(run_id,seq,action,status,started) "
                       "VALUES(?,?,?,'running',?)", (self.run_id, seq, action, time.time()))

    def result(self, seq: int, status: str, detail: Any = None) -> None:
        with _db() as db:
            db.execute("UPDATE run_steps SET status=?,detail=?,finished=? WHERE run_id=? AND seq=?",
                       (status, _seal(detail) if detail is not None else None,
                        time.time(), self.run_id, seq))

    def finish(self, status: str, reason: str = "") -> None:
        self.status = status
        with _db() as db:
            db.execute("UPDATE runs SET status=?,reason=?,finished=? WHERE run_id=?",
                       (status, reason[:200], time.time(), self.run_id))

    def public(self) -> dict:
        return _run_detail(self.operator, self.run_id)


def new_run(operator: str, automation_id: str, trigger: str = "manual",
            trigger_key: str = "", trigger_detail: Any = None) -> Run:
    return Run(operator, automation_id, trigger, trigger_key, trigger_detail)


# ---------------------------------------------------------------------------
# Validation


def _text(value, name: str, low: int = 1, high: int = 500) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ValueError(f"{name} must be {low}-{high} characters")
    return value.strip()


def _bounded(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be {low}-{high}")
    return value


def _accounts(config: dict) -> None:
    rotation = config.get("rotation_mode", "single")
    if rotation == "round_robin":
        ids = config.get("account_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 5
                or any(not isinstance(a, str) or not a.strip() for a in ids) or config.get("account_id")):
            raise ValueError("Rotation needs 1-5 account_ids and no account_id")
    elif not isinstance(config.get("account_id"), str) or not config["account_id"].strip():
        raise ValueError("Choose one signed-in account")


def _validate_config(action: str, config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("Each step needs a config object")
    if action in ("auto_join", "bulksend", "member_add", "member_transfer"):
        _accounts(config)
    if action == "run_workflow":
        import re
        if not re.fullmatch(r"wf_[A-Za-z0-9]{6,32}", str(config.get("workflow_id") or "")):
            raise ValueError("Choose a saved workflow")
        ids = config.get("account_ids") or []
        if not isinstance(ids, list) or not 1 <= len(ids) <= 5:
            raise ValueError("A workflow run needs 1-5 accounts")
    elif action == "auto_join":
        urls = config.get("urls")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 500:
            raise ValueError("Provide at least one channel or group link")
        _bounded(config.get("delay", 38), "Join delay", 35, 3600)
    elif action == "bulksend":
        targets = config.get("targets")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 2000:
            raise ValueError("Provide 1-2000 recipients")
        _bounded(config.get("delay", 0), "Send delay", 0, 3600)
        if not str(config.get("message") or "").strip() and not config.get("template_id"):
            raise ValueError("A message or template is required")
        if not config.get("consent_confirmed"):
            raise ValueError("Confirm recipient consent to send")
    elif action in ("member_add", "member_transfer"):
        if action == "member_add":
            users = config.get("usernames")
            if not isinstance(users, list) or not 1 <= len(users) <= 10:
                raise ValueError("Provide 1-10 recipients")
        else:
            _text(config.get("source_target"), "Source group", 1, 256)
            _bounded(config.get("limit", 10), "Transfer limit", 1, 10)
        _text(config.get("dest_target"), "Destination group", 1, 256)
        _bounded(config.get("delay", 35), "Add delay", 35, 3600)
        if not config.get("consent_confirmed"):
            raise ValueError("Confirm recipient consent to invite")
    elif action == "scrape":
        _text(config.get("target"), "Scrape target", 1, 256)
        _bounded(config.get("limit", 200), "Scrape limit", 1, 1000)
        ids = config.get("account_ids") or []
        if not isinstance(ids, list) or len(ids) != 1:
            raise ValueError("A scrape needs exactly one account")
    elif action == "notify":
        _text(config.get("text"), "Notification text", 1, 1000)
    elif action == "webhook":
        if not config.get("integration_id"):
            _text(config.get("url"), "Webhook URL", 8, 500)
    elif action == "mcp":
        _text(config.get("integration_id"), "MCP integration", 1, 60)
        _text(config.get("tool"), "MCP tool", 1, 120)
        if config.get("arguments") is not None and not isinstance(config.get("arguments"), dict):
            raise ValueError("MCP arguments must be an object")
    elif action == "ai":
        _text(config.get("prompt"), "AI prompt", 1, 8000)
    elif action == "wait":
        _bounded(config.get("seconds", 5), "Wait seconds", 0, 600)
    elif action != "forward":
        raise ValueError("Unsupported action")


def _validate_trigger(trigger: dict) -> dict:
    if not isinstance(trigger, dict):
        raise ValueError("A trigger is required")
    kind = trigger.get("type")
    if kind not in TRIGGER_TYPES:
        raise ValueError("Trigger must be manual, schedule or event")
    if kind == "schedule":
        cron = str(trigger.get("cron") or "").strip()
        try:
            croniter(cron)
        except (ValueError, KeyError):
            raise ValueError("Cron must be a valid 5-field expression") from None
        return {"type": "schedule", "cron": cron}
    if kind == "event":
        event = trigger.get("event")
        if event not in JOB_EVENTS:
            raise ValueError("Choose a job event to listen for")
        job_kind = trigger.get("kind") or ""
        return {"type": "event", "event": event, "kind": str(job_kind)[:40]}
    return {"type": "manual"}


class StepPayload(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    config: dict = Field(default_factory=dict)


class AutomationPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    enabled: bool = True
    trigger: dict = Field(default_factory=lambda: {"type": "manual"})
    steps: list[StepPayload] = Field(default_factory=list, max_length=MAX_STEPS)


def validate_automation(payload: dict) -> dict:
    name = _text(payload.get("name"), "Name", 1, 80)
    trigger = _validate_trigger(payload.get("trigger") or {"type": "manual"})
    steps = payload.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"An automation needs 1-{MAX_STEPS} steps")
    clean = []
    for step in steps:
        action = str((step or {}).get("action") or "")
        config = (step or {}).get("config") or {}
        _validate_config(action, config)
        clean.append({"action": action, "config": config})
    return {
        "name": name,
        "description": str(payload.get("description") or "")[:300],
        "enabled": bool(payload.get("enabled", True)),
        "trigger": trigger,
        "steps": clean,
    }


# ---------------------------------------------------------------------------
# Action handlers
#
# Dispatch handlers return {"job_id": ...} and stop there; the job queue owns
# delivery. Inline handlers return small JSON-safe results.


async def _enqueue(kind: str, operator: str, body, targets: list, accounts: list) -> dict:
    import outreach_jobs

    snapshot = await outreach_jobs.enqueue(kind, operator, body, targets, accounts)
    return {"job_id": snapshot["id"], "state": snapshot["state"], "total": snapshot.get("total", len(targets))}


async def _do_workflow(operator: str, config: dict) -> dict:
    import workflows as workflows_module

    workflow = next((w for w in (_workflows(operator))
                     if w["id"] == config["workflow_id"]), None)
    if workflow is None:
        raise RuntimeError("workflow_missing")
    run = workflows_module.start_run(workflow, config["account_ids"], operator=operator)
    return {"workflow_run_id": run["id"], "workflow_id": workflow["id"]}


def _workflows(operator: str) -> list:
    import workflows as workflows_module

    return workflows_module._record(operator)


async def _do_auto_join(operator: str, config: dict) -> dict:
    from outreach_jobs import AutoJoinJobRequest

    body = AutoJoinJobRequest(
        request_id=config["request_id"],
        urls=config["urls"],
        delay=config.get("delay", 38),
        limit=config.get("limit", 0),
        rotation_mode=config.get("rotation_mode", "single"),
        account_ids=config.get("account_ids", []),
        account_id=config.get("account_id", ""),
        group_type=config.get("group_type", "all"),
    )
    return await _enqueue("auto_join", operator, body, list(body.urls), body.selected_accounts())


async def _do_bulksend(operator: str, config: dict) -> dict:
    from outreach_jobs import BulkJobRequest

    body = BulkJobRequest(
        request_id=config["request_id"],
        targets=config["targets"],
        message=config.get("message", ""),
        template_id=config.get("template_id"),
        delay=config.get("delay", 0),
        rotation_mode=config.get("rotation_mode", "single"),
        account_ids=config.get("account_ids", []),
        account_id=config.get("account_id", ""),
        consent_confirmed=True,
    )
    targets = [t.strip().lower().removeprefix("@") for t in body.targets if t.strip()]
    return await _enqueue("personal_send", operator, body, targets, body.selected_accounts())


async def _do_member_add(operator: str, config: dict) -> dict:
    from outreach_jobs import MemberJobRequest

    body = MemberJobRequest(
        request_id=config["request_id"],
        dest_target=config["dest_target"],
        usernames=config["usernames"],
        filter_bots=config.get("filter_bots", True),
        limit=min(len(config["usernames"]), 10),
        delay=config.get("delay", 35),
        rotation_mode=config.get("rotation_mode", "single"),
        account_ids=config.get("account_ids", []),
        account_id=config.get("account_id", ""),
        consent_confirmed=True,
    )
    targets = [t.strip().lower().removeprefix("@") for t in body.usernames]
    return await _enqueue("member_add", operator, body, targets, body.selected_accounts())


async def _do_member_transfer(operator: str, config: dict) -> dict:
    import outreach_jobs

    return await outreach_jobs.enqueue_transfer(operator, config)


async def _do_forward(operator: str, config: dict) -> dict:
    import auto_forward

    result = await auto_forward.poll_operator(operator)
    return result


async def _do_scrape(operator: str, config: dict) -> dict:
    from telegram_ops import collect_scrape_members, matches_activity

    import account_manager

    client = await account_manager.account_manager.get_client(config["account_ids"][0])
    if not client:
        raise RuntimeError("account_unavailable")
    entity = await client.get_entity(config["target"])
    participants, source, member_count = await collect_scrape_members(client, entity, config.get("limit", 200))
    members = [{"id": p.id, "username": p.username or "", "name": " ".join(
        part for part in (p.first_name, p.last_name) if part)[:80]}
        for p in participants if not p.bot and matches_activity(p, config.get("activity_filter", "all"))]
    return {"count": len(members), "source": source, "member_count": member_count,
            "members": members[:200]}


def _integration(integration_id: str) -> dict:
    import dashboard_features

    item = next((entry for entry in dashboard_features._integrations()
                 if entry.get("id") == integration_id), None)
    if item is None:
        raise RuntimeError("integration_missing")
    return item


def _integration_token(integration_id: str) -> str:
    from open_teleset.operator_store import read_secret as read

    token = (read("integration_tokens", {}) or {}).get(integration_id)
    if isinstance(token, str) and token:
        return token
    import dashboard_features

    entry = dashboard_features._oauth_entry(integration_id)
    return str((entry.get("tokens") or {}).get("access_token") or "")


async def _do_webhook(operator: str, config: dict) -> dict:
    import webhooks

    url = config.get("url") or ""
    headers = {"Content-Type": "application/json", "User-Agent": "open-teleset-automation/1"}
    if config.get("integration_id"):
        item = _integration(config["integration_id"])
        url = str(item.get("base_url") or "")
        token = _integration_token(config["integration_id"])
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        url = webhooks._validate_url(url)
        webhooks._assert_public_target(url)
    except ValueError as exc:
        raise RuntimeError(f"unsafe_webhook_target: {exc}") from None
    payload = {"source": "open-teleset", "automation_run": config["run_id"],
               "event": config.get("event") or "automation.step", "data": config.get("data") or {}}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"webhook_http_{response.status_code}")
    return {"status": response.status_code}


async def _do_mcp(operator: str, config: dict) -> dict:
    item = _integration(config["integration_id"])
    url = str(item.get("base_url") or "")
    token = _integration_token(config["integration_id"])
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "User-Agent": "open-teleset-automation/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": "automation", "method": "tools/call",
            "params": {"name": config["tool"], "arguments": config.get("arguments") or {}}}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(url, json=body, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"mcp_http_{response.status_code}")
    text = response.text[:200000]
    payload = _jsonrpc_payload(text)
    if payload is None:
        raise RuntimeError("mcp_unexpected_response")
    if payload.get("error"):
        raise RuntimeError("mcp_tool_error")
    result = payload.get("result") or {}
    content = result.get("content") if isinstance(result, dict) else None
    text_out = ""
    if isinstance(content, list):
        text_out = "\n".join(str(part.get("text") or "") for part in content
                             if isinstance(part, dict))[:DETAIL_LIMIT]
    return {"tool": config["tool"], "is_error": bool(result.get("isError") if isinstance(result, dict) else False),
            "result": text_out or result}


def _jsonrpc_payload(text: str):
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass
    # Streamable HTTP answers with server-sent events; the last data line wins.
    for line in reversed(text.splitlines()):
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


async def _do_ai(operator: str, config: dict) -> dict:
    from dashboard_features import _ai_chat, _ai_config, _ai_keys, _resolve_ai

    saved = _ai_config(operator)
    provider = saved.get("provider", "open_connect")
    key = (_ai_keys(operator) or {}).get(provider)
    if not key:
        raise RuntimeError("ai_provider_not_configured")
    base_url, model = _resolve_ai(provider, saved.get("base_url", ""), saved.get("model", ""))
    messages = []
    if config.get("system"):
        messages.append({"role": "system", "content": str(config["system"])[:4000]})
    messages.append({"role": "user", "content": config["prompt"]})
    answer = await _ai_chat(base_url, key, model, messages, max_tokens=config.get("max_tokens", 800))
    return {"model": model, "answer": answer[:DETAIL_LIMIT]}


async def _do_notify(operator: str, config: dict) -> dict:
    queue_event(operator, "automation.step", config["text"])
    return {"queued": True}


async def _do_wait(operator: str, config: dict) -> dict:
    seconds = config.get("seconds", 5)
    await asyncio.sleep(seconds)
    return {"waited": seconds}


HANDLERS = {
    "run_workflow": _do_workflow,
    "auto_join": _do_auto_join,
    "bulksend": _do_bulksend,
    "member_add": _do_member_add,
    "member_transfer": _do_member_transfer,
    "forward": _do_forward,
    "scrape": _do_scrape,
    "notify": _do_notify,
    "webhook": _do_webhook,
    "mcp": _do_mcp,
    "ai": _do_ai,
    "wait": _do_wait,
}

DISPATCH_ACTIONS = frozenset({"run_workflow", "auto_join", "bulksend", "member_add",
                              "member_transfer"})


async def execute_automation(operator: str, automation: dict, trigger: str = "manual",
                             trigger_key: str = "", trigger_detail: Any = None) -> dict:
    run = new_run(operator, automation.get("id") or "external", trigger, trigger_key, trigger_detail)
    statuses = []
    for seq, step in enumerate(automation.get("steps") or []):
        action = step.get("action")
        config = dict(step.get("config") or {})
        handler = HANDLERS.get(action)
        run.step(seq, action or "unknown")
        if handler is None:
            statuses.append("failed")
            run.result(seq, "failed", {"error": "unknown_action"})
            run.finish("failed", "unknown_action")
            return run.public()
        config["request_id"] = f"{run.run_id}-{seq}"
        config["run_id"] = run.run_id
        try:
            outcome = await handler(operator, config)
        except HTTPException as exc:
            statuses.append("failed")
            run.result(seq, "failed", {"error": str(exc.detail)[:300]})
            run.finish("failed", "step_failed")
            log_manager.add_log("Automations", run.run_id, f"{action} failed", "error")
            return run.public()
        except asyncio.CancelledError:
            run.result(seq, "cancelled", None)
            run.finish("stopped", "cancelled")
            raise
        except Exception as exc:
            statuses.append("failed")
            # Never echo exception text: it can carry provider payloads.
            run.result(seq, "failed", {"error": type(exc).__name__})
            run.finish("failed", "step_failed")
            log_manager.add_log("Automations", run.run_id, f"{action} failed ({type(exc).__name__})", "error")
            return run.public()
        statuses.append("dispatched" if action in DISPATCH_ACTIONS else "completed")
        run.result(seq, statuses[-1], outcome)

    final = "dispatched" if any(s == "dispatched" for s in statuses) else "completed"
    run.finish(final)
    log_manager.add_log("Automations", automation.get("id") or "", f"Run {run.run_id} {final}", "success")
    return run.public()


async def run_automation(operator: str, automation_id: str, trigger: str = "manual",
                         trigger_key: str = "", trigger_detail: Any = None) -> dict:
    automation = _find(operator, automation_id)
    if not automation.get("enabled", True) and trigger != "manual":
        raise HTTPException(409, "Automation is disabled")
    return await execute_automation(operator, automation, trigger, trigger_key, trigger_detail)


def handle_job_terminal(operator: str, kind: str, snapshot: dict) -> None:
    """Fan a terminal job out to enabled event automations (best effort)."""
    if not operator:
        return
    status = str(snapshot.get("state") or "completed")
    job_id = str(snapshot.get("id") or "")
    if not job_id:
        return
    try:
        automations = _record(operator)["automations"]
    except HTTPException:
        return
    for automation in automations:
        trigger = automation.get("trigger") or {}
        if trigger.get("type") != "event" or not automation.get("enabled", True):
            continue
        event = trigger.get("event") or "job.terminal"
        if event != "job.terminal" and event != f"job.{status}":
            continue
        if trigger.get("kind") and trigger["kind"] != kind:
            continue
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            continue
        key = f"{job_id}:{automation['id']}"
        loop.create_task(_event_run(operator, automation["id"], key,
                                    {"job_id": job_id, "kind": kind, "state": status}))


async def _event_run(operator: str, automation_id: str, key: str, detail: dict) -> None:
    try:
        await run_automation(operator, automation_id, trigger="event", trigger_key=key,
                             trigger_detail=detail)
    except Exception as exc:
        log_manager.add_log("Automations", automation_id,
                            f"event run failed ({type(exc).__name__})", "error")


# ---------------------------------------------------------------------------
# Schedule sync


def _schedule_id(automation_id: str) -> str:
    return f"auto_{automation_id}"


def _sync_schedule(operator: str, automation: dict) -> None:
    """One clock: a scheduled automation becomes a TaskScheduler entry."""
    from scheduler import task_scheduler

    schedule_id = _schedule_id(automation["id"])
    existing = next((s for s in task_scheduler.list_schedules()
                     if s.get("id") == schedule_id), None)
    if existing:
        task_scheduler.remove_schedule(schedule_id)
    trigger = automation.get("trigger") or {}
    if trigger.get("type") != "schedule" or not automation.get("enabled", True):
        return
    task_scheduler.add_schedule(
        schedule_id=schedule_id,
        name=f"Automation: {automation['name']}",
        cron=trigger["cron"],
        action="run_automation",
        target="",
        enabled=True,
        automation_id=automation["id"],
        operator_id=operator,
    )


def _drop_schedule(automation_id: str) -> None:
    from scheduler import task_scheduler

    task_scheduler.remove_schedule(_schedule_id(automation_id))


# ---------------------------------------------------------------------------
# Catalog and API


def _catalog() -> list:
    return [
        {"id": "run_workflow", "label": "Run a saved workflow", "dispatch": True,
         "fields": ["workflow_id", "account_ids"]},
        {"id": "auto_join", "label": "Auto Join channels/groups", "dispatch": True,
         "fields": ["urls", "delay", "account_id"]},
        {"id": "bulksend", "label": "Bulk send a message", "dispatch": True,
         "fields": ["targets", "message", "delay", "account_id"]},
        {"id": "member_add", "label": "Invite members", "dispatch": True,
         "fields": ["dest_target", "usernames", "delay", "account_id"]},
        {"id": "member_transfer", "label": "Transfer members from a source group", "dispatch": True,
         "fields": ["source_target", "dest_target", "limit", "delay", "account_id"]},
        {"id": "forward", "label": "Run Auto Forwarder now", "dispatch": False, "fields": []},
        {"id": "scrape", "label": "Scrape members", "dispatch": False,
         "fields": ["target", "limit", "account_ids"]},
        {"id": "notify", "label": "Notify the operator", "dispatch": False, "fields": ["text"]},
        {"id": "webhook", "label": "Call a webhook / integration", "dispatch": False,
         "fields": ["url", "integration_id", "event", "data"]},
        {"id": "mcp", "label": "Call an MCP tool", "dispatch": False,
         "fields": ["integration_id", "tool", "arguments"]},
        {"id": "ai", "label": "Ask the configured AI provider", "dispatch": False,
         "fields": ["prompt", "system"]},
        {"id": "wait", "label": "Wait", "dispatch": False, "fields": ["seconds"]},
    ]


def _public(automation: dict) -> dict:
    info = {**automation}
    try:
        from scheduler import task_scheduler

        entry = next((s for s in task_scheduler.list_schedules()
                      if s.get("id") == _schedule_id(automation["id"])), None)
        info["next_run"] = entry.get("next_run") if entry else None
    except Exception:
        info["next_run"] = None
    return info


@router.get("/api/automations")
async def list_automations(request: Request):
    operator = _operator(request)
    automations = [_public(a) for a in _record(operator)["automations"]]
    return {
        "automations": automations,
        "catalog": _catalog(),
        "enabled": feature_enabled("automation"),
        "recent_runs": _run_rows(operator, limit=10),
    }


@router.get("/api/automations/catalog")
async def automation_catalog():
    return {"catalog": _catalog()}


@router.post("/api/automations", status_code=201)
async def create_automation(body: AutomationPayload, request: Request):
    operator = _operator(request)
    if not feature_enabled("automation"):
        raise HTTPException(403, "Automations are turned off. Turn them on in Automation settings.")
    try:
        clean = validate_automation(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    automations = _record(operator)["automations"]
    if len(automations) >= MAX_AUTOMATIONS:
        raise HTTPException(400, f"At most {MAX_AUTOMATIONS} automations")
    now = _now_iso()
    entry = {"id": _new_id(), **clean, "created": now, "updated": now}
    automations.insert(0, entry)
    _save(operator, automations)
    _sync_schedule(operator, entry)
    return {"automation": _public(entry)}


@router.put("/api/automations/{automation_id}")
async def update_automation(automation_id: str, body: AutomationPayload, request: Request):
    operator = _operator(request)
    automations = _record(operator)["automations"]
    existing = _find(operator, automation_id)
    try:
        clean = validate_automation(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    updated = {**existing, **clean, "updated": _now_iso()}
    automations[automations.index(existing)] = updated
    _save(operator, automations)
    _sync_schedule(operator, updated)
    return {"automation": _public(updated)}


@router.delete("/api/automations/{automation_id}")
async def delete_automation(automation_id: str, request: Request):
    operator = _operator(request)
    automations = _record(operator)["automations"]
    existing = _find(operator, automation_id)
    automations.remove(existing)
    _save(operator, automations)
    _drop_schedule(automation_id)
    return {"success": True}


class RunPayload(BaseModel):
    dry_run: bool = False


@router.post("/api/automations/{automation_id}/run", status_code=202)
async def trigger_automation(automation_id: str, request: Request, body: RunPayload | None = None):
    operator = _operator(request)
    if not feature_enabled("automation"):
        raise HTTPException(403, "Automations are turned off. Turn them on in Automation settings.")
    automation = _find(operator, automation_id)
    if body and body.dry_run:
        return {"dry_run": True, "automation": _public(automation)}
    return {"run": await execute_automation(operator, automation, trigger="manual")}


@router.get("/api/automations/{automation_id}/runs")
async def automation_runs(automation_id: str, request: Request):
    operator = _operator(request)
    _find(operator, automation_id)
    return {"runs": _run_rows(operator, automation_id, limit=RUNS_LIMIT)}


@router.get("/api/automation-runs/{run_id}")
async def run_detail(run_id: str, request: Request):
    return _run_detail(_operator(request), run_id)
