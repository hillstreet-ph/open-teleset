# Production Readiness Runbook

This runbook explains the Open-Teleset setup, automated deploy path and release
gates. Provider changes require an authorized connector or verified console
session. A successful frontend deployment does not prove runtime or OAuth
readiness.

## Explicit Worker-only rollout

Normal releases retain Docker publication before the Worker job. For a bounded
edge-only maintenance rollout, dispatch `deploy.yml` on `main` with
`worker_only=true` and `skip_deploy=false`. This skips Docker, Pages, and Edge
Function publication, but still requires application/container validation and
the migration gate. `scripts/worker_preflight.py` then requires the existing
origin to return fresh liveness metadata, database readiness, and `401` for
anonymous operational requests before the Worker can deploy. An unreachable,
degraded, or anonymously accessible backend blocks this path; it is not an
automatic fallback after a normal release fails.

The reviewed provider targets and environment consumer names are recorded in
`platform.json`. Database configuration prefers `DATABASE_POOLER_URL`, with
`DATABASE_URL` as a fallback. The manifest describes intended configuration;
verify the live bindings and acceptance checks in this runbook separately.

## Environment separation

| Environment | Runtime and data | Credentials | Release gate |
| --- | --- | --- | --- |
| Development | Local runtime and disposable fixtures; never live Telegram sessions | Developer/test credentials only | Python, Node and Ruff checks |
| Staging | Dedicated service and isolated test data; provision before claiming it exists | Staging-scoped provider credentials | Immutable image, container smoke, readiness, OAuth and authorization E2E |
| Production | `open-teleset.site`; persistent `/app/accounts` dashboard state; canonical `open-operations` project and `open_teleset` schema for database-backed components | GitHub `production` environment and verified runtime secret store | Persistent-volume and restore gates, staging acceptance, rollback digest, production health and release evidence |

Do not reuse production session encryption keys, Telegram sessions, service-role
keys or database credentials in development/staging. Keep the existing shared
Supabase user UUIDs, schemas and authorization rules. This runbook does not
provision a separate staging service or change production DNS.

## Configuration inventory

Use [.env.production.example](../.env.production.example) for runtime variable
names and [OAuth setup](OAUTH_SETUP.md) for separate app registrations.
Inventory only provider IDs and secret-manager references, never secret values.

| Destination | Required configuration | Validation |
| --- | --- | --- |
| Supabase Auth | Dedicated Google/GitHub client credentials, exact callback, additive redirect allowlist | `/auth/v1/settings`, then real account sign-in |
| Pages `open-teleset-dashboard` | `static/config.js`: production Supabase URL, public publishable key, same-origin API | Deployed config has no placeholder or service-role key; dashboard restores session |
| Zeabur verified Open-Teleset service | `APP_ENV=production`, port `8080`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, provider-issued `DATABASE_POOLER_URL`, `SESSION_ENCRYPTION_KEY`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | `SELECT 1`, `/health`, `/readyz`, authenticated API |
| Zeabur persistent storage | Retain the existing volume mounted at `/app/accounts`; writable by container UID `10001`; retain its matching session encryption key | Confirm the live mount, protected consistent backup, isolated restore and state preservation across deployment |
| GitHub `production` environment | `SUPABASE_PUBLISHABLE_KEY`, `DATABASE_POOLER_URL` or `DATABASE_URL`, `DOCKERHUB_USERNAME`, write-scoped `DOCKERHUB_TOKEN`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `SUPABASE_TOKEN` | Inspect successful publication/deployment jobs; Supabase token can access the canonical project |
| Cloudflare Worker | `ORIGIN` points to the verified Open-Teleset runtime | Same-origin proxy reaches health and authenticated routes |
| Docker Hub | Write permission for workflow image `hillstreet/open-teleset` | Push immutable commit/version tag; record digest and architecture |
| Sentry and backups | App-scoped DSN/environment/release, encrypted backup store and restoration evidence | Trace test event, restore drill, release record without credentials |

OAuth client secrets stay in Supabase Auth. `SUPABASE_PUBLISHABLE_KEY` is the
only Supabase key permitted in the Pages artifact. An API service-role key is
not a Supabase management token and cannot replace `SUPABASE_TOKEN` in CI.
Environment variable names alone do not establish that a credential is valid
or that a provisioned service is the active runtime. Postgres requires a database
password, not a Supabase API JWT. Verify the pooled username belongs to the
canonical project before changing either database connection reference.

## Persistent state and restoration gate

The deployed `dashboard.py` uses the root `account_manager.py`, which reads and
writes encrypted sessions in `./accounts/config.json`. With the image working
directory `/app`, this is `/app/accounts/config.json`. Schedules, templates,
proxies, health records, logs and statistics also live under `/app/accounts`.
The package database pool is used by dashboard readiness; a successful
`SELECT 1` does not prove these local files are durable or backed up.

