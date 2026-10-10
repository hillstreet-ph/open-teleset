# BulkSend settings

The Bulk Personal Sender no longer requires a manual approval reference. Its recipient list and selected-account list have no application count cap. Set the per-account cap to **0** for unlimited, or a positive integer to limit this run. Delay accepts any finite nonnegative number, including **0**. Telegram restrictions still stop the batch and persist cooldowns; another account is never used to retry a restricted recipient.

For recipients who have consented, select **I confirm these recipients consented to messages from the selected accounts**. The authenticated project administrator's declaration is recorded in the existing mounted outreach policy store, using hashed identifiers and a 30-day expiry. The store is created on the first confirmed run. Existing opt-outs are preserved and enforced; a damaged policy store is never overwritten. Without confirmation, existing current consent records are required. No approval is manufactured and no message is sent merely by importing a CSV.

CSV has no recipient count cap for BulkSend. The 2 MB file-size safeguard and Telegram's 4,096-character message validation remain. Large synchronous batches can still encounter infrastructure request timeouts; inspect results/logs before retrying, since a disconnected browser does not prove a send failed.

Free global proxy discovery is enabled by default. It tries the hourly checked, response-time-sorted public SOCKS5 list at https://github.com/monosans/proxy-list, falling back to the existing Proxifly source when unavailable. Candidates must pass both Telegram TCP reachability and a verified TLS request before being saved. Existing manual and per-account proxies take priority. Removing the global proxy disables discovery; use **Find free global proxy** to enable it again. A free proxy's future availability is not guaranteed.

Other workflows (Adder, Scheduler, legacy batch tools) retain their own approval and pacing policies. No database schema, project permissions, or credentials change in this update. Rollback is the preceding immutable commit/image; retain the accounts volume and consent/opt-out records.
