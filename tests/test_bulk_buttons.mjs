import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import assert from 'node:assert/strict';
const html = readFileSync('static/dashboard.html', 'utf8');
const computed = vm.runInNewContext(`({${html.slice(html.indexOf('bulkButtons() {'), html.indexOf('workflowLinks() {'))}})`);
const methods = vm.runInNewContext(`({${html.slice(html.indexOf('addBulkButton() {'), html.indexOf('// ---- Automation ----'))}})`);
const runner = html.slice(html.indexOf('async runBulkSend() {'), html.indexOf('// === Auth Methods ==='));

function state(buttons, post = async () => ({ data: { id: 'job', state: 'queued' } })) {
  const view = {
    accessReady: true,
    authRevision: 1,
    outreachJobs: [],
    loadOutreachJobs() {},
    apiErrorText: (error, fallback) => error.message || fallback,
    bulksend: {
      accountId: 'account', accountIds: [], rotationMode: 'single', perAccountLimit: 0,
      consentConfirmed: true, delay: 0, message: 'test', messages: {}, media: [],
      uploading: false, dragActive: false, filterBots: true, activityFilter: 'all',
      targets: '111', requestId: '', buttons: buttons.map(button => ({ ...button })),
    },
    get bulkButtons() { return computed.bulkButtons.call(this); },
    addBulkButton: methods.addBulkButton,
    removeBulkButton: methods.removeBulkButton,
  };
  // runBulkSend reads `this.bulkButtons`, so it must be called on the same view.
  view.runBulkSend = vm.runInNewContext(`({${runner}}).runBulkSend`, { _api: { post }, crypto: { randomUUID } });
  return view;
}

test('button rows are trimmed and empty rows are ignored', () => {
  const view = state([
    { text: '  Open  ', url: ' https://example.com ' },
    { text: '', url: '' },
    { text: 'Typing…', url: '' },
  ]);
  // Map to plain tuples: vm values carry another realm's prototypes.
  assert.deepEqual(view.bulkButtons.map(button => [button.text, button.url]), [
    ['Open', 'https://example.com'],
    ['Typing…', ''],
  ]);
});

test('at most three buttons can be added and a row can be removed', () => {
  const view = state([{ text: 'One', url: 'https://example.com/1' }, { text: 'Two', url: 'https://example.com/2' }]);
  view.addBulkButton();
  assert.equal(view.bulksend.buttons.length, 3);
  view.addBulkButton();
  assert.equal(view.bulksend.buttons.length, 3);
  view.removeBulkButton(1);
  assert.deepEqual(Array.from(view.bulksend.buttons, button => button.text), ['One', '']);
});

test('a finished button rides on the job payload', async () => {
  let payload;
  const view = state([{ text: ' Claim now ', url: ' https://example.com/deal ' }],
    async (path, body) => { payload = body; return { data: { id: 'job', state: 'queued' } }; });
  await view.runBulkSend();
  assert.deepEqual(payload.buttons.map(button => [button.text, button.url]),
    [['Claim now', 'https://example.com/deal']]);
});

test('a half-filled button is refused before any request', async () => {
  let called = false;
  const view = state([{ text: 'Open', url: '' }], async () => { called = true; });
  await view.runBulkSend();
  assert.equal(called, false);
  assert.match(view.bulksend.resultMsg, /label and an http/);
});
