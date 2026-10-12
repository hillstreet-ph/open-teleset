import { createHash } from 'node:crypto';
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
  const context = {_api:{post},crypto:{randomUUID}};
  vm.runInNewContext(readFileSync('static/csv.js', 'utf8'), context);
  return {accessReady:true, authRevision:1, outreachJobs:[], loadOutreachJobs(){},
    adder:{consentConfirmed:true, accountId:'account', destTarget:'group', usernames:'111', limit:10, delay:35, filterBots:true, activityFilter:'all'},
    ...vm.runInNewContext(`({${source}})`, context)};
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
    [{usernames:' \n\t'}, /Enter at least one valid consenting recipient/],
    [{usernames:'@'}, /Enter at least one valid consenting recipient/],
    [{usernames:'abc'}, /Enter at least one valid consenting recipient/],
    [{usernames:'alice_test bob_test'}, /Enter at least one valid consenting recipient/],
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

test('Member Adder retains invalid rows for skipped history while queuing valid recipients', async () => {
  let payload;
  const state=adderHarness(async (path,body) => {payload=body;return {data:{id:'job'}};});
  state.adder.usernames=Array.from({length:10}, (_,i)=>String(i+1)).join('\n')+'\nhttps://example.com';
  const summary=state.parseAdderRecipients();
  assert.equal(summary.targets.length,10);
  assert.deepEqual(Array.from(summary.invalidLines),[11]);
  await state.runAdder();
  assert.match(state.adder.resultMsg,/1 invalid entries will be skipped/);
  assert.equal(payload.usernames.length,11);
  assert.equal(payload.consent_confirmed,true);
});

test('Member Adder allows a valid upper delay boundary', async () => {
  let calls=0;
  const state=adderHarness(async () => {calls++;return {data:{id:'job'}};});
  state.adder.delay=3600;
  await state.runAdder();
  assert.equal(calls,1);
});


test('dashboard requests the content-versioned CSV script, bypassing a cached old API', () => {
  const hash = createHash('sha256').update(readFileSync('static/csv.js')).digest('hex').slice(0,16);
  assert.ok(html.includes(`/static/csv.js?v=${hash}`));
});

test('Member Adder queues all 250 unique recipients without a manual cap', async () => {
  let payload;
  const state = adderHarness(async (path, body) => {payload=body;return {data:{id:'job'}};});
  state.adder.usernames=Array.from({length:250}, (_,i)=>String(i+1)).join('\n');
  await state.runAdder();
  assert.equal(payload.usernames.length,250);
  assert.equal(Object.hasOwn(payload,'limit'),false);
  assert.match(state.adder.resultMsg,/Queued/);
});

test('oversized pasted recipient list is rejected before network', async () => {
  const state = adderHarness(async () => assert.fail('must not submit'));
  state.adder.usernames='1'.repeat(2*1024*1024+1);
  await state.runAdder();
  assert.match(state.adder.resultMsg,/at most 2 MB/);
});


test('Member Adder requires explicit consent and never infers it from a pasted list', async () => {
  const state=adderHarness(async()=>assert.fail('must not send'));
  state.adder.consentConfirmed=false;
  await state.runAdder();
  assert.match(state.adder.resultMsg,/Confirm that these recipients agreed to join/);
});


test('Member Adder preserves malformed repeated-at usernames for backend skip', async () => {
  let payload;
  const state = adderHarness(async (path, body) => {payload = body; return {data:{id:'job'}};});
  state.adder.usernames='@alice_test\n@@alice_test';
  await state.runAdder();
  assert.deepEqual(Array.from(payload.usernames), ['alice_test','@@alice_test']);
});
