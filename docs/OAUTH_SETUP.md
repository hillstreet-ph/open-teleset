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

Use the dedicated **Open-Teleset** OAuth App registered on 2026-10-10 under
`master-kanor`, application ID `3918956`, public client ID
`Ov23liCiLtQnlvOqD6vr`:
<https://github.com/settings/applications/3918956>.
Its homepage is `https://open-teleset.site` and its exact redirect URI is the
provider callback above. Wildcard redirects and device flow are disabled.
Keep the existing Open-TGate and Open Connect registrations intact.

Generate the dedicated client secret after GitHub's **Confirm access** check.
Save it directly in Supabase Auth or the approved secret manager; do not put it
in repository files, browser config, CI logs, issues, or credential spreadsheets.
Configure its client ID and secret in
Supabase's GitHub provider and enable it. Sign in with the GitHub identity
whose verified email is `tanauancharles1@gmail.com`. The dashboard requests
`user:email` only. Account email addresses do not configure OAuth clients.

## Verification

Inspect `/auth/v1/settings` with the public publishable key; `external.google`
and `external.github` must be true. Test each Google account and the GitHub
account in Android Chrome through the dashboard. Confirm return to this app,
session restoration, and the existing administrator authorization gate.
Successful OAuth alone does not grant administrator access.

## Separation boundaries

| Component | Open-Teleset scope | Shared boundary to preserve |
| --- | --- | --- |
| Google OAuth | Separate consent application and web client named Open-Teleset | The built-in Google provider has one project-wide credential configuration |
| GitHub OAuth | Dedicated app `3918956`; no repository scopes | The built-in GitHub provider has one project-wide credential configuration |
| Dashboard | `open-teleset.site/dashboard`, explicit redirect on every OAuth request | Preserve Open-TGate's existing Site URL and redirect entries |
| Authorization | `operations_shared.project_access.project_key = 'open-teleset'`, plus approved owner/admin role | Existing Supabase users and UUIDs remain canonical |
| Application data | `open_teleset` schema | Preserve shared `profiles`, Auth triggers and other applications' schemas |
| Deployment | Open-Teleset repository, production environment, Pages project, Worker and verified Zeabur service | Separate service/credential scopes; no new paid project implied |

Separate external OAuth registrations do **not** create separate Supabase Auth
tenants. A built-in provider is visible to all apps using the same Auth project.
Before enabling it, coordinate use of the dedicated credentials across that
project. Never replace a populated provider configuration without a migration
and approval record. At the investigation, both built-in providers were disabled
with no client credentials configured.

Supabase also offers named custom OAuth/OIDC providers. These require provider
enablement, different frontend identifiers, and end-to-end validation. They are
not configured by this change. Do not point a generic GitHub OAuth provider at
`/user` and assume it proves a private email: the built-in provider additionally
reads `/user/emails` and honors its verified flag. If fully independent Auth
tenants are required, plan user/identity and authorization migration separately;
do not silently create a new project or duplicate owner records.

## Current setup evidence (2026-10-10)

The dedicated GitHub application was created successfully. Client-secret
generation stopped at GitHub's account-verification screen; no secret was
generated or enabled. Supabase dashboard sign-in did not complete. The Google
Cloud console was unavailable in this browser, so no separate Google client
was created. Both built-in providers remain disabled. Callback allowlist
entries are configured and the existing password identity was repaired without
changing the owner's UUID or project roles.

This is a setup record, not an OAuth completion claim. Acceptance requires a
real sign-in for both Google accounts and the GitHub account, a restored session,
the existing owner/admin access checks, and production readiness. See
[the deployment runbook](DEPLOYMENT_RUNBOOK.md) for runtime and publication gates.

Official provider references:

- <https://supabase.com/docs/guides/auth/social-login/auth-google>
- <https://supabase.com/docs/guides/auth/social-login/auth-github>
- <https://supabase.com/docs/guides/auth/custom-oauth-providers>

Local validation: `node --test tests/*.mjs` and the repository Python tests.
Revert the frontend commit to roll back UI behavior; remove only the three
Open-Teleset redirect entries if rolling back the configuration change.
