# Read-only remote MCP

The dashboard serves Streamable HTTP at `https://open-teleset.site/mcp`.
This is a remote endpoint; Android Chrome does not need a local process.

Every MCP request must carry a current Supabase user access token in an
`Authorization: Bearer …` header. The existing dashboard gate verifies the
issuer, audience, expiry, approved global administrator role and explicit
Open-Teleset project administrator assignment. Missing configuration or grants
deny access. A Telegram session, bot token, API key or service-role key is not
a substitute for a user access token. Keep tokens in approved connector secret
storage, never in a skill, prompt, URL or plugin manifest.

The endpoint exposes exactly three read-only tools:

| Tool | Result |
|---|---|
| `authenticated_identity` | Verified user ID and project role |
| `runtime_readiness` | Existing readiness status and database health boolean |
| `account_status_summary` | Total, online and offline account counts |

No phone numbers, Telegram account IDs, messages, contact details, credentials,
session strings or stdio tool catalog are exposed. MCP is stateless; each request
rechecks authorization. There is no dynamic client registration or OAuth server
implemented by this endpoint. Existing Supabase sign-in and token refresh remain
the authentication path. A connector must support bearer authentication and
refresh before this can be advertised as a persistent mobile integration.

Deployment verification requires an immutable runtime image, `/health=200`,
`/readyz=200`, anonymous `/mcp=401`, denied-user `/mcp=403`, and authorized
initialize, tools/list and each tools/call. Test unknown/write tools are denied.
The public Worker must proxy `/mcp` unchanged, including Authorization and
Content-Type. Server-to-server clients omit Origin; browser-origin requests
remain limited to the existing dashboard origin allowlist.

This source change does not fix provider sign-in setup (#25), runtime readiness
(#21), or desktop-only installation controls imposed by ChatGPT. The endpoint
must not be marked connected based only on a deployed route or saved metadata.
Rollback: deploy the previous immutable image; no database migration is needed.
