// Per-recipient send receipts: the durable job cards must show which account
// sent to which username, with a kind-aware status, and explain a Telegram
// cooldown without promising that Pause/Resume can shorten it.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

const receiptHelpers = vm.runInNewContext(
  `({${html.slice(html.indexOf('jobAccountLabel(id) {'), html.indexOf('outreachReason(reason) {'))}})`);
const accountHelpers = vm.runInNewContext(
  `({${html.slice(html.indexOf('accountDisplayName(acc) {'), html.indexOf('async loadAccountPhoto(id) {'))}})`);

function context(accounts) {
  return { accounts, ...receiptHelpers, ...accountHelpers };
}

test('jobAccountLabel resolves a loaded account to its display name', () => {
  const ctx = context([{ account_id: 'abc123', first_name: 'Ada', last_name: 'Lovelace' }]);
  assert.equal(ctx.jobAccountLabel('abc123'), 'Ada Lovelace');
});

test('jobAccountLabel falls back to a short id for an unknown account', () => {
  const ctx = context([]);
  assert.equal(ctx.jobAccountLabel('abcdefghijklmnopqrst'), 'abcdefgh…qrst');
  assert.equal(ctx.jobAccountLabel('short'), 'short');
});

test('jobResultState uses a kind-aware verb for completed work', () => {
  const ctx = context([]);
  assert.equal(ctx.jobResultState({ kind: 'personal_send' }, 'completed'), 'Sent');
  assert.equal(ctx.jobResultState({ kind: 'member_add' }, 'completed'), 'Added');
  assert.equal(ctx.jobResultState({ kind: 'auto_join' }, 'completed'), 'Joined');
});

test('jobResultState labels every non-terminal and failure state', () => {
  const ctx = context([]);
  for (const [state, label] of [['pending', 'Pending'], ['sending', 'Sending'],
    ['skipped', 'Skipped'], ['failed', 'Failed'], ['uncertain', 'Uncertain']]) {
    assert.equal(ctx.jobResultState({ kind: 'personal_send' }, state), label);
  }
});

test('both durable job cards render account → target receipts', () => {
  const cards = html.match(/<job-history\b/g) || [];
  assert.ok(cards.length >= 3, 'Overview, Progress tracker and the schedule card must both show receipts');
  assert.match(html, /<td>\{\{ account\(row\.account_id\) \}\}<\/td>/);
  assert.match(html, /<td style="overflow-wrap:anywhere;">\{\{ row\.target \}\}<\/td>/);
  assert.match(html, /resultState\(job, row\.state\)/);
});

test('the Tasks outreach panel explains rotation and a whole-job Telegram cooldown', () => {
  const overview = html.slice(html.indexOf('<h3>Outreach jobs</h3>'), html.indexOf('<!-- ============ Automation ============ -->'));
  assert.match(overview, /<job-history :job="job" :account="jobAccountLabel"/);
  assert.match(overview, /switches to the next eligible account automatically/);
  assert.match(overview, /cannot be shortened by Pause, Resume or a new job/);
  assert.match(overview, /stale wait is re-read from the live limit records/);
  assert.match(overview, /Assigned account: \{\{ jobAccountLabel\(job\.next_account_id\) \}\}/);
});
