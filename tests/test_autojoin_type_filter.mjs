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

function harness(groupType) {
  const calls = [];
  const context = {
    crypto: { randomUUID: () => 'uuid-1' },
    _api: { post: async (url, body) => {
      calls.push({ url, body });
      return { data: { id: 'job-1', kind: 'auto_join', state: 'queued', total: 1, skipped: 0 } };
    } },
  };
  const methods = vm.runInNewContext(
    `({${methodSource('async runAutoJoin() {', '// ---- BulkSend ----')}})`, context);
  const state = {
    accessReady: true,
    authRevision: 1,
    autojoinValidLinks: ['https://t.me/examplechan'],
    outreachJobs: [],
    autojoin: {
      requestId: '', rotationMode: 'single', accountId: 'ui-one', accountIds: [],
      urls: 'https://t.me/examplechan', groupType, delay: 38, limit: 0,
      loading: false, resultMsg: '',
    },
    loadOutreachJobs: async () => {},
    apiErrorText: (e, fallback) => fallback,
  };
  state.runAutoJoin = methods.runAutoJoin.bind(state);
  return { state, calls };
}

test('the dashboard sends the chosen type with the join job', async () => {
  for (const value of ['all', 'channel', 'group']) {
    const { state, calls } = harness(value);
    await state.runAutoJoin();
    assert.equal(calls.length, 1, `${value}: one submission`);
    assert.equal(calls[0].url, '/api/join/jobs');
    assert.equal(calls[0].body.group_type, value);
  }
});

test('the Auto Join card offers All, Channels and Groups', () => {
  const select = html.match(/<select[^>]*v-model="autojoin\.groupType"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(select, 'the type select is missing from the Auto Join card');
  const values = [...select[1].matchAll(/value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(values, ['all', 'channel', 'group']);
  assert.match(select[1], /Channels only/);
  assert.match(select[1], /Groups only/);
});

test('groupType starts at all so nothing changes for existing operators', () => {
  assert.match(html, /groupType: 'all',/);
});

test('a skipped link explains which kind mismatch happened', () => {
  assert.match(html, /target_type_mismatch: 'This link is the other kind of chat than "Join only" allows — skipped'/);
  assert.match(html, /target_type_unknown: 'Telegram did not say whether this link is a channel or a group — skipped'/);
});
