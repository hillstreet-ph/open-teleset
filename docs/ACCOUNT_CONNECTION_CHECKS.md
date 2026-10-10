# Account connection checks

Use **Accounts → Check connection** to reconnect a saved Telegram session and
run the existing authenticated health check. This performs read-only Telegram
requests; it sends no messages and adds no members.

Saved-session connection probes have a 20-second deadline, using a timeout
supported by Python 3.10. The fixed read-only authorization probe does not follow
data-center redirects: Telethon's generic redirect handler clears the original
authorization key. A redirect instead requests a fresh sign-in while preserving
the stored session. Flood waits and other failures are surfaced without retrying.
Failed attempts disconnect their client; concurrent health and user requests
share both success and failure results. Cancelling one waiter does not cancel
another waiter's bounded probe. Successful reconnection saves the encrypted
session. Existing outbound consent, suppression,
rate cooldown and zero-retry behavior remains in force.

The account list and sender distinguish expired/revoked sessions, timeouts,
provider cooldowns, temporary server failures, and data-center redirects. An
expired/revoked session needs a new Telegram sign-in; reconnecting cannot restore
revoked authorization. Existing records and encrypted sessions are retained.

The Phone column displays the saved phone number. New account timestamps include
UTC offsets; the dashboard interprets legacy offset-free runtime timestamps as
UTC. Tables scroll within mobile cards rather than widening the entire page.

## Verification

The reconnection regressions use real Telethon clients with synthetic sender
responses. They cover preservation of authorization on redirects, session persistence,
unauthorized/server/flood/timeout failures, cancellation, invalid stored sessions,
concurrent successful/failed callers, health client eviction, and QR persistence
failure through the status endpoint and polling UI. Redirect tests assert that
the saved-client migration guard preserves the original authorization, including
during Telethon connection bootstrap. These tests do not establish that a revoked
session can recover.

Browser-script tests cover the existing health endpoint button and UTC rendering.
Live UI inspection covers Accounts, Proxies, Health, Stats, Logs, Templates,
Schedule, Scraper, Adder, and BulkSend. Sending and deletion require dedicated
user-authorized test data and are not exercised against real contacts.

Rollback baseline: commit `32cd0f98da3c3075b37c2662afbf11824ed2b172`, image
`hillstreet/open-teleset@sha256:84677ac677719bd667f92d46d16f667bd458d6139e641bafcf7919d9fb2ff87b`.
Retain the existing `/app/accounts` volume and session encryption key. This change
has no schema migration or new credential requirements.
