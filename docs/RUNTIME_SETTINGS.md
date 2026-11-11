# Runtime settings

Server-owned values the backend reads at runtime, so an operator can change one
without a redeploy. Today that is the `tgstat_token` used by account-free
Channel & Group Finder discovery.

## Where the value lives

`operations_shared.app_settings(key, value, updated_at)` in Supabase. Row Level
Security is on with **no policies**, so only the service role (which bypasses
RLS) can read or write it. The browser never sees the value:
`src/open_teleset/app_settings.py::get_setting` reads it over the REST API with
the service key and caches the answer for five minutes.

Resolution order is:

1. the purpose-named environment variable (`TGSTAT_TOKEN`) when it is set and
   non-empty — container secrets keep working and always win;
2. the stored row;
3. `""` (treated as unset).

A failed lookup degrades to the last known value instead of raising.

## Writing the value

Never paste a credential into a command line, an issue, or a commit. The value
travels as the `TGSTAT_TOKEN` environment secret of the GitHub `production`
environment, and a dispatch-only workflow stores it:

```bash
gh workflow run configure-runtime-settings.yml --repo hillstreet-ph/open-teleset --ref main
```

The `production` environment only permits runs from `main`, so dispatch from
`main`. The workflow runs two steps:

1. `scripts/store_runtime_setting.py tgstat_token` upserts the row through the
   service role and prints only `tgstat_token: stored (HTTP 201)`.
2. `scripts/check_tgstat_token.py "<keyword>"` calls the same TGStat endpoint the
   app uses and prints a JSON report — HTTP status, `status`, item count and
   TGStat's own `error`. The token itself is never printed.

## Reading the report

- `{"status": "ok", "items": <n>}` — the directory answers.
- `{"status": "error", "error": "no_active_subscription"}` — the token is valid
  but its TGStat account has no active API subscription. Directory search
  cannot work until the account owner activates one; discovery falls back to
  public web search and says so instead of returning a bare zero.
- `http_401` / `http_403` — the token is rejected; replace the secret.
