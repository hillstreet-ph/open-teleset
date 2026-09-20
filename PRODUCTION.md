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

## Consent-only outreach gate

**Draft implementation; do not deploy as a complete consent boundary.** Independent
review found ungated legacy schedules and direct-send paths, missing per-send
opt-out revalidation, and inconsistent rate bounds. These must be repaired and
covered by integration tests before approval and merge.

The guarded entry points require an `approval_id` whose server-side record matches the
action and sending account, plus current explicit consent for every recipient.
An active suppression wins when the policy is evaluated. The direct member-add
endpoint rejects scrape-and-invite requests; this does not prove that every
outbound route enforces the policy.

Set `OUTREACH_POLICY_FILE` to a provider-mounted secret file (the default is
`./accounts/outreach_policy.json`, which is gitignored). The version 1 document
contains:

- `approvals`: approval references with an exact `action`, expiry, and allowed
  account hashes.
- `consents`: hashed recipients, permitted actions, an explicit-consent source,
  grant/expiry timestamps, and allowed account hashes.
- `suppressions`: hashed recipients with an `active` flag.

Generate recipient and account hashes with `outreach_policy.hash_identifier` in
an approved administrative process. Do not put raw Telegram identifiers,
session values, message bodies, provider tokens, or consent documents in the
policy file or application logs. Mount the file read-only, limit it to the
runtime identity, and update it atomically through the consent system of record.

Intended bounds are 20 recipients with at least 3 seconds between personal or
scheduled sends, five accounts for a multi-account send, and 10 explicit member
additions with at least 35 seconds between attempts. Current integration does
not consistently enforce these bounds. New schedules preserve the approval
reference; legacy schedules and opt-outs during a running batch still need fixes.

Deployment must stop if the policy file is absent/malformed, the approval is
missing/expired/mismatched, consent is absent/expired/mismatched, or any target
is suppressed. These are expected denials, not reasons to bypass the gate.

Rollback preparation: pause scheduled and bulk outbound work before reverting
this change. Reverting restores the older unguarded behavior, so outbound work
must remain paused until a reviewed consent boundary is in place. No database
migration is introduced by this draft.