Before replacing the runtime or promoting a deployment:

1. Verify the existing volume ID and `/app/accounts` mount on the live service,
   plus runtime ownership/access for UID `10001`. A directory created by the
   Dockerfile or a Compose declaration alone does not prove a Zeabur mount.
2. Capture a consistent, encrypted backup of the entire volume while application
   writes and scheduled/background workers are quiesced. Keep the matching
   `SESSION_ENCRYPTION_KEY` version in the approved secret store separately;
   never print sessions or credentials in evidence. S3 variable presence alone
   does not prove that a backup job ran.
3. Restore to isolated storage, keeping network activity and workers disabled.
   Verify JSON integrity, expected state counts and encrypted-session decoding
   with the retained key without initiating Telegram activity. Record the
   protected backup reference, timestamp and restore result.
4. Deploy using the same volume and encryption key. Compare state counts and
   integrity evidence before and after deployment. If restoring is necessary,
   first preserve the failed state and quiesce writes; restore the verified
   image, volume snapshot and matching key together before resuming workers.

Database, OAuth and API checks remain separate release gates. Do not claim a
successful restore from volume metadata or a database probe alone.

## What is automated

`.github/workflows/deploy.yml` (CI · Migrate · Deploy) runs on every push to
`main`:

1. `test` — Ruff (currently advisory in the workflow), all Node suites, Python
   pytest. Run `ruff check .` as a mandatory local validation.
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

## Provider-side completion sequence

Use an authorized provider connection. Runtime repairs are coordinated in
issue #21; OAuth setup is tracked in issue #25. Do not duplicate a locked task.

1. Reconcile the Zeabur service, environment, attached domain and deployed
   image with the runtime responding at `open-teleset-prod.zeabur.app`. Complete
   the persistent-state gate above; do not restart an assumed service or create
   a duplicate. Probe the actual origin and public domain.
2. On that service, set `DATABASE_POOLER_URL` (preferred) or `DATABASE_URL` to
   the Supavisor pooler for `hoseohvgoiarxluxqwqv`. Copy the exact connection
   string and SSL requirements from the provider's Connect panel; do not guess
   a regional pooler hostname. Confirm runtime network access and `SELECT 1`.
3. Confirm `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SESSION_ENCRYPTION_KEY`,
   `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` are present on the runtime.
4. Deploy the Worker so `ORIGIN` is set (workflow `Configure Worker Secrets`,
   or `deploy.yml`). Without a resolvable Worker/Pages proxy, browser API calls
   fail even when the backend is healthy.
5. After staging acceptance, deploy the approved immutable image digest and
   require `https://open-teleset.site/readyz` to return `200` with
   `{"database": true}`. Verify an unauthenticated management request is rejected
   and an approved Open-Teleset owner/admin request succeeds.
6. Attach Sentry release evidence and record the rollback image digest.

## Known blockers and rollback

At the 2026-10-10 inspection, the direct origin returned `/health` 200 but
`/readyz` 503 (`database_unreachable`). The public host rejected command-line
probes while the dashboard rendered in the browser; this does not establish
public API readiness. The Zeabur server had been renewed and was running, but
service metadata still showed a crashed/suspended deployment, so mapping needs
reconciliation.

Main workflow `37995833282` published Pages successfully. Container publication
failed with Docker Hub `insufficient_scope`; edge deployment failed because its
Supabase management token lacked project access. Worker deployment was skipped
behind the failed image job. Correct credential scopes in their secret stores;
do not weaken checks, expose secrets or report the full deployment as complete.

The previously observed `openclose8/open-teleset:1.0.32` digest was
`sha256:41891355fca3b6e4838ace6f13a46723acee3a4f4c2dd6425f261fb8c67330a1`.
Treat it as a rollback candidate requiring service/architecture and readiness
verification, not as a proven healthy release. Before promotion, record the
actual previous runtime digest. Roll back to that verified digest on failed
readiness, authorization or smoke/E2E checks, then repeat the probes. Retain the
session encryption key and persistent volume so existing encrypted sessions
remain readable; image rollback alone cannot restore lost filesystem state.

## Verifying locally

```bash
pip install -r requirements-prod.txt pytest pytest-asyncio ruff
ruff check .
export SESSION_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
PYTHONPATH=src pytest tests/ -q
node --test tests/*.mjs
PYTHONPATH=src:. python -m uvicorn dashboard:app --host 127.0.0.1 --port 8080
curl -s localhost:8080/health   # 200
curl -s localhost:8080/readyz   # 503 until a database is configured
```
