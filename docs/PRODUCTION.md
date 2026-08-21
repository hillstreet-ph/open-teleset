# Open-Teleset production runbook

Open-Teleset is a standalone service. It is not part of Open-Connect or Open-System.

## Security boundary

Never commit or paste real credentials into source, issues, pull requests, logs, or chat. Rotate all credentials that have been disclosed. Telegram StringSession values are equivalent to account credentials and must be encrypted before persistence.

## Architecture

- Dashboard/API: authenticated FastAPI service
- MCP: authenticated remote MCP service plus local stdio compatibility
- Worker: scheduled and batch operations
- Supabase Auth: user identity
- Supabase Postgres: roles, account metadata, access, schedules, approvals, audit and backup catalog
- Redis: queue, distributed locks and rate limits
- Object storage: encrypted backup objects
- Cloudflare: DNS, TLS, Access/WAF and rate limiting
- Persistent container host: Railway, Zeabur, Fly.io or a managed VPS

Cloudflare Workers and Pipedream are not suitable as the primary Telethon process because Telegram clients require durable connections and state.

## Required secret names

Configure these in the deployment provider, never in Git:

- SUPABASE_URL
- SUPABASE_PUBLISHABLE_KEY
- SUPABASE_SERVICE_ROLE_KEY
- DATABASE_URL
- TELEGRAM_API_ID
- TELEGRAM_API_HASH
- SESSION_ENCRYPTION_KEY
- MCP_SERVER_API_KEY
- REDIS_URL
- REDIS_PASSWORD
- BACKUP_S3_ENDPOINT
- BACKUP_S3_BUCKET
- BACKUP_S3_ACCESS_KEY_ID
- BACKUP_S3_SECRET_ACCESS_KEY
- BACKUP_ENCRYPTION_KEY
- SENTRY_DSN

## First owner

1. Create the first user through Supabase Auth.
2. In Supabase SQL Editor, promote that exact user only:
   `update public.profiles set role = 'owner' where id = '<AUTH_USER_UUID>';`
3. Disable public signup unless explicitly required.
4. Require MFA for the owner and administrators.

## Deployment gates

Do not deploy until:

1. Every disclosed secret has been rotated.
2. Production CI passes.
3. Dashboard authentication is enabled.
4. Session encryption is exercised by an integration test.
5. Destructive MCP tools require an approved action record.
6. Restore from an encrypted backup has been tested.
7. Exact production domain is present in ALLOWED_ORIGINS.
8. Cloudflare TLS, Access, WAF and rate limits are enabled.
9. A staging Telegram account passes login, reconnect and restart testing.

## Rollback

Deploy immutable image tags containing the Git commit SHA. Retain the previous known-good digest. Roll back the application image without rolling back database migrations unless a tested down migration exists. Restore account state only from a checksum-verified encrypted backup.
