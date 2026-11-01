"""Browser agent: a plan the operator approves, one step at a time.

The AI provider never drives the hosted browser. It is shown the page's own list of
fields and controls and answers with an ordered plan; the panel shows every step and
runs one only when the operator presses that step's button.

Two rules make that more than a warning label. A type step carries no value from the
provider - the operator types it when the step runs - so a credential never reaches a
model or a stored plan. And a click or type step is re-located on the live page at
approval time rather than aimed at coordinates captured when the plan was written, so
a page that has moved since the proposal refuses the step instead of clicking
something else.
"""

from __future__ import annotations

import json
import re
import time
from itertools import count
from uuid import uuid4

from fastapi import HTTPException

MAX_STEPS = 12
MAX_ELEMENTS = 60
PLAN_TTL_SECONDS = 900
MAX_PLANS = 4
WHY_MAX = 160
LABEL_MAX = 70
ACTIONS = ("open", "click", "type", "key")

UNUSABLE_PLAN = "The provider did not return a usable plan."
NO_STEPS = "The provider proposed no steps."
TOO_MANY_STEPS = f"A plan is limited to {MAX_STEPS} steps."
UNKNOWN_ELEMENT = "That plan points at a field the page does not have."
UNKNOWN_ACTION = "That plan asks for something the browser cannot do from here."
GONE = "That plan is no longer available."
NO_PROVIDER = "Configure an AI provider and API key in Settings → AI providers."

# Read-only: it measures the page and reports what it finds. Nothing here clicks,
# types or navigates, and no field's *value* is ever read - a login form's contents
# stay in the page.
ELEMENTS_JS = """(() => {
  const SEL = 'a,button,input:not([type=hidden]),select,textarea,[role="button"],[contenteditable="true"]';
  const vw = window.innerWidth || 1;
  const vh = window.innerHeight || 1;
  const items = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    if (items.length >= MAX_LIMIT) break;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('name') || el.getAttribute('id') || '';
    const label = (
      el.getAttribute('aria-label') ||
      (el.textContent || '').trim() ||
      (el.labels && el.labels[0] ? el.labels[0].textContent : '') ||
      el.getAttribute('placeholder') || name || type || tag
    ).replace(/\\s+/g, ' ').trim().slice(0, LABEL_LIMIT);
    const key = [tag, type, name, label].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({
      tag, type, name, label,
      x: (r.left + r.width / 2) / vw,
      y: (r.top + r.height / 2) / vh,
    });
  }
  return JSON.stringify(items);
})()""".replace("MAX_LIMIT", str(MAX_ELEMENTS)).replace("LABEL_LIMIT", str(LABEL_MAX))

SYSTEM_PROMPT = (
    "You draft step-by-step plans for an operator who drives their own browser through "
    "Chrome DevTools. Reply with JSON only: an array of steps, in the order they should "
    "run. Each step is exactly one of "
    '{"action":"open","url":"https://…"}, '
    '{"action":"click","element":N}, '
    '{"action":"type","element":N}, '
    '{"action":"key","key":"Enter"}. '
    "N must be a number from the list you are given; never invent one. Never include "
    "text to type - the operator supplies it when the step runs. Use only the keys "
    "Enter, Tab, Escape, Backspace, Delete, Space, Home, End, PageUp, PageDown, "
    "ArrowLeft, ArrowUp, ArrowRight, ArrowDown. Give every step a short \"why\". "
    "Stop as soon as the page needs the operator's judgement, and never plan a step "
    "that sends a message or pays for anything without saying so in its why."
)


_ORDER = count()


class Plan:
    def __init__(self, plan_id: str, operator: str, target_id: str, goal: str, page: dict, steps: list) -> None:
        self.id = plan_id
        self.operator = operator
        self.target_id = target_id
        self.goal = goal
        self.page = page
        self.steps = steps
        self.created = time.monotonic()
        # monotonic can tie inside a fast loop, so age order needs its own clock.
        self.seq = next(_ORDER)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > PLAN_TTL_SECONDS

    @property
    def done(self) -> bool:
        return bool(self.steps) and all(step["status"] != "todo" for step in self.steps)


_plans: dict[str, Plan] = {}


def _prune(operator: str) -> None:
    """Expired first, then the oldest beyond the cap - a plan is a working note."""
    for plan_id in [pid for pid, plan in _plans.items() if plan.expired]:
        _plans.pop(plan_id, None)
    mine = sorted((plan for plan in _plans.values() if plan.operator == operator), key=lambda plan: plan.seq)
    for plan in mine[:max(0, len(mine) - (MAX_PLANS - 1))]:
        _plans.pop(plan.id, None)


def new_plan(operator: str, target_id: str, goal: str, page: dict, steps: list) -> Plan:
    _prune(operator)
    plan = Plan(uuid4().hex, operator, target_id, goal, page, steps)
    _plans[plan.id] = plan
    return plan


def get_plan(operator: str, plan_id: str) -> Plan:
    plan = _plans.get(plan_id)
    if plan is None or plan.operator != operator or plan.expired:
        raise HTTPException(404, GONE)
    return plan


def forget_operator(operator: str) -> None:
    """The browser these plans were drafted against is gone, so the plans are too."""
    for plan_id in [pid for pid, plan in _plans.items() if plan.operator == operator]:
        _plans.pop(plan_id, None)


def forget(plan: Plan) -> None:
    _plans.pop(plan.id, None)


def reset() -> None:
    _plans.clear()


def next_index(plan: Plan) -> int | None:
    for step in plan.steps:
        if step["status"] == "todo":
            return step["index"]
    return None


