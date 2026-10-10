# Account connection checks

Use **Accounts → Check connection** to reconnect a saved Telegram session and
run the existing authenticated health check. This performs read-only Telegram
requests; it sends no messages and adds no members.

Saved-session connection probes have a 20-second deadline. A Telegram data-center
redirect may repeat the authorization probe once. Flood waits and other failures
are surfaced without retrying. Failed or cancelled attempts disconnect their
client; concurrent health and user requests share one connection attempt.
Successful reconnection saves the current encrypted session so changed
connection information survives restart. Existing outbound consent, suppression,
rate cooldown and zero-retry behavior remains in force.

The account list and sender distinguish expired/revoked sessions, timeouts,
provider cooldowns, temporary server failures, and repeated redirects. An
expired/revoked session needs a new Telegram sign-in; reconnecting cannot restore
revoked authorization. Existing records and encrypted sessions are retained.

The Phone column displays the saved phone number. New account timestamps include
UTC offsets; the dashboard interprets legacy offset-free runtime timestamps as
UTC. Tables scroll within mobile cards rather than widening the entire page.

## Verification

The reconnection regressions use real Telethon RPC dispatch with synthetic
responses. They cover bounded redirect handling, changed session persistence,
unauthorized/server/flood/timeout failures, cancellation, invalid stored sessions,
concurrent callers, health recovery, and QR persistence failure. DC switching is
mocked; these tests do not establish that a real revoked session can recover.

Browser-script tests cover the existing health endpoint button and UTC rendering.
Live UI inspection covers Accounts, Proxies, Health, Stats, Logs, Templates,
Schedule, Scraper, Adder, and BulkSend. Sending and deletion require dedicated
user-authorized test data and are not exercised against real contacts.

Rollback baseline: commit `32cd0f98da3c3075b37c2662afbf11824ed2b172`, image
`hillstreet/open-teleset@sha256:84677ac677719bd667f92d46d16f667bd458d6139e641bafcf7919d9fb2ff87b`.
Retain the existing `/app/accounts` volume and session encryption key. This change
has no schema migration or new credential requirements.
