# Account login, proxies and Hex session import

Issue #60 adds a backend path for a Telegram user authorization key represented
as 512 hexadecimal characters (256 bytes), together with its original DC ID
(1–5). Whitespace from line wrapping is accepted. Labels, partial keys,
non-hexadecimal characters, zero keys and invalid DC IDs are rejected before
connecting. Production bootstrap addresses come from Telegram Desktop's
`mtproto_dc_options.cpp`; caller-supplied server addresses are not accepted by
this conversion path.

The converter uses Telethon's `StringSession`, `set_dc` and `AuthKey` APIs. It
never writes a raw hex key to disk. Verification uses the existing saved-session
migration guard and a 20-second limit; a mismatched DC does not trigger an
unauthorized replacement key. Every temporary client is disconnected, including
on cancellation. A successful read-only authorization and identity check precedes
the existing encrypted, atomic configuration save. Failed saves roll back the
new in-memory account. Existing IDs and duplicate Telegram identities are
rejected. Existing accounts are not overwritten.

`AccountManager.import_hex_session(account_id, hex_key, dc_id, proxy=None)`
returns a success/account-ID result or a sanitized error. The existing boolean
`add_account_with_session` entry point delegates to the same verified import
implementation. Telegram supplies identity fields; caller-supplied metadata
cannot override them. The importer rejects bot sessions.

## Dashboard and API

Add account supports QR, international phone numbers and Hex session import.
Hex session import requires the original DC (1–5), a masked 512-character key,
and an authenticated administrator. Only import sessions you own or are
explicitly authorized to manage. The key clears on success, close or sign-out.
`POST /api/accounts/add-hex-session` accepts `account_id`, `hex_key`, `dc_id`,
`proxy_mode` and an optional `proxy_id`; the browser timeout exceeds the bounded
backend verification and cleanup. StringSession imports use the same verifier.

Every login offers Global default, Direct connection, or a saved proxy.
Direct explicitly bypasses the global proxy. The choice is persisted with the
account and used on reconnect. Global default tracks the configured global
proxy; if none exists it uses direct networking. Legacy accounts retain their
existing per-account assignment until their connection is changed.
The account table allows changing this preference; saving disconnects the old
client, and Check connection verifies the new route. A deleted selected proxy
fails closed until the user chooses an available connection.

Supported proxy protocols are SOCKS5, SOCKS4 and HTTP CONNECT. HTTPS transport
is not supported by the Telegram client. Test a proxy before use. Public free
proxy discovery is best-effort and can report no available proxy; it cannot
ensure Telegram reachability. Removing the global proxy disables discovery.
No account is silently rotated to bypass a Telegram restriction.

Phone/QR connect, authentication RPCs and temporary-client cleanup have finite
timeouts. Duplicate phone/QR starts for the same ID are rejected. Cancellation
invalidates the old session so its completion cannot overwrite a retry. An
uncertain code-request response is not automatically retried; check Telegram
before retrying manually. Telegram controls delivery, 2FA and flood waits.

## Per-account Telegram app credentials

Every sign-in form (QR, phone and Hex import) accepts an optional **App (API) ID**
and **API hash**. Use the pair from your own Telegram application so the
authorization stays a recognized login instead of a new app session; leave both
blank to use the server default (`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`).

- Both fields are validated together: one without the other, a non-numeric App
  ID, or a hash that is not exactly 32 hexadecimal characters is refused before
  Telegram is contacted, and the message stays in the form where it can be fixed.
- A saved pair is stored with the account and its hash is encrypted by the same
  cipher as the session string (`api_hash_encrypted` in `accounts/config.json`).
  The list API exposes only the App ID and whether the account has its own app.
- Reconnect, contact listing, session listing and re-login use the account's own
  pair when present. A re-login that omits credentials reuses the saved pair
  instead of falling back to the server default, so restoring a logged-out
  account keeps the same recognized app.
- App ID and hash are identifiers for the application, not a login password.
  They are still configuration: do not paste them into issues, commits or chat.

## Hex session export

Each account can export its authorization key as 512 hexadecimal characters
together with its DC and App ID (**Export** on the account row). Paste that key
with the same DC into **Import Hex session** to restore the account elsewhere.

`GET /api/accounts/{account_id}/export-hex-session` returns
`hex_key`, `dc_id` and `api_id`. It is administrator-only and returns `404` when
the account or session is missing. The key is returned only to the requesting
browser; it is not written into logs, Airtable or another store. The dashboard
shows it in the export dialog with the string session folded away by default,
and copies nothing until the operator presses **Copy hex**.

Treat the exported key as a password: anyone holding it and the DC can act as
that account. Keep backups private, and re-import only on connections you
control.

## Verification and rollback

Tests use a synthetic byte sequence only; they make no Telegram connections.
Real account acceptance requires an authorized user to import their own session
through the completed UI. No session from an attached screenshot is a test fixture.
This change adds no schema, credentials, services or volume changes.
Rollback is a code revert retaining the existing encrypted accounts volume and
matching encryption key.
