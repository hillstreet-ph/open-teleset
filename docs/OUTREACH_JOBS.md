# Persistent Member Adder and BulkSend jobs

Both dashboard forms submit a job and return HTTP 202 without waiting for
Telegram. The progress tracker reloads every two seconds and after sign-in.
The server continues an accepted finite queue when the browser closes; it idles
when no eligible work remains. It does not acquire additional recipients.

## Controls and outcomes

- Start submits the explicit recipients. The browser reuses its submission key
  if an HTTP response is lost. Changed input with the same key returns 409;
  inspect the original job before explicitly starting a separate job.
- Pause prevents the next dispatch. Resume retains any outstanding cooldown.
- Stop is terminal. An operation already in flight may finish and is recorded.
- Completed, skipped and failed recipients leave the pending queue; records
  remain in history. Counts cover the full job; the UI displays its latest 100
  recipients. All active jobs and the most recent 30 terminal jobs are listed.
- Duplicate normalized inputs and resolved aliases are skipped within a job.
  Member Adder checks membership before inviting and skips existing members.
- A timed Telegram flood wait pauses the whole job, preserves the assigned
  account and recipient, and resumes only after the stored wait expires.
- Other provider restrictions pause for operator review. Network loss or a
  server error after dispatch, or restart during dispatch, produces
  `needs_review`. That job cannot automatically resume or repeat delivery.
- Single-account and existing round-robin assignment are supported. Recipients
  remain pinned to their assigned sender and verified Telegram user ID. Replacing
  an account under the same application ID pauses the job. An offline account waits; another
  account is never substituted to evade a restriction.

The creator must retain current administrator access. Ownership is enforced on
every job read and control. Consent, suppression and member-add approval checks
remain enforced at the final Telegram RPC boundary, including after any wait.
BulkSend retains its existing operator consent declaration. Resume never renews
consent. The legacy synchronous API routes remain compatible.

Successful queue messages contribute exactly once to the Stats tab through a
transactional completion ledger. Invalid recipients fail individually so later
recipients can continue. Template retries reuse the originally frozen content.

## Runtime and recovery

`outreach_jobs.py` provides one dispatcher started/stopped by the dashboard
lifespan. Its nonblocking OS file lock prevents a second worker on the same
mounted state path. Use the existing one-worker container and persistent
`/app/accounts` volume. This is not a distributed multi-host queue.

`OUTREACH_JOBS_DB` defaults to `./accounts/outreach_jobs.sqlite3`. Recipient,
account and message payloads use the existing versioned `SESSION_ENCRYPTION_KEY`
cipher. Do not change that key or remove the mounted volume during deployment.
Missing keys, unavailable state storage or dispatcher failure reject new jobs.
Back up the database consistently with SQLite's backup API or while stopped;
retain the matching encryption key in the provider secret store.

No Supabase schema or authentication migration is needed. New routes:

- `POST /api/outreach/jobs/personal` and `/members`: existing form payload plus
  `request_id` (16–128 letters, digits, underscores or hyphens).
- `GET /api/outreach/jobs`: owner-scoped job summaries and dispatcher status.
- `GET /api/outreach/jobs/{id}`: owner-scoped progress/history.
- `POST /api/outreach/jobs/{id}/pause`, `/resume`, `/stop`: controls.

## Release and rollback

Run Python, Node, RPC-boundary and container checks before merging. Verify the
state volume, a recoverable backup and the current runtime revision. Stage with
synthetic clients and isolated data; never test by contacting real recipients.
After deployment check health/readiness, anonymous rejection and the authenticated
empty-job listing. Verify that the displayed worker is ready. Do not submit a
live campaign as a deployment test.

Rollback to the recorded prior release while retaining the new queue database.
The previous runtime ignores this additive database. Before resuming with the
new runtime, inspect any pending jobs and `needs_review` entries; never erase
the database to clear a delivery ambiguity.

## Tests

`tests/test_outreach_jobs.py` exercises durable progress, encryption, HTTP
idempotency storage, ownership, restart/Stop/Pause, fixed sender on flood wait,
restriction pause, uncertain delivery, duplicate aliases, existing membership,
mid-run opt-outs, revoked operator access, offline waits and single-worker
locking. Browser tests cover new submissions, timeout retry, stale responses
after logout and edited-input conflicts. All Telegram traffic uses fixtures.
