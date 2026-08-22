# open-teleset — Production

Secret-free production overlay. **Never commit `.env` or real keys.**

## Repository secrets (GitHub → Settings → Secrets → Actions)

| Secret | Purpose |
|--------|---------|
| `DATABASE_URL` | Supabase Postgres connection string |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only |
| `SUPABASE_ANON_KEY` | Optional client |
| `SESSION_ENCRYPTION_KEY` | Fernet key for sessions |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | my.telegram.org |
| `APP_SECRET_KEY` | App signing |
| `DASHBOARD_PASSWORD` | Dashboard basic auth |
| `CLOUDFLARE_API_TOKEN` | Optional CF deploy |
| `CLOUDFLARE_ACCOUNT_ID` | Optional CF deploy |

## CI pipeline

On push to `main` / `prod-setup`:
1. Lint + crypto tests
2. Apply SQL migrations (if `DATABASE_URL` set)
3. Docker build smoke test
4. Cloudflare Worker deploy (if CF secrets + `deploy/wrangler.toml` present)

## Local bootstrap

```bash
export DATABASE_URL=...
export SUPABASE_URL=...
export SUPABASE_SERVICE_ROLE_KEY=...
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
./scripts/autonomous_setup.sh
```

## Supabase Auth

Migration `002_auth.sql` creates `profiles` linked to `auth.users` and auto-provisions on signup. Enable Email auth in Supabase Dashboard → Authentication.

## Cloudflare

1. Copy `deploy/wrangler.toml.example` → `deploy/wrangler.toml`
2. Set secrets: `wrangler secret put ORIGIN`
3. Add `CLOUDFLARE_*` GitHub secrets
4. Merge to main to deploy
