import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

function methodSource(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = html.indexOf(endMarker, start);
  assert.ok(end > start, `missing end marker after ${startMarker}`);
  return html.slice(start, end);
}

function harness() {
  const calls = [];
  const context = {
    _api: { post: async (url, body) => {
      calls.push({ url, body });
      return { data: { id: 'job-1', kind: 'auto_join', state: 'queued', retry_after: 0 } };
    } },
  };
  const methods = vm.runInNewContext(
    `({${methodSource('async controlOutreachJob(job, action) {', '// ---- Webhooks ----')}})`, context);
  const state = {
    accessReady: true,
    authRevision: 1,
    outreachJobBusy: '',
    outreachJobError: '',
    outreachJobs: [{ id: 'job-1', kind: 'auto_join', state: 'paused' }],
  };
  state.controlOutreachJob = methods.controlOutreachJob.bind(state);
  return { state, calls };
}

test('recheck posts to the dedicated route and swaps in the new snapshot', async () => {
  const { state, calls } = harness();
  await state.controlOutreachJob(state.outreachJobs[0], 'recheck');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/outreach/jobs/job-1/recheck');
  assert.equal(state.outreachJobs[0].state, 'queued');
  assert.equal(state.outreachJobBusy, '');
});

test('both job cards offer Recheck now alongside pause, resume and stop', () => {
  const recheckButtons = html.match(/controlOutreachJob\(job, 'recheck'\)/g) || [];
  assert.equal(recheckButtons.length, 2, 'overview and progress cards must both wire Recheck');
  assert.match(html, />Recheck now</);
});

test('the channel-limit copy names a permanent condition, not a countdown', () => {
  assert.match(html, /channel limit is a permanent condition, not a timer/);
  assert.ok(!html.includes("This account reached Telegram's join limit"),
    'the fake one-day join-limit countdown copy must be gone');
});

test('job cards name each account state when the job rotates', () => {
  const accountLines = html.match(/job\.accounts && job\.accounts\.length > 1/g) || [];
  assert.equal(accountLines.length, 2, 'both job cards must show per-account states');
  assert.match(html, /entry\.state === 'blocked' \? outreachReason\(entry\.reason\)/);
  assert.match(html, /'free in ' \+ outreachDuration\(entry\.retry_after\)/);
});
