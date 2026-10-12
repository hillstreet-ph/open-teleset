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

function adderHarness(post) {
  const source = html.slice(html.indexOf('parseAdderRecipients() {'), html.indexOf('// ---- BulkSend ----'));
  return {accessReady:true, authRevision:1, outreachJobs:[], loadOutreachJobs(){},
    adder:{accountId:'account', destTarget:'group', usernames:'111', limit:10, delay:35, filterBots:true, activityFilter:'all'},
    ...vm.runInNewContext(`({${source}})`, {_api:{post},crypto:{randomUUID}})};
}

test('Member Adder submits without a manual approval reference', async () => {
  const form = html.slice(html.indexOf('<!-- ============ Adder Tab'), html.indexOf('<!-- ============ BulkSend'));
  assert.doesNotMatch(form, /adder\.approvalId|Approved member-add reference/);
  let payload;
  const state = adderHarness(async (path,body)=>{
      assert.equal(path,'/api/outreach/jobs/members'); payload=body; return {data:{id:'job'}};
    });
  await state.runAdder();
  assert.ok(payload.request_id);
  assert.equal(Object.hasOwn(payload,'approval_id'),false);
  assert.equal(state.outreachJobs.length,1);
});

test('Member Adder counts and sends unique recipients without modifying pasted text', async () => {
  let payload;
  const state = adderHarness(async (path, body) => {payload = body; return {data:{id:'job'}};});
  const input = ' @Alice_Test \r\nalice_test\n\n@ALICE_TEST\r123\n' + Array.from({length:8}, (_,i)=>String(i+124)).join('\n');
  state.adder.usernames = input;
  const summary = state.parseAdderRecipients();
  assert.equal(summary.targets.length, 10);
  assert.equal(summary.duplicates, 2);
  await state.runAdder();
  assert.equal(payload.usernames.length, 10);
  assert.equal(payload.usernames[0], 'alice_test');
  assert.equal(payload.usernames[1], '123');
  assert.equal(state.adder.usernames, input);
  assert.match(state.adder.resultMsg, /Queued/);
});

test('Member Adder explains the actual invalid field and never submits invalid input', async () => {
  const cases = [
    [{usernames:' \n\t'}, /Enter at least one consenting recipient/],
    [{usernames:Array.from({length:12}, (_,i)=>String(i+1)).join('\n')}, /12 unique recipients; the maximum is 10\. Remove 2/],
    [{usernames:'alice_test\n\n@vietnamnet.vn'}, /Invalid recipient on line 3/],
    [{usernames:'@'}, /Invalid recipient on line 1/],
    [{usernames:'alice_test bob_test'}, /Invalid recipient on line 1/],
    [{limit:''}, /Max Members to Add must be a whole number from 1 to 10/],
    [{limit:1.5}, /Max Members to Add must be a whole number/],
    [{limit:11}, /Max Members to Add must be a whole number/],
    [{usernames:'111\n222',limit:1}, /2 unique recipients, but Max Members to Add is 1\. Increase it to 2 or remove 1/],
    ...['',NaN,Infinity,34,3601].map(delay=>[{delay}, /Delay Between Adds must be a number from 35 to 3600 seconds/]),
  ];
  for (const [change, error] of cases) {
    const state = adderHarness(async () => assert.fail('Invalid input must not call the API'));
    Object.assign(state.adder,change);
    await state.runAdder();
    assert.match(state.adder.resultMsg,error);
    assert.equal(state.adder.loading,false);
    assert.equal(state.outreachJobs.length,0);
  }
});

test('Member Adder allows a valid upper delay boundary', async () => {
  let calls=0;
  const state=adderHarness(async () => {calls++;return {data:{id:'job'}};});
  state.adder.delay=3600;
  await state.runAdder();
  assert.equal(calls,1);
});
