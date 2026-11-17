# Automations

The **Automations** tab chains the actions this workspace already offers — saved
workflows, Auto Join, BulkSend, the Adder, Auto Transfer, scraping, Auto
Forwarder, AI, MCP tools, webhooks and waits — into one ordered automation, and
runs it by hand, on a schedule, or when a job finishes. Everything runs inside
Open Teleset with your own accounts; n8n is not used and stays available as a
separate integration for other flows.

## Triggers

| Trigger | When it runs | Notes |
| --- | --- | --- |
| **Manual** | Only when you press **Run now**. | Always allowed, even while the automation is paused. |
| **On a schedule** | A 5-field cron expression, checked by the shared scheduler. | Hourly, daily, weekdays and weekly presets are offered; the next run is shown on the card. |
| **When a job finishes** | Every time a durable job reaches a terminal state, optionally limited to one job kind. | Events: any terminal result, completed, failed or stopped. Kinds: BulkSend, Adder, Auto Transfer, Auto Join. |

A paused automation keeps its steps but does not run from a schedule or a job
event. **Run now** still works while paused.

## Steps

Steps run in the order they are listed. Each step is written to the run ledger
before it starts, so a failure is visible per step instead of disappearing
inside one long action.

| Step | What it does | Bounds |
| --- | --- | --- |
| **Run a saved workflow** | Starts a saved node workflow with 1–5 accounts. | A saved `wf_…` workflow that belongs to you. |
| **Auto Join channels/groups** | Joins the links or public usernames you list, one at a time. | 1–500 links and 35–3600 seconds between joins. |
| **Bulk send a message** | Sends one message to the recipients you list. | 1–2000 recipients, 0–3600 seconds between messages, and the consent confirmation inside the step. |
| **Invite members** | Adds people you list to one destination group. | 1–10 usernames, destination group, 35–3600 seconds between invites, consent inside the step. |
| **Transfer members from a source group** | Reads a source group's members and invites up to the limit into a destination group. | 1–10 members, 35–3600 seconds between invites, consent inside the step. |
| **Run Auto Forwarder now** | Starts the existing Auto Forwarder cycle. | Uses its own saved settings and bounds. |
| **Scrape members** | Reads one channel or group's member list. Nothing is sent. | Target ≤256 characters, limit 1–1000, exactly one account. |
| **Notify the operator** | Writes a short notification. | Text only. |
| **Call a webhook / integration** | Calls an MCP or HTTP integration, or a webhook URL directly. | One integration id or one URL with the same SSRF guards as webhooks. |
| **Call an MCP tool** | Calls one tool on a saved MCP integration. | The integration must belong to you; arguments are a JSON object. |
| **Ask the AI provider** | Sends one prompt to the provider configured under Integrations. | Prompt and optional system instruction. |
| **Wait** | Pauses the automation between steps. | 0–600 seconds per step. |

Job steps — workflows, Auto Join, BulkSend, the Adder and Auto Transfer — hand
off to the durable job queue. The step result records the job id and the
automation is marked **dispatched**; delivery, rate limits and receipts stay
with the job surface. The inline steps (Auto Forwarder, scrape, notify, webhook,
MCP, AI, wait) complete inside the automation run.

## Runs and the ledger

Every run gets a `run_…` id, a trigger (`manual`, `schedule` or `event`) and one
row per step with its status, timestamps and a small result. The **Runs** panel
lists the ten most recent runs; **Runs** on an automation lists that
automation's history and expands one run's steps. Run rows are stored on
sqlite with the rest of the durable job state, and step details pass through the
same redaction as job logs: failures record the error type, not Telegram
response text or credentials.

A job-event trigger fires at most once per job and automation, so replaying a
terminal job does not start duplicates.

## Safety

- The same bounds are enforced when an automation is saved and again on every
  run, because the stored definition can be edited on disk.
- BulkSend, Invite members and Auto Transfer require the consent confirmation
  inside the step; the underlying consent record is written when the job is
  enqueued, exactly as on their own surfaces.
- Platform switches still apply: the **Automation** platform feature must be on
  to create or run an automation, and each job step still obeys its own
  feature switch and per-account limits.
- Deleting an automation removes its schedules and its run history.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/automations` | Saved automations, the action catalog, recent runs and whether the feature is on. |
| `GET /api/automations/catalog` | The action catalog on its own. |
| `POST /api/automations` | Create (`201`). |
| `PUT /api/automations/{id}` | Replace name, description, enabled, trigger and steps. |
| `DELETE /api/automations/{id}` | Delete the definition, its schedule and its runs. |
| `POST /api/automations/{id}/run` | Run now (`202`); `{ "dry_run": true }` returns the definition without running. |
| `GET /api/automations/{id}/runs` | That automation's runs. |
| `GET /api/automation-runs/{run_id}` | One run with its steps. |

A body is `{ name, description, enabled, trigger, steps }`. `trigger` is
`{"type": "manual"}`, `{"type": "schedule", "cron": "0 9 * * *"}` or
`{"type": "event", "event": "job.terminal", "kind": "member_transfer"}`
(`kind` optional). `steps` is a non-empty list of `{ action, config }` with at
most 30 steps. A rejected body returns `400` and saves nothing.

## n8n

n8n is unchanged and remains under **Integrations** for the account-factory
flow. It is not used by this engine: an automation can call an n8n webhook as a
**Call a webhook** step if you want a separate workflow connected later.
