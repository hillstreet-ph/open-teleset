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
      return { data: { id: 'job-1', kind: 'auto_join', state: 'queued', total: 2, skipped: 0, pending: 2 } };
    } },
    crypto: { randomUUID: () => 'request-key-1234567890' },
  };
  const methods = vm.runInNewContext(`({${methodSource('async runAutoJoin() {', '// ---- BulkSend ----')}})`, context);
  const computed = vm.runInNewContext(`({${methodSource('autojoinLinks() {', 'progressJobs() {')}})`, {});
  const state = {
    apiErrorText: (error, fallback) => error?.message || fallback,
    accessReady: true,
    authRevision: 1,
    autojoin: {
      requestId: '', rotationMode: 'single', accountId: 'acct', accountIds: [],
      urls: 'https://t.me/chanone\nhttps://t.me/chantwo', delay: 45, limit: 20,
      loading: false, resultMsg: '',
    },
    outreachJobs: [],
    loadOutreachJobs: async () => {},
  };
  // Bind the real computed properties so runAutoJoin sees autojoinValidLinks.
  for (const name of ['autojoinLinks', 'autojoinValidLinks']) {
    Object.defineProperty(state, name, { get: () => computed[name].call(state) });
  }
  state.runAutoJoin = methods.runAutoJoin.bind(state);
  return { state, calls };
}

test('auto join queues a durable job instead of the synchronous join endpoint', async () => {
  const { state, calls } = harness();
  await state.runAutoJoin();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/join/jobs');
  assert.ok(!html.includes("_api.post('/api/channels/join'"), 'synchronous join call must be gone');
  assert.equal(calls[0].body.account_id, 'acct');
  assert.deepEqual(calls[0].body.urls, ['https://t.me/chanone', 'https://t.me/chantwo']);
  assert.equal(calls[0].body.request_id, 'request-key-1234567890');
  assert.equal(state.autojoin.requestId, '');
  assert.equal(state.autojoin.loading, false);
  assert.match(state.autojoin.resultMsg, /Queued 2 links/);
  assert.match(state.autojoin.resultMsg, /background/i);
  assert.equal(state.outreachJobs[0].id, 'job-1');
});

test('progress tracker renders dates, counts and collapsible job cards', () => {
  assert.match(html, /jobStamp\(job\.created\)/);
  assert.match(html, /jobStamp\(job\.started\)/);
  assert.match(html, /jobStamp\(job\.finished\)/);
  assert.match(html, /<record-list :items="progressJobs"/);
  assert.match(html, /:open-when="jobIsActive"/);
  assert.match(html, /jobCountLabel\(job\)/);
  assert.match(html, /\[['"]adder['"], ['"]bulksend['"], ['"]autojoin['"]\]\.includes\(tab\)/);
});

test('scraper and adder expose the detected group pickers', () => {
  assert.match(html, /loadGroups\('scraper'\)/);
  assert.match(html, /loadGroups\('adder'\)/);
  assert.match(html, /pickGroup\(\$event, 'adder'\)/);
  assert.match(html, /\/api\/channels\/groups/);
});

test('auto join has no per-run cap and zero queues every link', async () => {
  const { state, calls } = harness();
  state.autojoin.limit = 0;
  await state.runAutoJoin();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.limit, 0);
  assert.deepEqual(calls[0].body.urls, ['https://t.me/chanone', 'https://t.me/chantwo']);
  assert.match(html, /v-model\.number="autojoin\.limit" min="0"/);
  assert.ok(!/max="2000"/.test(html), 'the per-run ceiling must be gone');
  assert.ok(!html.includes('from 1 to 50'), 'the old fifty-link cap must be gone');
  assert.match(html, /limit: 0,/, 'the dashboard must default to no per-run cap');
});

test('unrecognised lines are filtered before queuing', async () => {
  const { state, calls } = harness();
  state.autojoin.urls = 'https://t.me/chanone\nnot a link!!\n\nhttps://t.me/chantwo';
  assert.equal(state.autojoinLinks.length, 3);
  assert.equal(state.autojoinValidLinks.length, 2);
  await state.runAutoJoin();
  assert.deepEqual(calls[0].body.urls, ['https://t.me/chanone', 'https://t.me/chantwo']);
});

test('a negative per-run cap is refused before it reaches the server', async () => {
  const { state, calls } = harness();
  state.autojoin.limit = -1;
  await state.runAutoJoin();
  assert.equal(calls.length, 0);
  assert.match(state.autojoin.resultMsg, /0 to join every link/);
});
