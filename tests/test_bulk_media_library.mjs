import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const start = html.indexOf('async loadBulkMedia() {');
const end = html.indexOf('// ---- Automation ----');
const sandbox = { _api: null };
const methods = vm.runInNewContext(`({${html.slice(start, end)}})`, sandbox);

function harness(api) {
  sandbox._api = api;
  return {
    bulksend: { media: [], library: [] },
    toasts: [],
    showToast(message, isError) { this.toasts.push({ message, isError }); },
    loadBulkMedia: methods.loadBulkMedia,
    reuseBulkMedia: methods.reuseBulkMedia,
    removeBulkMedia: methods.removeBulkMedia,
  };
}

test('saved uploads survive a reload and stay selectable', async () => {
  const state = harness({ get: async () => ({ data: { media: [
    { id: 'a', name: 'one.png' }, { id: 'b', name: 'two.pdf' },
  ] } }) });
  await state.loadBulkMedia();
  assert.deepEqual(Array.from(state.bulksend.library, item => item.id), ['a', 'b']);
});

test('a file already attached for this send is not offered twice', async () => {
  const state = harness({ get: async () => ({ data: { media: [
    { id: 'a' }, { id: 'b' },
  ] } }) });
  state.bulksend.media = [{ id: 'a' }];
  await state.loadBulkMedia();
  assert.deepEqual(Array.from(state.bulksend.library, item => item.id), ['b']);
});

test('attaching a saved file moves it out of the saved list', () => {
  const state = harness({});
  state.bulksend.library = [{ id: 'a' }, { id: 'b' }];
  state.reuseBulkMedia({ id: 'a' });
  assert.deepEqual(Array.from(state.bulksend.media, item => item.id), ['a']);
  assert.deepEqual(Array.from(state.bulksend.library, item => item.id), ['b']);
});

test('the ten attachment cap is enforced when reusing', () => {
  const state = harness({});
  state.bulksend.media = Array.from({ length: 10 }, (_, i) => ({ id: `m${i}` }));
  state.reuseBulkMedia({ id: 'a' });
  assert.equal(state.bulksend.media.length, 10);
  assert.equal(state.toasts[0].isError, true);
});

test('deleting a saved file clears it locally and on the server', async () => {
  const deleted = [];
  const state = harness({ delete: async (path) => { deleted.push(path); } });
  state.bulksend.library = [{ id: 'a' }];
  await state.removeBulkMedia('a');
  assert.deepEqual(deleted, ['/api/bulk/media/a']);
  assert.equal(state.bulksend.library.length, 0);
});
