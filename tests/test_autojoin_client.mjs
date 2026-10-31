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
  const state = {
    accessReady: true,
    authRevision: 1,
    autojoin: {
      requestId: '', rotationMode: 'single', accountId: 'acct', accountIds: [],
      urls: 'https://t.me/chanone\nhttps://t.me/chantwo', delay: 45, limit: 20,
      loading: false, resultMsg: '',
    },
    get autojoinLinks() {
      return [...new Set(this.autojoin.urls.split(/\r\n|\r|\n/).map(l => l.trim()).filter(Boolean))];
    },
    outreachJobs: [],
    loadOutreachJobs: async () => {},
  };
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
  assert.match(html, /<details v-for="job in progressJobs"/);
  assert.match(html, /jobCountLabel\(job\)/);
  assert.match(html, /\[['"]adder['"], ['"]bulksend['"], ['"]autojoin['"]\]\.includes\(tab\)/);
});

test('scraper and adder expose the detected group pickers', () => {
  assert.match(html, /loadGroups\('scraper'\)/);
  assert.match(html, /loadGroups\('adder'\)/);
  assert.match(html, /pickGroup\(\$event, 'adder'\)/);
  assert.match(html, /\/api\/channels\/groups/);
});
