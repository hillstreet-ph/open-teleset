// Member transfer: read a source group's members into the recipient list.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/dashboard.html', import.meta.url), 'utf8');

function slice() {
  const start = html.indexOf('async loadTransferMembers() {');
  const end = html.indexOf('async runAdder() {');
  assert.ok(start > 0 && end > start, 'loadTransferMembers sits before runAdder');
  return html.slice(start, end);
}

function state(overrides = {}) {
  return {
    authRevision: 1,
    accessReady: true,
    transfer: { source: 'https://t.me/sourcegroup', limit: 100, loading: false, summary: '', notice: '' },
    adder: {
      rotationMode: 'single', accountId: 'ui-one', accountIds: [],
      usernames: 'stale-one\nstale-two', consentConfirmed: true,
    },
    ...overrides,
  };
}

function bind(target, post) {
  const method = vm.runInNewContext(`({${slice()}}).loadTransferMembers`,
    { _api: { post }, console });
  return method.bind(target);
}

test('the transfer reads the source with the selected adder account', async () => {
  const calls = [];
  const target = state();
  await bind(target, async (url, body) => {
    calls.push({ url, body });
    return { data: { members: [{ id: 1, username: 'ana' }, { id: 2, username: 'ben' }] } };
  })();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/scraper/members');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),
    { account_id: 'ui-one', target: 'https://t.me/sourcegroup', limit: 100, filter_bots: true });
  assert.equal(target.adder.usernames, '@ana\n@ben');
  assert.match(target.transfer.summary, /Loaded 2 recipients/);
  assert.equal(target.transfer.loading, false);
});

test('transferring a new list drops the previous consent', async () => {
  const target = state();
  await bind(target, async () => ({ data: { members: [{ id: 7, username: 'cleo' }] } }))();
  assert.equal(target.adder.consentConfirmed, false);
  assert.match(target.transfer.summary, /confirm consent/i);
});

test('a member without a username is carried by numeric id', async () => {
  const target = state();
  await bind(target, async () => ({ data: { members: [
    { id: 11, username: null }, { id: 12, username: 'dee' }, { id: 13, username: 'Dee' },
  ] } }))();
  assert.equal(target.adder.usernames, '11\n@dee');
  assert.match(target.transfer.summary, /1 have no username|1 has no username/);
});

test('a hidden member list is reported as recent posters', async () => {
  const target = state();
  await bind(target, async () => ({ data: { members: [{ id: 1, username: 'ana' }],
                                          limited: true, notice: 'Non-admins only see recent posters.' } }))();
  assert.equal(target.transfer.notice, 'Non-admins only see recent posters.');
});

test('a failed read names the reason and stops the spinner', async () => {
  const target = state();
  await bind(target, async () => {
    const error = new Error('bad');
    error.response = { data: { detail: 'Could not find the source group.' } };
    throw error;
  })();
  assert.equal(target.transfer.summary, 'Could not find the source group.');
  assert.equal(target.transfer.loading, false);
});

test('no read is attempted without an account or a source', async () => {
  let called = false;
  const noSource = state({ transfer: { source: '   ', limit: 100, loading: false, summary: '', notice: '' } });
  await bind(noSource, async () => { called = true; return { data: {} }; })();
  assert.equal(called, false);

  const noAccount = state({ adder: { rotationMode: 'single', accountId: '', accountIds: [], usernames: '' } });
  await bind(noAccount, async () => { called = true; return { data: {} }; })();
  assert.equal(called, false);
});

test('the transfer control is in the adder tab with an explicit load button', () => {
  const adder = html.slice(html.indexOf("tab === 'adder'"), html.indexOf("tab === 'bulksend'"));
  assert.match(adder, /v-model="transfer\.source"/);
  assert.match(adder, /id="transfer-load"/);
  assert.match(adder, /@click="loadTransferMembers"/);
  assert.match(adder, /Reading does not consent/);
});
