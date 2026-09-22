# open-teleset — Credentials & Setup Reference

> **Source of truth for _where_ every credential lives and _which app_ needs it.**
> This file contains **variable names, placeholders, and status only** — it MUST NOT contain
> real secret values. Secrets live only in their platform's secret store (GitHub Actions
> Secrets, Zeabur env vars, Cloudflare secrets, Supabase server-side). The only literal values
> included below are explicitly marked **PUBLIC** (safe to expose, e.g. the Supabase anon/
> publishable key that ships in the browser).

_Last reviewed: 2026-09-22._

---

## 1. Canonical infrastructure (non-secret facts)

| Component | Identity | Notes |
|-----------|----------|-------|
| **GitHub** (source of truth) | `hillstreet-ph/open-teleset` | branches: `main` (prod), `development`, `feature/*`, `fix/*`, `chore/*` |
| **Docker Hub** (registry) | `openclose8/open-teleset` | tags: `latest`, `1.0.<run>`, `sha-<short>`; multi-arch amd64+arm64 |
| **Zeabur** (backend runtime) | origin `https://open-teleset-prod.zeabur.app` | auto-pulls the pushed image; runs FastAPI + Telegram + scheduler |
| **Cloudflare** (edge) | site `https://open-teleset.site`; Worker + Pages | Worker proxies to Zeabur `ORIGIN`; Pages serves the dashboard |
| **Supabase** (DB/Auth) — used by open-teleset | project **open-operations** · ref `hoseohvgoiarxluxqwqv` · region `ap-southeast-1` · org **Kobeplay** (`qbromdeoidotfakgtsvj`) | URL `https://hoseohvgoiarxluxqwqv.supabase.co` |
| Supabase (other project, NOT used here) | **open-platform** · ref `huadtiuuoiriqrjpjxhr` | separate app — do not point open-teleset at it |
| Supabase publishable/anon key | **PUBLIC**: `sb_publishable_VlMuLWtIfCd5TpYS3cNXLQ_19XzR6Tx` | browser-safe; ships in `static/config.js`. Never confuse with the service-role key. |

---

## 2. Credential matrix — which credential, which platform, which app

Legend for **Where**: `GH` = GitHub Actions Secret · `ZB` = Zeabur env var · `CF` = Cloudflare
Worker/Pages secret · `SB` = Supabase server-side. Values are placeholders — fill in the store, not here.

### 2.1 Telegram
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `TELEGRAM_API_ID` | ZB | backend, login, workers | REQUIRED — set in Zeabur |
| `TELEGRAM_API_HASH` | ZB | backend, login, workers | REQUIRED — set in Zeabur |

### 2.2 Session / app secrets
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `SESSION_ENCRYPTION_KEY` | ZB + GH + CF | backend (session crypto), crypto tests, Worker | REQUIRED — must be the **same** value in all three; Fernet key |
| `SESSION_ENCRYPTION_KEY_VERSION` | ZB | backend | optional, default `1` |
| `APP_SECRET_KEY` | ZB | backend | REQUIRED — long random ≥32 chars |
| `MCP_SERVER_API_KEY` | ZB | MCP server auth | REQUIRED if MCP exposed |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | ZB | dashboard basic auth | set strong values |

### 2.3 Supabase (database / auth)
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `SUPABASE_URL` | ZB + GH + SB | backend, edge fns, CI | = `https://hoseohvgoiarxluxqwqv.supabase.co` (non-secret) |
| `SUPABASE_PUBLISHABLE_KEY` | GH + CF + ZB | browser dashboard, backend | **SET** (Pages deploy verified it exists). Public-safe key. |
| `SUPABASE_SERVICE_ROLE_KEY` | ZB + GH + SB | backend, edge fns | REQUIRED — **secret**, server-side only, never in browser |
| `SUPABASE_TOKEN` (Supabase **access/PAT**) | GH | CI `deploy-edge`, migrations | ❌ **BROKEN** — current token lacks privileges (see §4) |
| `DATABASE_URL` | ZB + GH | backend, migrations | REQUIRED — `postgresql://postgres:<pw>@db.hoseohvgoiarxluxqwqv.supabase.co:5432/postgres` |
| `DATABASE_POOLER_URL` | GH | migrations (pooler) | port `6543` variant |
| `SUPABASE_JWT_ISSUER` | ZB | backend JWT verify | `https://hoseohvgoiarxluxqwqv.supabase.co/auth/v1` (non-secret) |
| `SUPABASE_JWT_AUDIENCE` | ZB | backend JWT verify | `authenticated` (non-secret) |

### 2.4 Docker Hub (CI/CD)
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `DOCKERHUB_USERNAME` | GH | `docker` + `release-dockerhub` jobs | SET (build/push verified) |
| `DOCKERHUB_TOKEN` | GH | image push auth | SET (build/push verified) |

