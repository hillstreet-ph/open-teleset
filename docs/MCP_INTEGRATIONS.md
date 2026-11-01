# Custom MCP servers

Integrations → **Custom MCP servers** connects the dashboard to an external MCP server —
Open Connect, Composio, Airtable, n8n or your own — over HTTP or SSE. Each entry is one
server, one address and one credential, stored encrypted for the operator who saved it.

Two ways to authenticate, chosen per server with the **Sign-in** selector:

| Sign-in | What you supply | Where it lives |
|---|---|---|
| **Bearer token** | The token the server issued you | Fernet-encrypted operator store, write-only |
| **OAuth login** | Nothing, or a Client ID if the server issued one | Access and refresh tokens in the same encrypted store |

A blank token keeps what is stored; clearing the field deletes it. The page is never given
the value back — it is told whether one is stored.

## OAuth login

For a server that signs people in rather than handing out static tokens. The dashboard
behaves as an OAuth public client, in this order:

1. Asks the MCP server's own origin for `/.well-known/oauth-protected-resource` to find
   its authorization server (falling back to the server itself).
2. Reads that server's authorization, token and registration endpoints.
3. Registers itself as a public client (`token_endpoint_auth_method: none`) unless you
   pasted a Client ID.
4. Sends **your** browser to the authorization server with a PKCE `S256` challenge.
   The verifier stays in this process.
5. The authorization server returns a code to the dashboard, which spends it once, with
   the verifier, directly from the backend.

**Callback address.** The panel prints the exact one for your deployment — production is
`https://open-teleset.site/dashboard`. Register it with the server if it insists on an
issued client. There is only ever one, so a code cannot be spent against a different
address.

**Scopes.** Only the scopes you type are requested. Leave the field empty and the request
carries no `scope` at all, letting the server apply its own default. The server's own list
of supported scopes is shown underneath for reference and is never sent automatically.
After a sign-in the row shows the two separately: what you asked for is the field, what the
server granted is on the row.

**Lifetime.** A sign-in request is valid for ten minutes. A bearer is renewed a minute
before it expires whenever the dashboard needs it, so a scheduled call never races a dead
token; the row shows **Sign-in expiring** inside fifteen minutes. **Renew now** forces it,
**Sign out** drops the tokens and keeps the registration so the next sign-in is one click.

## What never leaves the backend

- No access token, refresh token, client secret or PKCE verifier is returned to the
  browser, written to the JSON store, logged, or put in an error message.
- A code is single-use: the attempt is spent whether the server accepts it or not, and the
  panel takes `code` and `state` out of the address bar before anything else runs, so a
  refresh cannot replay it.
- The state is random, short-lived and matched with a constant-time comparison. A code from
  elsewhere is refused: `That sign-in did not start from this dashboard.`
- Every address the remote servers name — the issuer, the authorization endpoint, the token
  endpoint — is re-validated before it is requested. `http`, loopback, private hosts and
  embedded credentials are refused with `That server pointed its sign-in at an address we
  will not use.` This is the defence against a hostile MCP server aiming your backend at
  your own network.
- If the token exchange fails, the panel gets one fixed sentence
  (`The sign-in server refused these credentials. Sign in again.`) — never the server's own
  error body, which can carry secrets.

Requests stay conservative on purpose: this surface connects to servers you have chosen to
trust. It does not proxy, rotate or disguise anything.

## When a button says no

| Message | Meaning |
|---|---|
| `Switch that server to OAuth sign-in first.` | The entry is still in bearer-token mode. |
| `That server does not advertise an OAuth sign-in we can use.` | No authorization-server metadata at either well-known path. Use a bearer token. |
| `That server does not register its own clients. Add its Client ID first.` | No dynamic registration. Create an OAuth app there, paste its Client ID. |
| `That server pointed its sign-in at an address we will not use.` | Its metadata names an `http`, private or credential-bearing address. |
| `That sign-in expired. Start again.` | More than ten minutes passed on the consent page. |
| `That sign-in did not start from this dashboard. Try again.` | Unrecognised state — an old tab, or a code meant for someone else. |
| `That sign-in can no longer be renewed. Sign in again.` | The server gave no refresh token. Sign in once more. |

## Checks

- `tests/test_mcp_oauth.py` — 22 tests against a mock authorization server: discovery,
  registration as a public client, the scope actually asked for, the verifier, single-use
  codes, renewal, expiry leeway, write-only storage, and no leak in any refusal.
- `tests/test_mcp_oauth_panel.mjs` — 10 tests on the panel's own methods: the address bar
  cleaned at boot, the code spent once, failures surfaced in the list, saves that never
  resend a blank secret.
- `ui_mcp_oauth.mjs` — 43 button-level checks driving the real dashboard against a fake
  authorization server: add → sign in → consent → land → test → renew → a deliberately rude
  token endpoint → sign out → sign in again → rename → mode switch → denied consent → remove.
- `docs/CLOUD_BROWSER.md` documents the rest of the Integrations tab.
