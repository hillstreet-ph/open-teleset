# Node workflows

The **Workflows** tab builds one chain of internal actions on a canvas and runs that chain with one of your accounts. It is a visual editor for the same bounded actions the Automation tab schedules — it is not a general integration platform and it cannot contact people on its own.

## Node types

| Node | What it does | Bounds |
| --- | --- | --- |
| **Scrape members** | Reads the member list of one group or channel with the signed-in account. Only a count is kept; member identities are not stored. | Target ≤256 characters, limit 1–1000, bots skipped by default. |
| **Wait** | Pauses the run. | 1–600 seconds per node, 1800 seconds in total across the chain. |
| **Join groups** | Joins the links you list, one at a time. Invite links and public `@usernames` are accepted. | 1–10 links per node and at least 35 seconds between joins. |
| **Note yourself** | Sends one message to your own Saved Messages. `{scraped}` and `{joined}` are replaced with the counts from earlier nodes. | Text ≤1000 characters. |

Actions that contact other people are deliberately absent: creating accounts, buying numbers, adding strangers, outreach and limit evasion are refused by the product and live behind their own consent-gated surfaces, if at all.

## Building a chain

1. **New workflow**, then add nodes from the toolbar. A new node is appended to the tail automatically while the chain has exactly one end.
2. Drag a node to move it. Click a node to open its settings on the right.
3. Click the right dot of one node and then the left dot of another to connect them. Clicking a line disconnects it.
4. Save, choose **Run with account**, then **Run**.

The server validates the graph before saving: 1–20 nodes with unique ids, no self-links or duplicates, at most one edge in and one edge out per node, and exactly one starting node and one ending node that reaches every node exactly once. A workflow is always one chain, so a broken or looping graph is rejected with a `400` and nothing is stored.

Running checks the account is signed in and refuses a second run of the same workflow while the first is still going (`409`). **Stop** cancels the run at the current step.

## Watching a run

The run log under the editor lists every node with its status and a short result: members read, seconds waited, joined/skipped/failed counts, note length, or the Telegram cooldown in seconds. The log polls `/api/workflows/{id}/runs`; the newest run is also shown as **last run** in the list. History keeps the last 10 runs per workflow and is held in memory only — nothing about a run is written to disk.

A failing step stops the chain and marks the run failed. A Telegram flood wait stops the chain and reports the cooldown instead of retrying, so nothing is bypassed. Errors are logged by type only; no exception text, peer data or credential is written to the log.

## Storage and API

Workflows are stored per operator in the encrypted operator store (`workflows.secret.json`) and are scoped to the signed-in operator. Only node ids, types, display positions and the bounded settings above are stored. Without `SESSION_ENCRYPTION_KEY` the routes return `500`.

| Route | Purpose |
| --- | --- |
| `GET /api/workflows` | List your workflows, the capacity and the node catalog. |
| `POST /api/workflows` | Create a workflow. |
| `PUT /api/workflows/{id}` | Replace the chain (refused while it is running). |
| `DELETE /api/workflows/{id}` | Delete a workflow and cancel its run. |
| `POST /api/workflows/{id}/run` | Start a run with `{"account_id": "…"}`. |
| `POST /api/workflows/{id}/stop` | Cancel the running run. |
| `GET /api/workflows/{id}/runs` | The last runs, newest first. |

Anonymous requests receive `403`, and another operator's workflow id returns `404`.