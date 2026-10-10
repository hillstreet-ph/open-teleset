# Automatic proxy health failover

Open-Teleset keeps a healthy proxy stable. Every five minutes the existing health monitor checks the managed free global proxy. Two consecutive failed checks are required before replacement. Concurrent checks cannot count twice, and check/backoff state survives restart.

A replacement must pass Telegram TCP connectivity and a certificate-verified TLS request. Discovery tests at most 24 candidates, four at a time, from the existing fixed public sources. If no replacement works or discovery fails, the old configuration remains and another discovery attempt waits 15 minutes. No automatic direct-connection fallback occurs. Proxy configuration is replaced atomically on disk with private file permissions.

Existing live Telegram clients stay connected. On a later disconnected-session connection check, the existing per-account shared connection task closes the old client and selects the configured proxy. No sent or uncertain messages are replayed. A working connection is not disconnected merely to change IP addresses.

Manual global proxies and explicit account proxy assignments take priority. Automatic replacement applies only to the managed free global proxy. Removing that proxy disables free discovery; the existing **Find free global proxy** action enables discovery again. Administrators can disable automatic maintenance by setting `auto_failover_enabled` to false in the mounted `accounts/proxies.json` and restarting. `/api/proxies` exposes `maintenance.status`, `last_check`, `next_check`, `consecutive_failures`, and the last failover time to authenticated administrators. No additional provider subscription is created.

Telegram rate limits, spam restrictions, bans, and revoked sessions never trigger proxy replacement. Outbound provider cooldowns remain persisted; health monitoring skips accounts in a current cooldown and persists FloodWait errors raised by its read-only profile probe and saved-session reconnect. A successful network probe does not prove that an account is unrestricted. Follow Telegram's wait duration and use its official appeal process for incorrect restrictions.

No proxy or sending rate guarantees that Telegram will not restrict an account. Free proxies can be unreliable and offer no guaranteed availability or exclusive IP reputation. Contact recipients who expect the messages and honor opt-outs. See Telegram's [Spam FAQ](https://telegram.org/faq_spam) and [API error handling](https://core.telegram.org/api/errors).

This change does not increase bulk-send throughput or change the user's current optional delay/cap. No real Telegram messages are sent by proxy tests. Roll back to the previous immutable source/image while retaining the accounts volume and consent/cooldown stores; older versions ignore the added maintenance metadata.
