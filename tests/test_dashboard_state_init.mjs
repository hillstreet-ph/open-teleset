// Regression: every rotation-capable form must initialise rotationMode and
// accountIds in data(). The Adder tab is v-show (always rendered), so a missing
// `adder.accountIds` made `adder.accountIds.length` throw on every render and
// blanked the whole dashboard.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const appStart = html.indexOf('const app = createApp({');
const start = html.indexOf('data() {', appStart);
const end = html.indexOf('computed: {', start);
const dataFn = vm.runInNewContext(`({${html.slice(start, end).replace(/,\s*$/, '')}})`, {
  window: {}, localStorage: { getItem: () => null }, location: { search: '', hash: '' },
  APP_CONFIG: {}, Date, Math, JSON, URLSearchParams,
}).data;

test('rotation forms initialise rotationMode and accountIds', () => {
  const state = dataFn.call({});
  for (const form of ['adder', 'bulksend', 'autojoin']) {
    assert.ok(state[form], `${form} state missing`);
    assert.equal(state[form].rotationMode, 'single', `${form}.rotationMode`);
    assert.ok(Array.isArray(state[form].accountIds), `${form}.accountIds must be an array`);
  }
});

test('every <form>.accountIds referenced in the template exists in data()', () => {
  const state = dataFn.call({});
  const refs = new Set([...html.matchAll(/\b(\w+)\.accountIds\b/g)].map(m => m[1]))
  refs.delete('this');
  for (const form of refs) {
    if (!(form in state)) continue; // local variables, not component state
    assert.ok(Array.isArray(state[form].accountIds), `${form}.accountIds is referenced but not initialised`);
  }
});

test('opening the Add account dialog does not throw', () => {
  const body = html.slice(html.indexOf('openQrModal() {'), html.indexOf('startQrLogin() {'));
  const openQrModal = vm.runInNewContext(`({${body}})`).openQrModal;
  const state = { loadProxies() {} };
  assert.doesNotThrow(() => openQrModal.call(state));
  assert.equal(state.showQrModal, true);
  assert.equal(state.qrRemaining, 120);
});

test('every Copilot ops destination exposes its declared gate', () => {
  for (const gate of ['scraper', 'adder', 'bulksend', 'autojoin', 'autoreply', 'factory', 'automation', 'knowledge', 'inbox']) {
    assert.ok(html.includes(`gate-${gate}`), `missing gate-${gate}`);
  }
});

test('knowledge export downloads the authenticated zip response', async () => {  const start = html.indexOf('async exportKnowledge() {');
  const end = html.indexOf('// ---- Repeated wording ----', start);
  assert.ok(start >= 0 && end > start, 'exportKnowledge method missing');
  const calls = [];
  const link = { click() { this.clicked = true; } };
  const context = {
    _api: { get: async (url, config) => { calls.push({ url, config }); return { data: new Blob(['zip']) }; } },
    URL: { createObjectURL: () => 'blob:knowledge', revokeObjectURL: () => {} },
    document: { createElement: () => link },
    setTimeout: fn => fn(),
  };
  const { exportKnowledge } = vm.runInNewContext(`({${html.slice(start, end)}})`, context);
  const state = { knowledge: { busy: '' }, showToast() {} };
  await exportKnowledge.call(state);
  assert.equal(calls[0].url, '/api/knowledge/export');
  assert.equal(calls[0].config.responseType, 'blob');
  assert.match(link.download, /^knowledge-.*\.zip$/);
  assert.equal(link.clicked, true);
  assert.equal(state.knowledge.busy, '');
});

// Regression: long record lists must render a bounded first page so the operator
// never scrolls through hundreds of rows. Tables slice with visibleRows();
// everything else uses <bounded-list> / <record-list :limit>.
const rowHelpers = vm.runInNewContext(`({${html.slice(
  html.indexOf('visibleRows(list, key, limit) {'),
  html.indexOf('jobCountLabel(job) {', html.indexOf('visibleRows(list, key, limit) {'))
)}})`);

test('table lists start bounded and only expand on an operator click', () => {
  const { visibleRows, toggleRows } = rowHelpers;
  const rows = Array.from({ length: 30 }, (_, i) => i);
  const state = { expandedRows: {} };
  assert.equal(visibleRows.call(state, rows, 'riskAccounts', 8)[0], 0);
  assert.equal(visibleRows.call(state, rows, 'riskAccounts', 8).length, 8);
  toggleRows.call(state, 'riskAccounts');
  assert.equal(visibleRows.call(state, rows, 'riskAccounts', 8).length, 30);
  toggleRows.call(state, 'riskAccounts');
  assert.equal(visibleRows.call(state, rows, 'riskAccounts', 8).length, 8);
  assert.equal(visibleRows.call(state, null, 'empty', 8).length, 0);
});

test('expandedRows is initialised so toggles stay reactive', () => {
  const state = dataFn.call({});
  assert.equal(typeof state.expandedRows, 'object');
  assert.equal(Object.keys(state.expandedRows).length, 0);
});

test('every long record list declares a bound', () => {
  assert.ok((html.match(/<bounded-list/g) || []).length >= 20, 'too few bounded lists');
  for (const list of ['automation.status.runs', 'riskAccounts', 'topAccounts', 'autoReply.config.activity']) {
    assert.ok(html.includes(`visibleRows(${list}`), `${list} table renders unbounded`);
  }
  for (const list of ['outreachJobs', 'progressJobs']) {
    assert.ok(new RegExp(`:items="${list}"[\\s\\S]{0,500}:limit=`).test(html), `${list} record list has no limit`);
  }
  assert.ok(html.includes('<bounded-list v-else :items="schedules"'), 'overview schedules are unbounded');
});