### 2.5 Cloudflare (edge)
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `CLOUDFLARE_API_TOKEN` | GH | Worker + Pages deploy | SET (deploy verified) |
| `CLOUDFLARE_ACCOUNT_ID` | GH | Worker + Pages deploy | SET (deploy verified) |

### 2.6 Storage / cache / observability (optional but recommended)
| Variable | Where | Consumed by | Status |
|----------|-------|-------------|--------|
| `REDIS_URL` | ZB | scheduler, cache | recommended |
| `S3_ENDPOINT` / `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` / `S3_BUCKET` / `S3_REGION` | ZB | media & exports (R2/S3) | optional |
| `BACKUP_S3_ENDPOINT` / `BACKUP_S3_BUCKET` / `BACKUP_S3_ACCESS_KEY_ID` / `BACKUP_S3_SECRET_ACCESS_KEY` / `BACKUP_ENCRYPTION_KEY` | ZB | backup export | recommended for DR |
| `SENTRY_DSN` | ZB | error tracking | optional |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ZB | tracing | optional |

---

## 3. Zeabur backend — environment variable checklist

Zeabur **auto-pulls** `openclose8/open-teleset:latest` (or pin a specific `1.0.<run>` tag for
rollback). No deploy trigger needed from CI. Set these on the Zeabur **service** before/after redeploy:

Required: `APP_ENV=production`, `PORT=8080`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`SESSION_ENCRYPTION_KEY`, `APP_SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_PUBLISHABLE_KEY`, `DATABASE_URL`, `SUPABASE_JWT_ISSUER`, `SUPABASE_JWT_AUDIENCE`.
Recommended: `REDIS_URL`, `SENTRY_DSN`, backup S3 set, `MCP_SERVER_API_KEY`, `DASHBOARD_USER/PASSWORD`.

Zeabur service health: `GET /health` (liveness), `GET /readyz` (checks DB). Configure the health
check to `/readyz` so DB-unreachable states are caught. Keep a persistent volume only for state
that cannot be reconstructed (Telegram session files if not stored in Supabase).

---

## 4. Fix: `SUPABASE_TOKEN` (unblocks the failing `deploy-edge` CI job)

**Symptom:** `supabase link --project-ref hoseohvgoiarxluxqwqv` fails with
_"Your account does not have the necessary privileges to access this endpoint"_, failing the
`deploy-edge` job on every push to `main`. Edge functions are currently **not deployed** (0 live).

**Root cause:** the account behind the current `SUPABASE_TOKEN` is not a member of the **Kobeplay**
org that owns `hoseohvgoiarxluxqwqv`.

**Remediation (manual — tokens must not flow through automation):**
1. Sign in to Supabase as a member of the **Kobeplay** org (`qbromdeoidotfakgtsvj`).
2. Account → **Access Tokens** → generate a new personal access token (`sbp_...`).
3. GitHub → repo **Settings → Secrets and variables → Actions** → update secret **`SUPABASE_TOKEN`**
   with the new token. (Also confirm the `production` environment doesn't override it.)
4. Re-run the latest `CI · Migrate · Deploy` workflow; `deploy-edge` should link + deploy.

> ⚠️ **Before enabling edge deploys:** the repo's `supabase/functions/` includes `send-message`
> and `run-schedules` — the outbound sending paths that PR #17 (consent/suppression guard) is
> still holding as unmerged. Deploying them now would put an **un-consent-guarded sender** live.
> Recommended: either land PR #17 first, or temporarily restrict `deploy-edge` to `health-ping`
> until the guard is in place.

---

## 5. Security advisories (Supabase `open-operations`, 2026-09-22)

Review before hardening — do not blindly change, some may be intentional:
- **INFO** `platform.service_identities` has RLS enabled but **no policy** (table is fully locked).
- **WARN** 6 `SECURITY DEFINER` functions are RPC-executable by `authenticated`:
  `operations_shared.current_app_role`, `operations_shared.sync_authorized_identity`,
  `platform.user_in_org`, `platform.user_role`, `private.has_role`, `public.handle_new_user`.
  Revoke `EXECUTE` or switch to `SECURITY INVOKER` if signed-in users shouldn't call them.
- **WARN** Auth **leaked-password protection disabled** — enable HaveIBeenPwned check in Auth settings.

---

## 6. Golden rules (recap)

- Never commit real secret values; never paste them into this doc, a PR, an issue, or logs.
- `SUPABASE_SERVICE_ROLE_KEY` and `DATABASE_URL` are server-side only — never in browser/Cloudflare Pages.
- Rotate a credential in its store, then update every platform that holds a copy
  (`SESSION_ENCRYPTION_KEY` and `SUPABASE_*` are duplicated across ZB/GH/CF).
- `latest` only tracks validated releases; pin `1.0.<run>` on Zeabur for deterministic rollback.
