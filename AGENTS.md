# AI Agent Instructions

This repository is part of the **hillstreet-ph** organization and follows the shared multi-agent development protocol.

## Canonical Protocol

The full multi-agent development protocol is maintained in [`open-connect/docs/MULTI_AGENT_DEVELOPMENT_PROTOCOL.md`](https://github.com/hillstreet-ph/open-connect/blob/main/docs/MULTI_AGENT_DEVELOPMENT_PROTOCOL.md).

All AI agents (Claude, ChatGPT, Grok) **must** read and follow that protocol before making any changes.

## Mandatory Behaviors

1. **Discovery first** — search the repo, issues, PRs, and branches before creating anything new.
2. **Adopt before creating** — modify existing code/config before writing new. Never duplicate.
3. **Lock before working** — apply the `agent:locked` label to the issue you are implementing.
4. **One agent per issue** — check for `agent:locked` before starting. If locked, pick another task.
5. **Branch naming** — use `agent/<issue-number>-<short-purpose>` for all work branches.
6. **Agent labels** — apply your identity label (`agent:claude`, `agent:chatgpt`, or `agent:grok`).
7. **PR contract** — every PR must have: summary, test evidence, issue reference, no secrets.
8. **CI must pass** — never merge with failing required checks. Fix CI before requesting review.
9. **No secrets in code** — never commit credentials, tokens, keys, or passwords.
10. **Coordinate via task board** — check the [Agent Task Board](https://docs.google.com/spreadsheets/d/18CtvAvfOLu2LQvgQ9OqceKxNHO9AvHEJnULzywj0bX8/edit#gid=1257404761) for assignments and status.

## Agent Hierarchy

| Role | Responsibility |
|------|---------------|
| **Coordinator** | Assigns tasks, resolves conflicts, approves architecture changes |
| **Developer** | Implements features, fixes bugs, writes tests |
| **Reviewer** | Reviews PRs, validates CI, checks for regressions |
| **QA** | Runs integration tests, validates deployments |
| **Release** | Tags versions, builds Docker images, publishes releases |
| **Deployment** | Deploys to Zeabur/Cloudflare, runs migrations, verifies health |
| **Recovery** | Investigates failures, rolls back broken deployments |

## Platform Stack

| Component | Service |
|-----------|---------|
| Source of truth | GitHub (`hillstreet-ph` org) |
| Container registry | Docker Hub |
| DNS / CDN / Edge | Cloudflare |
| Database / Auth | Supabase (Kobeplay org) |
| Runtime | Zeabur |
| Observability | Sentry |
| Coordination | Google Sheets, GitHub Issues + Labels |

## Connector Priority

When integrating with external services, prefer in this order:

1. Native API / MCP connector
2. Composio
3. Slim.tools
4. Pipedream

## Labels

This repo uses the following coordination labels:

- `agent:claude` — assigned to or worked on by Claude
- `agent:chatgpt` — assigned to or worked on by ChatGPT
- `agent:grok` — assigned to or worked on by Grok
- `agent:locked` — implementation in progress, do not duplicate
- `agent:review` — ready for agent review
- `agent:blocked` — blocked on external dependency or human action
- `auto-merge` — approved for automatic merge when checks pass
- `auto-deploy` — approved for automatic deployment after merge

## Repository Notes for Agents

- **Two source trees.** Root modules (`dashboard.py`, `account_manager.py`,
  `main.py`, `scheduler.py`, …) are the dashboard/MCP runtime. `src/open_teleset/`
  is the package used by `requirements-prod.txt` tooling (`db/client.py`,
  `crypto/`, `security.py`, `workers/`). `dashboard.py` imports the package via
  `from src.open_teleset.db.client import init_pool` and the root modules directly.
- **Commands.**
  ```bash
  pip install -r requirements-prod.txt pytest pytest-asyncio ruff
  ruff check .                                   # must be clean; CI runs src+scripts
  export SESSION_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  PYTHONPATH=src pytest tests/ -q                # 100+ tests
  node --test tests/test_edge_functions.mjs      # Worker/edge boundaries
  ```
- **Deploy path.** `.github/workflows/deploy.yml` runs test → container-test →
  migrate → docker → worker → pages → edge. Migrations are advisory for the
  shared canonical Supabase project; `/readyz` is the real database gate. See
  `docs/DEPLOYMENT_RUNBOOK.md`.
- **Fail-closed rules.** Never weaken `/readyz` (returns `503` +
  `reason=database_not_configured|database_unreachable` until `SELECT 1` works).
  Never bypass `scripts/apply_migrations.py` guards that protect the shared
  `profiles`/auth triggers.
- **Provider secrets.** `ZEABUR_TOKEN`, `CLOUDFLARE_*`, `SUPABASE_*`,
  `DATABASE_*`, `DOCKERHUB_*` are not available in the agent sandbox. Provider
  console actions are tracked in issue #21 and are human-owned.
- **Frontend base URL.** `static/config.js` sets `apiBase` to
  `https://open-teleset.site` (same-origin, Worker-proxied). Do not hardcode
  `*.workers.dev` hosts; that host was unresolvable and broke all browser API
  calls.

