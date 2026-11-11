# Node workflows

The **Workflows** tab builds one chain of internal actions on a canvas and runs
that chain with one or more of your accounts. It is a visual editor for the same
bounded actions the Automation tab schedules — it is not a general integration
platform and it cannot contact people on its own.

## Node types

| Node | What it does | Bounds |
| --- | --- | --- |
| **Scrape members** | Reads the member list of one group or channel with the signed-in account. The usernames feed a later **Save to a table** node; the run log keeps counts only. | Target ≤256 characters, limit 1–1000, bots skipped by default. |
| **Inspect a group** | Reads a group's title, kind and member count without touching its member list. Useful as a first node to confirm the target. | Target ≤256 characters. The member count is best effort; a hidden count shows as "member count hidden". |
| **Read a table** | Loads one text column from a Data Table for later nodes. A following **Join groups** node with *Take the links from the last Read a table node* joins that list. | Real `t_…` table and `c_…` text column ids; the table must belong to you. |
| **Save to a table** | Adds the usernames read by an earlier Scrape node to one text column of a Data Table, once each (case-insensitive dedupe). | Needs a Scrape node before it in the chain; the table must belong to you; stops at the table's row cap. |
| **Join groups** | Joins the links you list, one at a time, or the links loaded by a **Read a table** node. Invite links and public `@usernames` are accepted. | 1–10000 links per node (or the table's values) and at least 35 seconds between joins. A table-sourced join needs a Read a table node earlier in the chain. |
| **Wait** | Pauses the run. | 1–600 seconds per node, 1800 seconds in total across the chain. |
| **Note yourself** | Sends one message to your own Saved Messages. `{scraped}`, `{table}` and `{joined}` are replaced with the counts from earlier nodes. | Text ≤1000 characters. |

Actions that contact other people are deliberately absent: creating accounts,
buying numbers, registering or logging in accounts, adding strangers, outreach,
auto-reply and limit evasion are refused by the product and live behind their
own consent-gated surfaces (Account Factory, Adder, BulkSend, Auto Reply), if at
all. There is no connector node that reaches external services.

## Presets

**Start from a preset** offers five drafts: *Member sweep to a table*, *Member
sweep digest*, *Sweep, save and digest*, *Slow join rollout* and *Join from a
table*. A preset opens on the canvas with its required fields (group target,
table, column, join links) left empty on purpose — fill them in before saving or
running, so a preset never runs against a guessed target. *Join from a table*
ships a **Read a table** node wired into a **Join groups** node with *Take the
links from the last Read a table node* already checked; pick the table and the
text column that holds the links.

## Building a chain

1. **New workflow** (or a preset), then add nodes from the toolbar. A new node is
   appended to the tail automatically while the chain has exactly one end.
2. Drag a node to move it. Click a node to open its settings on the right.
3. Click the right dot of one node and then the left dot of another to connect
   them. Clicking a line disconnects it.
4. Save, pick one or more accounts under **Run with accounts**, then **Run**.

The server validates the graph before saving: 1–20 nodes with unique ids, no
self-links or duplicates, at most one edge in and one edge out per node, and
exactly one starting node and one ending node that reaches every node exactly
once. A workflow is always one chain, so a broken or looping graph is rejected
with a `400` and nothing is stored.

## Running with several accounts

A run takes 1–5 signed-in accounts. Accounts run **sequentially**, each with its
own fresh context and a 5-second gap between accounts — this is a conservative
rotation through your own accounts, not a way to multiply limits. A failing
account stops the run; the accounts after it stay pending. A second run of the
same workflow while one is going is refused (`409`). **Stop** cancels the run at
the current step.

## Scheduling a workflow

**Schedule** under the editor stores a `run_workflow` task in the same scheduler
the Automation tab uses. Pick the accounts (1–5, signed in), a repeat of *once*,
*daily*, *weekly* or *workdays*, and a time; a one-off also needs a future date.
The schedule appears in the workflow's schedule list and in the Schedule tab,
where it can be toggled or deleted like any other task. At execution time the
scheduler re-checks the workflow still exists (a deleted workflow disables the
schedule) and runs only the accounts that are still signed in.

## Watching a run

The run log under the editor lists every node per account with its status and a
short result: members read, group title and member count, values read from a
table, rows appended/skipped, seconds waited, joined/skipped/failed counts with
the failure reasons, note length, or the Telegram cooldown in seconds. The log
polls `/api/workflows/{id}/runs`; the newest run is also shown as **last run** in
the list. History keeps the last 10 runs per workflow and is held in memory only
— nothing about a run is written to disk.

A failing step stops the chain and marks the run failed. A Telegram flood wait
stops the chain and reports the cooldown instead of retrying, so nothing is
bypassed. A join node that reads an empty table stops with `table_empty` rather
than running against nothing. Failures are reported by a fixed short code, and
the dashboard spells it out in plain words:

| Code | Meaning |
| --- | --- |
| `account_not_connected` | The account is no longer signed in. |
| `operator_required` | The step needs a signed-in operator and had none. |
| `timed_out` | The step ran past its time budget. |
| `network` | A connection error ended the step. |
| `invalid_link` | A join target was neither an invite link nor a public username. |
| `telegram_…` | Telegram refused the action (for example `telegram_peer_flood`); the name comes from the error class, not its message. |
| `step_failed` | Anything else, with no further detail. |

Only these codes are logged. No exception text, peer data or credential is
written to the log.

## Storage and API

Workflows are stored per operator in the encrypted operator store
(`workflows.secret.json`) and are scoped to the signed-in operator. Only node
ids, types, display positions and the bounded settings above are stored. Without
`SESSION_ENCRYPTION_KEY` the routes return `500`.

| Route | Purpose |
| --- | --- |
| `GET /api/workflows` | List your workflows, capacity, node catalog, presets and workflow schedules. |
| `POST /api/workflows` | Create a workflow. |
| `GET /api/workflows/templates` | The preset drafts, served as copies. |
| `GET /api/workflows/{id}` | Read one workflow. |
| `PUT /api/workflows/{id}` | Replace the chain (refused while it is running). |
| `DELETE /api/workflows/{id}` | Delete a workflow and cancel its run. |
| `POST /api/workflows/{id}/run` | Start a run with `{"account_ids": ["…"]}` (legacy `{"account_id": "…"}` still works). |
| `POST /api/workflows/{id}/schedule` | Store a repeat schedule for 1–5 signed-in accounts. |
| `POST /api/workflows/{id}/stop` | Cancel the running run. |
| `GET /api/workflows/{id}/runs` | The last runs, newest first. |

Anonymous requests receive `403`, and another operator's workflow id returns
`404`.

## Verification

`tests/test_workflows.py` covers graph validation, the table node (real encrypted
store, dedupe, fail-closed without an operator), sequential multi-account runs,
the run cap, presets served as copies, and schedule validation and persistence.
`tests/test_workflow_nodes.py` covers the completed catalog: inspect, read a
table, table-sourced joins (including an empty table), the honest join counts and
failure reasons, the safe failure codes, the `{table}` note token, and the graph
rule that ties a table-sourced join to an earlier **Read a table** node.
`tests/test_automation_schedules.py` covers the scheduler side: bounds, stored
fields, starting a run for the owning operator, self-disabling on a missing
workflow, and the signed-in account filter. `tests/test_workflows_ui.mjs` drives
the dashboard methods against a fake HTTP client.