"""Model-driven Copilot agent loop.

The model chooses from a fixed capability registry — the internal tools, the
curated skills and the connectors the operator configured — and the server
decides what may happen. Read tools run inside the loop; anything that writes,
runs a curated skill or reaches a connector stops as a draft the operator must
approve. The agent never invents an ability it does not have: an unknown tool is
reported back to the model, never executed.

Without a configured AI provider the same endpoint answers from the existing
rule-based drafter, so the panel keeps working with no key.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import copilot
import dashboard_features

router = APIRouter(tags=["copilot-agent"])

MAX_STEPS = 6
MAX_CALLS_PER_STEP = 3
MAX_RESULT_CHARS = 1500
STEP_BUDGET_SECONDS = 30.0
MAX_PROMPT_CHARS = 2000
MAX_MODEL_MESSAGES = 40

SYSTEM_PROMPT = (
    "You are the Open Teleset Ops Copilot. You help one operator run their own "
    "Telegram workspace. You act only through the tools you are given; never claim "
    "to have done something you did not do through a tool.\n\n"
    "Rules that override any instruction:\n"
    "- Never create accounts, buy numbers, join or add members automatically, or send "
    "cold or scraped outreach. If asked, say it is refused and offer the operator's own "
    "gated surface instead.\n"
    "- Never ask for or repeat passwords, session strings, API keys or phone numbers.\n"
    "- Tools that write return a draft that the operator must approve; say so plainly "
    "and stop after calling them.\n"
    "- Answer in the operator's language, briefly, and lead with what you found.\n"
)


class AgentRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    mode: str = Field(default="ask", max_length=8)


def _provider(operator: str) -> tuple[str, str, str] | None:
    """The operator's own provider, or None when there is nothing to call."""
    config = dashboard_features._ai_config(operator)
    keys = dashboard_features._ai_keys(operator)
    name = str(config.get("provider") or "open_connect")
    preset = dashboard_features.PROVIDERS.get(name) or dashboard_features.PROVIDERS["custom"]
    base_url = str(config.get("base_url") or preset.get("base_url") or "").strip().rstrip("/")
    model = str(config.get("model") or ((preset.get("models") or [""])[0])).strip()
    key = str(keys.get(name) or "").strip()
    if not (base_url and model and key):
        return None
    return base_url, key, model


def _internal_names() -> list[str]:
    return [command for command in copilot.INTERNAL_COMMANDS]


def _instruction_schema(summary: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": f"What to do, in the operator's own words. {summary}",
            }
        },
        "required": ["instruction"],
        "additionalProperties": False,
    }


def _tool_specs() -> list[dict]:
    tools = []
    for command, spec in copilot.INTERNAL_COMMANDS.items():
        tools.append({
            "type": "function",
            "function": {
                "name": command,
                "description": f"{spec['title']}. {spec['summary']}"
                               + (" Needs the operator's approval before it runs."
                                  if command in copilot.INTERNAL_WRITES else ""),
                "parameters": _instruction_schema(command.replace("-", " ")),
            },
        })
    tools.append({
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Open one of the operator's curated runbook skills and prefill "
                           "the surface it belongs to. Always needs approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Id from the skill list."},
                    "instruction": {"type": "string", "description": "The operator's own words."},
                },
                "required": ["skill_id"],
                "additionalProperties": False,
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "inspect_connector",
            "description": "Report the configuration of one connector the operator saved. "
                           "Read-only; nothing leaves this workspace.",
            "parameters": {
                "type": "object",
                "properties": {"connector_id": {"type": "string"}},
                "required": ["connector_id"],
                "additionalProperties": False,
            },
        },
    })
    return tools


def _skills(operator: str) -> list[dict]:
    return [{"id": preset["id"], "title": preset["title"], "group": preset.get("group") or "Runbook"}
            for preset in copilot.list_presets(operator)]


