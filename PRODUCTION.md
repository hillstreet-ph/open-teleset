# Open-Teleset production setup

Production verification is pending. Never commit runtime credentials or `.env` files.

## Configuration and release flow

Use the approved provider secret stores or GitHub production environment for
`DATABASE_URL` / `DATABASE_POOLER_URL`, `SUPABASE_URL`, server-only
`SUPABASE_SERVICE_ROLE_KEY`, `SESSION_ENCRYPTION_KEY`, and
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`. The browser needs the matching
`SUPABASE_PUBLISHABLE_KEY`; it must never receive the service-role key.
Cloudflare and registry credentials remain scoped to their release jobs.
`DASHBOARD_PASSWORD` is not a substitute for the Supabase authorization boundary.

The existing CI workflow runs Python and Edge tests, builds the real runtime
container, and probes startup and anonymous access. Main-branch jobs also apply
migrations, publish the image, and deploy configured provider components. Merging
can therefore trigger production writes: complete the gates below first.
Migration and Edge deployment failures are fatal. All main deployment jobs depend
on successful migrations. Standalone Pages/Edge deployments are manual to avoid
duplicate automatic deployment; standalone jobs and tagged releases require the
same commit to have passed main CI, container checks and migrations. The legacy lint step remains
advisory; green tests alone do not prove a clean repository-wide lint audit.

The existing `migrations/001`–`003` set belongs to the legacy standalone public
schema. Its signup trigger assumes `public.profiles.email` and replaces shared
Auth behavior, which is incompatible with the canonical operations database.
The runner now rejects canonical/shared targets before any schema or migration
ledger write, including when a legacy ledger already exists. It exits nonzero;
it does not mark these migrations applied. Deployment stays blocked until a
reviewed migration set and ledger isolated in `open_teleset`, matching database
queries, and backup/rollback validation are ready. Do not remove shared-schema
markers, point credentials elsewhere, or add fabricated ledger entries to pass.

The manual `validate-heal.yml` provider preflight performs read-only checks and
prints only configuration matches, secret-presence booleans, OAuth enablement,
and service identifiers. It does not print credential values or modify providers.
Environment-level secrets override same-named repository secrets; validate the
resulting runtime project before deployment.

Use the existing Cloudflare configuration and Zeabur service mapping. Supabase
OAuth redirects, identity provisioning, role assignments, and runtime environment
must be verified together; the legacy `public.profiles` migration does not create
the canonical operations role tables described below.

## Consent and outbound authorization

Outreach requires a server-owned approval reference, current explicit recipient
consent, the correct action and sending account, and no active suppression.
The runtime resolves Telegram peers to stable signed numeric IDs before the final
policy check. Hash those IDs with `outreach_policy.hash_identifier`; usernames
may be accepted as lookup inputs but are not the final consent identity.

`OUTREACH_POLICY_FILE` defaults to `./accounts/outreach_policy.json`. Version 1
contains `approvals` (action, expires_at, account_hashes), `consents`
(subject_hash, actions, source=explicit, granted_at, expires_at, account_hashes),
and `suppressions` (subject_hash, boolean active). Malformed records deny the
whole document. The file must be mounted read-only and changed atomically by the
approved consent system; no policy-editing HTTP endpoint is exposed.

`OUTREACH_RATE_DB` defaults to `./accounts/outreach_rate.sqlite3`. All processes
serving an account must share this persistent path. Every attempt reserves a
cooldown, including failed sends, and rechecks opt-out/expiry after waiting.
The guarded Telegram client repeats this check at each actual send RPC after
media uploads or peer resolution; automatic RPC retry/flood sleeping is disabled.
Both dispatch and RPC checks reserve time, so effective waits may exceed the
minimum. An uncertain result must be reconciled before retrying.
Separate replicas on separate volumes are not supported by this rate limiter.
Use one runtime replica until a distributed rate store is validated.

Bounds: at most 20 personal recipients per request, at least three seconds between
attempts; at most five accounts per batch; ten explicit member additions per REST
request, at least 35 seconds between attempts. MCP group creation/invitation is
limited to one consented member per call. Legacy schedules cannot bypass checks
through missing policy metadata; invalid or unauthorized schedules are paused.
Native Telegram delayed sends are disabled because they cannot recheck opt-outs.

Audit output includes action, hashed account/subject reference and outcome, not
message/session/token content. Configure provider log retention and access controls
before deployment. Application log_manager retains at most 1000 operational rows;
this does not establish provider-side retention or deletion compliance.

## Login and account sessions

HTTP operational routes and WebSockets require a valid Supabase access token,
an approved global owner/admin in `operations_shared.account_roles`, and an
explicit owner/admin assignment for `open-teleset` in `operations_shared.project_access`.
User metadata never grants access. Public health/static pages remain accessible.
Only project administrators can operate/export shared Telegram accounts until an
account-level ownership model is validated.

Required runtime configuration: canonical `SUPABASE_URL`, server-only
`SUPABASE_SERVICE_ROLE_KEY`, matching asymmetric JWKS, and the browser publishable
key supplied by PR #15. `SUPABASE_JWT_ISSUER` must match URL + `/auth/v1`; audience
is `authenticated`. Browser API requests carry refreshed Bearer tokens; WebSocket
tokens use a subprotocol offer, never URL parameters.

Telegram credentials come only from runtime `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH`. `SESSION_ENCRYPTION_KEY` is required for session storage.
New local account configs contain encrypted session fields and use atomic mode-600
writes. Old plaintext configs deliberately fail closed. To prepare migration:

```sh
PYTHONPATH=src python scripts/encrypt_account_config.py /protected/config.json /protected/config.encrypted.json
```

Supply the existing encryption key from its secret store; this command does not
rotate it. The original file is preserved, existing destinations are rejected,
and encryption roundtrips are validated. Stop the runtime, take the approved
backup, validate the output, then promote it under the deployment approval.
No production session conversion has been executed by this change.

## Deployment gates and known provider prerequisites

Use the existing Zeabur project/service only after authoritative mapping and rollback
are verified. Mount persistent account storage and the server-owned policy. Redis
and database readiness must be independently checked; `/health` is liveness only.

PR CI builds the actual container and probes startup, anonymous API denial, and
unconfigured dependency readiness. Edge `send-message` forwards to the protected
runtime using `TELESET_API_ORIGIN`; it never claims an audit insert queued a message.
A timeout has an unknown delivery outcome and is not safe to retry blindly.
The former Edge scheduler returns 410 without advancing tasks; the persistent
backend scheduler owns execution. Edge JWT verification remains enabled except
for the public health function.

Before merge/deploy: required tests and container checks, independent review,
verified runtime mapping/digest, scoped environment values, approved admin
provisioning, OAuth/redirect settings, session backup/migration/rollback,
Supabase/Auth/Storage tests, TLS and Sentry release evidence must pass. Do not
interpret a 200 liveness response or successful image build as full verification.

Rollback: pause outbound schedules and bulk activity before reverting the code.
The old version does not enforce this consent boundary or read encrypted JSON
sessions; never restore plaintext sessions or resume unguarded sends merely to
make a rollback start. Keep the approved backup and immutable rollback image.
