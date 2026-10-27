// Regression: every rotation-capable form must initialise rotationMode and
// accountIds in data(). The Adder tab is v-show (always rendered), so a missing
// `adder.accountIds` made `adder.accountIds.length` throw on every render and
// blanked the whole dashboard.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const start = html.indexOf('data() {');
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
