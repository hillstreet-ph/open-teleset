# Production Readiness Runbook

This runbook explains what the automated deploy path does, how to read its
gates, and what only a human with provider access can finish. It exists because
code can be fixed and merged autonomously, but production credentials and
provider consoles cannot.

## What is automated

`.github/workflows/deploy.yml` (CI · Migrate · Deploy) runs on every push to
`main`:

1. `test` — Ruff, Node edge tests, Python pytest.
2. `container-test` — builds `deploy/Dockerfile` and probes liveness plus the
   anonymous access boundary via `scripts/container_smoke.py`.
3. `migrate` — applies `migrations/*.sql` when a database is configured.
4. `docker` — builds and pushes `hillstreet/open-teleset` multi-arch.
5. `deploy-worker` — deploys the Cloudflare Worker and sets `ORIGIN`.
6. `deploy-pages` — publishes `static/` to Cloudflare Pages.
7. `deploy-edge` — deploys Supabase Edge Functions.

## Migration gate semantics

`scripts/apply_migrations.py` prints a machine-readable `MIGRATIONS_STATUS`:

| Status | Meaning | Deploy effect |
| --- | --- | --- |
| `applied` | Migrations ran and were recorded. | continues |
| `no_files` | Nothing to apply. | continues |
| `not_applicable` | Target is the canonical shared Supabase project; the legacy public migration set must never overwrite shared `profiles`/auth triggers. | continues (skip) |
| `not_configured` | No `DATABASE_URL` / `DATABASE_POOLER_URL`. | continues (skip) |
| `unreachable` | A database is configured but cannot be reached. | continues (warning) |
| anything else | A genuine migration error. | fails |

`not_applicable`, `not_configured`, and `unreachable` are accepted skips because
they are not code defects. Real database readiness is proven by the runtime
`/readyz` probe, which stays fail-closed (`503`) until `SELECT 1` succeeds.

## Readiness contract

- `GET /health` / `/healthz` — liveness only; always `200` while the process runs.
- `GET /readyz` — `200` only when the database answers `SELECT 1`. On failure it
  returns `503` with `reason` set to `database_not_configured` or
  `database_unreachable`.

Never weaken `/readyz` to hide a missing connection reference.

## Provider-side actions (require human credentials)

These cannot be completed from CI or an agent sandbox and are tracked in
issue #21:

1. Confirm the Zeabur service for `open-teleset-prod` is running and reachable.
   As of this run, `https://open-teleset-prod.zeabur.app` resolves but times out
   on 443, so the Worker's `/health` fails closed with `upstream_unavailable`.
2. On that service, set `DATABASE_POOLER_URL` (preferred) or `DATABASE_URL` to
   the Supavisor pooler for `hoseohvgoiarxluxqwqv`, e.g.
   `postgresql://postgres.<ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres`.
   Direct `db.<ref>.supabase.co` hosts do not resolve in CI.
3. Confirm `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SESSION_ENCRYPTION_KEY`,
   `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` are present on the runtime.
4. Deploy the Worker so `ORIGIN` is set (workflow `Configure Worker Secrets`,
   or `deploy.yml`). Without a resolvable Worker/Pages proxy, browser API calls
   fail even when the backend is healthy.
5. Redeploy the runtime and require `https://open-teleset.site/readyz` to return
   `200` with `{"database": true}`.
6. Attach Sentry release evidence and record the rollback image digest.

## Verifying locally

```bash
pip install -r requirements-prod.txt pytest pytest-asyncio ruff
ruff check .
export SESSION_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
PYTHONPATH=src pytest tests/ -q
node --test tests/test_edge_functions.mjs
PYTHONPATH=src:. python -m uvicorn dashboard:app --host 127.0.0.1 --port 8080
curl -s localhost:8080/health   # 200
curl -s localhost:8080/readyz   # 503 until a database is configured
```