def public(plan: Plan) -> dict:
    return {
        "id": plan.id,
        "goal": plan.goal,
        "page": plan.page,
        "target_id": plan.target_id,
        "done": plan.done,
        "seconds_left": max(0, int(PLAN_TTL_SECONDS - (time.monotonic() - plan.created))),
        "steps": [
            {
                "index": step["index"],
                "action": step["action"],
                "label": step["label"],
                "why": step["why"],
                "status": step["status"],
                "result": step["result"],
                "needs_text": step["action"] == "type",
                "key": step.get("key", ""),
                "url": step.get("url", ""),
            }
            for step in plan.steps
        ],
    }


# --------------------------------------------------------------- the page's own words


def _text(value, cap: int = LABEL_MAX) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:cap]


def _fraction(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


NO_CONTROLS = "The page would not list its controls."


def parse_elements(raw: str) -> list:
    """The page's own answer as the numbered list the provider is shown."""
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        raise HTTPException(502, NO_CONTROLS) from None
    if not isinstance(items, list):
        raise HTTPException(502, NO_CONTROLS)
    elements = []
    for item in items:
        if not isinstance(item, dict):
            continue
        x, y = _fraction(item.get("x")), _fraction(item.get("y"))
        if x is None or y is None:
            continue
        elements.append({
            "index": len(elements) + 1,
            "tag": _text(item.get("tag"), 30).lower(),
            "type": _text(item.get("type"), 30).lower(),
            "name": _text(item.get("name"), 80),
            "label": _text(item.get("label")),
            "x": x,
            "y": y,
        })
    return elements


def describe(elements: list) -> str:
    if not elements:
        return "(the page reported no controls it can see)"
    return "\n".join(
        f'{element["index"]}. {element["tag"]}'
        + (f'[type={element["type"]}]' if element["type"] else "")
        + (f' name="{element["name"]}"' if element["name"] else "")
        + f' "{element["label"]}"'
        for element in elements
    )


def user_prompt(goal: str, page: dict, elements: list) -> str:
    return (
        f"Goal: {goal}\n"
        f"Page: {page.get('title') or '(untitled)'} — {page.get('url') or '(no address)'}\n"
        f"Numbered controls on the page:\n{describe(elements)}\n"
    )


def _reply_array(reply: str) -> list:
    text = (reply or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except ValueError:
        raise HTTPException(400, UNUSABLE_PLAN) from None
    if isinstance(payload, dict):
        payload = payload.get("steps")
    if not isinstance(payload, list):
        raise HTTPException(400, UNUSABLE_PLAN)
    return payload


def _element_index(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_steps(reply: str, elements: list, *, keys: list, url_ok) -> list:
    """The provider's JSON as validated steps, or a refusal that names nothing from it."""
    raw = _reply_array(reply)
    if not raw:
        raise HTTPException(400, NO_STEPS)
    if len(raw) > MAX_STEPS:
        raise HTTPException(400, TOO_MANY_STEPS)
    by_index = {element["index"]: element for element in elements}
    steps = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(400, UNUSABLE_PLAN)
        action = str(item.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise HTTPException(400, UNKNOWN_ACTION)
        step = {
            "index": len(steps),
            "action": action,
            "why": re.sub(r"\s+", " ", str(item.get("why") or "")).strip()[:WHY_MAX],
            "status": "todo",
            "result": "",
            "label": "",
        }
        if action in ("click", "type"):
            element = by_index.get(_element_index(item.get("element")))
            if element is None:
                raise HTTPException(400, UNKNOWN_ELEMENT)
            # The identity, not the position: the page is looked at again when the
            # operator approves, and the step runs where that control is now.
            step["want"] = {key: element[key] for key in ("tag", "type", "name", "label")}
            step["label"] = element["label"] or element["tag"]
        elif action == "key":
            key = str(item.get("key") or "").strip()
            if key not in keys:
                raise HTTPException(400, "That plan asks for a key the dashboard cannot send.")
            step["key"] = key
            step["label"] = key
        else:
            step["url"] = url_ok(str(item.get("url") or ""))
            step["label"] = step["url"]
        steps.append(step)
    return steps


def find_element(elements: list, want: dict) -> dict | None:
    """The same control in a fresh look at the page, or nothing."""
    named = want.get("name") or ""
    for element in elements:
        if element["tag"] != want["tag"] or element["type"] != want["type"]:
            continue
        if named:
            if element["name"] == named:
                return element
        elif element["name"] == want.get("name", "") and element["label"] == want["label"]:
            return element
    return None


async def ask_provider(operator: str, goal: str, page: dict, elements: list) -> str:
    """One completion, on the operator's own provider settings.

    The goal and the control labels leave the backend here, which is why the panel
    says not to write a password into the goal and why a type step carries no value.
    """
    from dashboard_features import PROVIDERS, _ai_chat, _ai_config, _ai_keys

    config = _ai_config(operator)
    provider = str(config.get("provider") or "")
    base_url = str(config.get("base_url") or PROVIDERS.get(provider, {}).get("base_url") or "").strip()
    model = str(config.get("model") or "").strip()
    key = (_ai_keys(operator).get(provider) or "").strip()
    if not (base_url and model and key):
        raise HTTPException(503, NO_PROVIDER)
    return await _ai_chat(
        base_url,
        key,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt(goal, page, elements)},
        ],
        max_tokens=900,
    )


def result_text(step: dict, characters: int = 0) -> str:
    if step["action"] == "type":
        # The text itself is a credential; only its length is worth reporting.
        return f"Typed {characters} characters"
    if step["action"] == "open":
        return f"Opened {step['label']}"
    if step["action"] == "key":
        return f"Sent {step['key']}"
    return f"Clicked {step['label'] or 'that control'}"
