import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import assert from 'node:assert/strict';
const html = readFileSync('static/dashboard.html', 'utf8');
const method = html.slice(html.indexOf('async runBulkSend() {'), html.indexOf('// === Auth Methods ==='));
function harness(post) {
  return { accessReady: true, authRevision: 1, outreachJobs: [], loadOutreachJobs() {}, bulksend: { accountId: 'account', accountIds: [], rotationMode: 'single', perAccountLimit: 0,
    consentConfirmed: true, delay: 0, message: 'test', messages: {}, targets: Array.from({length: 30}, (_,i)=>String(i+1)).join('\n') },
    runBulkSend: vm.runInNewContext(`({${method}}).runBulkSend`, { _api: { post }, crypto: { randomUUID } }) };
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
  assert.match(state.bulksend.resultMsg, /Queued/);
  assert.ok(payload.request_id);
  assert.equal(state.bulksend.requestId, '');
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

test('uncertain HTTP response reuses submission key on retry', async () => {
  const keys = [];
  const state = harness(async (path, body) => {
    assert.equal(path, '/api/outreach/jobs/personal'); keys.push(body.request_id);
    if (keys.length === 1) throw new Error('timeout');
    return {data: {id: 'job', state: 'queued'}};
  });
  await state.runBulkSend(); await state.runBulkSend();
  assert.equal(keys[0], keys[1]); assert.equal(state.outreachJobs.length, 1);
});

test('late response after logout cannot expose previous job', async () => {
  let resolve;
  const state = harness(() => new Promise(r => {resolve = r;}));
  const pending = state.runBulkSend();
  state.authRevision++; state.accessReady = false; state.bulksend.resultMsg = '';
  resolve({data: {id:'old-owner-private-job'}}); await pending;
  assert.equal(state.outreachJobs.length, 0); assert.equal(state.bulksend.resultMsg, '');
});
test('edited payload conflict refreshes original job without automatic resubmission', async () => {
  let calls = 0;
  const state = harness(async () => {calls++; throw {response:{status:409}};});
  state.bulksend.requestId = 'prior-submission';
  await state.runBulkSend();
  assert.equal(calls, 1); assert.equal(state.bulksend.requestId, '');
  assert.match(state.bulksend.resultMsg, /previous submission exists/);
});