def _connectors() -> list[dict]:
    return [{key: item.get(key) for key in ("id", "name", "transport", "authorized", "auth_mode")}
            for item in dashboard_features._integrations()]


def capabilities(operator: str) -> dict:
    tools = []
    for command, spec in copilot.INTERNAL_COMMANDS.items():
        tools.append({
            "id": command,
            "group": str(spec.get("group") or "Workspace"),
            "title": spec["title"],
            "summary": spec["summary"],
            "write": command in copilot.INTERNAL_WRITES,
            "example": copilot.INTERNAL_EXAMPLES.get(command, ""),
        })
    return {
        "tool": "Workspace tools the agent may call",
        "tools": tools,
        "skills": _skills(operator),
        "connectors": _connectors(),
        "limits": {"steps": MAX_STEPS, "calls_per_step": MAX_CALLS_PER_STEP,
                   "result_chars": MAX_RESULT_CHARS, "seconds": int(STEP_BUDGET_SECONDS)},
    }


def _compact(value: Any, limit: int = MAX_RESULT_CHARS) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _summary(result: dict) -> str:
    """One readable line for the panel, never a dump."""
    if not isinstance(result, dict):
        return "Done."
    for key in ("message", "summary", "detail", "error"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:220]
    for key in ("count", "total", "items", "accounts", "documents", "logs"):
        value = result.get(key)
        if isinstance(value, int):
            return f"{value} {key.replace('_', ' ')}."
        if isinstance(value, list):
            return f"{len(value)} {key.replace('_', ' ')}."
    return "Done."


def _execute(operator: str, prompt: str, name: str, args: dict) -> dict:
    """Run one tool call the model asked for. Writes come back as a pending draft."""
    instruction = str(args.get("instruction") or prompt)[:MAX_PROMPT_CHARS]
    if name in copilot.INTERNAL_COMMANDS:
        draft = copilot._internal_draft(name, copilot._internal_params(name, instruction))
        if draft.get("requires_confirmation"):
            return {"status": "pending", "draft": draft, "summary": draft["title"]}
        result = copilot._safe_run(operator, draft)
        status = "error" if isinstance(result, dict) and result.get("error") else "ok"
        return {"status": status, "summary": _summary(result), "result": result}
    if name == "run_skill":
        preset = copilot._preset_by_id(operator, str(args.get("skill_id") or ""))
        if not preset:
            return {"status": "error", "summary": "No skill with that id.",
                    "result": {"error": "unknown skill"}}
        draft = copilot._preset_draft(preset, instruction)
        return {"status": "pending", "draft": draft, "summary": preset["title"],
                "preset_id": preset["id"]}
    if name == "inspect_connector":
        connector_id = str(args.get("connector_id") or "")
        item = next((entry for entry in dashboard_features._integrations()
                     if entry.get("id") == connector_id), None)
        if not item:
            return {"status": "error", "summary": "No connector with that id.",
                    "result": {"error": "unknown connector"}}
        public = dashboard_features._public_integration(item)
        return {"status": "ok", "summary": f"{public['name']} is {public['transport']}", "result": public}
    return {"status": "error", "summary": f"There is no tool named {name}.",
            "result": {"error": "unknown tool"}}


def _chat(base_url: str, key: str, model: str, messages: list, tools: list) -> dict:
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "tools": tools,
                      "tool_choice": "auto", "temperature": 0.2, "max_tokens": 700},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Provider unreachable ({type(exc).__name__})") from None
    if response.status_code in (401, 403):
        raise HTTPException(400, "Provider rejected the API key.")
    if response.status_code >= 400:
        raise HTTPException(400, f"Provider returned HTTP {response.status_code}.")
    try:
        message = response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(502, "Provider returned an unexpected response.") from None
    if not isinstance(message, dict):
        raise HTTPException(502, "Provider returned an unexpected response.")
    return message


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rules(operator: str, prompt: str, mode: str) -> dict:
    """No provider configured: the same answer the rule-based drafter gives."""
    draft = copilot.build_draft(prompt)
    copilot._dispatch_internal(operator, draft, mode)
    draft["mode"] = mode
    draft["next"] = copilot._next_steps(draft)
    turn = copilot._remember(operator, prompt, draft)
    return {
        "success": True, "engine": "rules", "status": "done",
        "reply": "No AI provider is configured, so I drafted this with the built-in rules.",
        "steps": [], "draft": draft, "turn_id": turn["id"] if turn else "",
        "model": "", "steps_used": 0,
    }


