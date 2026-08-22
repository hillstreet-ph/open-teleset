# open-teleset platform operations

## Branches
- `main` — production
- `development` — integration (this branch feeds main via PR)

See [BRANCHING.md](./BRANCHING.md).

## Secrets (GitHub Actions)
Never store in git. Required:

| Secret | Used by |
|--------|--------|
| DATABASE_URL | migrate job |
| SUPABASE_URL / SERVICE_ROLE_KEY / ANON_KEY | app + edge |
| SESSION_ENCRYPTION_KEY | session crypto |
| TELEGRAM_API_ID / HASH | Telethon |
| APP_SECRET_KEY / DASHBOARD_PASSWORD | dashboard |
| DOCKERHUB_USERNAME / DOCKERHUB_TOKEN | release images |
| CLOUDFLARE_API_TOKEN / ACCOUNT_ID | Pages + Workers |

## Supabase
1. SQL: `migrations/001_init.sql`, `002_auth.sql`
2. Auth: enable Email (+ OAuth providers in Dashboard)
3. Edge: `supabase functions deploy health-ping run-schedules`
4. Cron: schedule `run-schedules` every 1–5 minutes
5. Vault: store service role only in Supabase secrets / GitHub secrets

## DockerHub versioning
```bash
# bump VERSION file, commit, tag:
git tag v1.0.0
git push origin v1.0.0
# → builds DOCKERHUB_USER/open-teleset:1.0.0 and :latest
```

## Cloudflare
- **Pages**: static dashboard from `static/`
- **Workers**: API edge proxy (`deploy/cloudflare-worker.js`)

## Self-healing / agent
- Scheduled workflow `validate-heal.yml` every 6h: lint, tests, migration presence
- Health worker: `src/open_teleset/workers/health_worker.py`
- Human/agent playbook: fix CI failures on `development`, open PR to `main`

## AI agent maintenance loop
1. Watch Actions failures on main/development
2. Reproduce with pytest / ruff
3. Patch on feature branch → PR → development → main
4. Never print or commit secrets
