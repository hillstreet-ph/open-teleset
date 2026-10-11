# Hex session import (backend preparation)

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

## Integration dependency

The Add account UI and authenticated API integration are not included yet.
Issue #59 currently owns `dashboard.py` and `static/dashboard.html`. Once that
lock is released, rebase and extend the existing Add account modal with a Hex
session choice, explicit DC selection, masked key field, consent/ownership help,
validation, busy state, safe errors and secret clearing on close/success. Reuse
existing proxy selection/assignment and authenticated administrator gates. Use a
request timeout longer than the backend verification plus cleanup limit. Add UI
and API regression coverage before making this feature available or deploying.
Do not advertise the option as live from this backend preparation alone.

## Verification and rollback

Tests use a synthetic byte sequence only; they make no Telegram connections.
Real account acceptance requires an authorized user to import their own session
through the completed UI. No session from an attached screenshot is a test fixture.
This preparation adds no schema, credentials, services or volume changes.
Rollback is a code revert retaining the existing encrypted accounts volume and
matching encryption key.