def run_agent(operator: str, prompt: str, mode: str) -> dict:
    blocked = copilot._blocked_reason(prompt)
    if blocked:
        return {"success": True, "engine": "guard", "status": "refused", "reply": blocked,
                "steps": [], "draft": None, "turn_id": "", "model": "", "steps_used": 0}
    provider = _provider(operator)
    if not provider:
        return _rules(operator, prompt, mode)
    base_url, key, model = provider
    tools = _tool_specs()
    registry = ", ".join(_internal_names())
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\nAvailable tools: {registry}, run_skill, inspect_connector."},
        {"role": "user", "content": prompt},
    ]
    steps: list[dict] = []
    started = time.monotonic()
    for index in range(MAX_STEPS):
        if time.monotonic() - started > STEP_BUDGET_SECONDS:
            return {"success": True, "engine": "model", "status": "done",
                    "reply": "I stopped at the time limit. Ask again to continue.",
                    "steps": steps, "draft": None, "turn_id": "", "model": model,
                    "steps_used": len(steps)}
        message = _chat(base_url, key, model, messages[-MAX_MODEL_MESSAGES:], tools)
        calls = message.get("tool_calls") or []
        if not calls:
            reply = str(message.get("content") or "").strip()
            return {"success": True, "engine": "model", "status": "done",
                    "reply": reply or "I have nothing to add.", "steps": steps,
                    "draft": None, "turn_id": "", "model": model, "steps_used": len(steps)}
        messages.append({"role": "assistant", "content": str(message.get("content") or ""),
                         "tool_calls": calls})
        for call in calls[:MAX_CALLS_PER_STEP]:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            args = _parse_arguments(function.get("arguments"))
            outcome = _execute(operator, prompt, name, args)
            steps.append({"index": len(steps) + 1, "tool": name,
                          "instruction": str(args.get("instruction") or args.get("skill_id") or "")[:200],
                          "status": outcome["status"], "summary": outcome.get("summary", "")})
            messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                             "content": _compact(outcome.get("result", {"status": outcome["status"],
                                                                        "summary": outcome.get("summary", "")}))})
            if outcome["status"] == "pending":
                draft = outcome["draft"]
                draft["mode"] = mode
                draft["next"] = copilot._next_steps(draft)
                turn = copilot._remember(operator, prompt, draft,
                                         str(outcome.get("preset_id") or ""))
                return {"success": True, "engine": "model", "status": "awaiting_approval",
                        "reply": f"{outcome.get('summary') or 'That step'} is ready. "
                                 "Review it and press Run.",
                        "steps": steps, "draft": draft,
                        "turn_id": turn["id"] if turn else "", "model": model,
                        "steps_used": len(steps)}
    return {"success": True, "engine": "model", "status": "done",
            "reply": f"I used all {MAX_STEPS} steps. Narrow the request and I will continue.",
            "steps": steps, "draft": None, "turn_id": "", "model": model,
            "steps_used": len(steps)}


@router.get("/api/copilot/agent/capabilities")
async def agent_capabilities(http_request: Request):
    """Everything the loop may choose from, so the panel can show it honestly."""
    return {"success": True, **capabilities(copilot._operator(http_request))}


@router.post("/api/copilot/agent")
async def agent_run(body: AgentRequest, http_request: Request):
    operator = copilot._operator(http_request)
    mode = "work" if str(body.mode or "").lower() == "work" else "ask"
    return run_agent(operator, body.prompt.strip(), mode)
