# Open-Teleset Google and GitHub sign-in

Open-Teleset uses the shared `open-operations` Supabase Auth project
`hoseohvgoiarxluxqwqv`. Preserve other apps' site URL and redirect entries.

Both providers need registered OAuth applications, not account passwords,
GitHub personal tokens, or GitHub App signing keys.

Provider callback (register this with Google and GitHub):
`https://hoseohvgoiarxluxqwqv.supabase.co/auth/v1/callback`

Supabase redirect allowlist entries:

- `https://open-teleset.site/dashboard`
- `https://www.open-teleset.site/dashboard`
- `https://open-teleset-dashboard.pages.dev/dashboard`

## Google

Use a Web application OAuth client in Google Cloud. Register the provider
callback above as an authorized redirect URI and the application origin
`https://open-teleset.site`. Configure the consent audience to permit
`tanauancharles1@gmail.com` and `kairocasino8@gmail.com`; when the consent app
is in Testing, both must be test users. Configure the client ID and secret
in Supabase's Google provider and enable it. The dashboard requests the
Google account chooser, with the entered email as an optional hint.

## GitHub

Register or reuse an OAuth App with homepage `https://open-teleset.site`
and the provider callback above. Configure its client ID and secret in
Supabase's GitHub provider and enable it. Sign in with the GitHub identity
whose verified email is `tanauancharles1@gmail.com`. The dashboard requests
`user:email` only. Account email addresses do not configure OAuth clients.

## Verification

Inspect `/auth/v1/settings` with the public publishable key; `external.google`
and `external.github` must be true. Test each Google account and the GitHub
account in Android Chrome through the dashboard. Confirm return to this app,
session restoration, and the existing administrator authorization gate.
Successful OAuth alone does not grant administrator access.

At the 2026-10-10 investigation, both providers were disabled and client
credentials absent. Callback allowlist entries were added, but enabling the
providers requires valid credentials. Runtime health was also degraded with
an unreachable Zeabur cluster. These are separate readiness blockers.

Local validation: `node --test tests/*.mjs` and the repository Python tests.
Revert the frontend commit to roll back UI behavior; remove only the three
Open-Teleset redirect entries if rolling back the configuration change.
