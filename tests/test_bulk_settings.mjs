import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html = readFileSync('static/dashboard.html', 'utf8');
const method = html.slice(html.indexOf('async runBulkSend() {'), html.indexOf('// === Auth Methods ==='));
function harness(post) {
  return { bulksend: { accountId: 'account', accountIds: [], rotationMode: 'single', perAccountLimit: 0,
    consentConfirmed: true, delay: 0, message: 'test', messages: {}, targets: Array.from({length: 30}, (_,i)=>String(i+1)).join('\n') },
    runBulkSend: vm.runInNewContext(`({${method}}).runBulkSend`, { _api: { post } }) };
}
test('form submits over 20 recipients with zero delay and no approval', async () => {
  let payload;
  const state = harness(async (path, body) => { payload = body; return {data: {success: true, sent: 30, total: 30}}; });
  await state.runBulkSend();
  assert.equal(payload.targets.length, 30);
  assert.equal(payload.delay, 0);
  assert.equal(payload.per_account_limit, 0);
  assert.equal(payload.consent_confirmed, true);
  assert.equal(Object.hasOwn(payload, 'approval_id'), false);
  assert.match(state.bulksend.resultMsg, /Completed/);
});
test('optional cap and negative delay still fail before network', async () => {
  for (const change of [{perAccountLimit: 1}, {delay: -1}]) {
    const state = harness(async () => assert.fail('must not call API'));
    Object.assign(state.bulksend, change);
    await state.runBulkSend();
    assert.match(state.bulksend.resultMsg, /❌/);
  }
});
test('CSV without recipient cap supports more than old row limit', () => {
  const context = {}; vm.runInNewContext(readFileSync('static/csv.js', 'utf8'), context);
  const text = 'recipient\n' + Array.from({length: 10002}, (_,i)=>String(i+1)).join('\n');
  assert.equal(context.OpenTelesetCSV.recipients(text).targets.length, 10002);
  assert.throws(() => context.OpenTelesetCSV.recipients(text, 10));
});
