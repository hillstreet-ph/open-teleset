import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const method = html.slice(html.indexOf('async backToPhoneInput() {'), html.indexOf('async cancelPhoneLogin() {'));

function harness(remove) {
  return {
    phoneStep: 'code', phoneAccountId: 'fixture', phoneLoading: false, phoneError: 'old error',
    phoneData: { code: 'fixture-code', password: 'fixture-password' },
    backToPhoneInput: vm.runInNewContext(`({${method}}).backToPhoneInput`, { _api: { delete: remove } }),
  };
}

test('Back waits for cancellation before allowing another code request', async () => {
  let complete;
  const state = harness(path => {
    assert.equal(path, '/api/accounts/fixture/phone-login');
    return new Promise(resolve => { complete = resolve; });
  });
  const pending = state.backToPhoneInput();
  assert.equal(state.phoneStep, 'code');
  assert.equal(state.phoneLoading, true);
  complete({ data: { success: true } });
  await pending;
  assert.equal(state.phoneStep, 'input');
  assert.equal(state.phoneData.code, '');
  assert.equal(state.phoneData.password, '');
  assert.equal(state.phoneLoading, false);
});

test('failed cancellation retains the code step and reports the error', async () => {
  const state = harness(async () => { throw { response: { data: { detail: 'fixture cancellation failure' } } }; });
  await state.backToPhoneInput();
  assert.equal(state.phoneStep, 'code');
  assert.equal(state.phoneData.code, 'fixture-code');
  assert.equal(state.phoneError, 'fixture cancellation failure');
  assert.equal(state.phoneLoading, false);
});

test('Back cannot cancel while code verification is already running', async () => {
  const state = harness(async () => { assert.fail('must not cancel'); });
  state.phoneLoading = true;
  await state.backToPhoneInput();
  assert.equal(state.phoneStep, 'code');
});

test('Back recovers when the previous session is already absent', async () => {
  const state = harness(async () => { throw { response: { status: 404 } }; });
  await state.backToPhoneInput();
  assert.equal(state.phoneStep, 'input');
  assert.equal(state.phoneData.code, '');
  assert.equal(state.phoneData.password, '');
  assert.equal(state.phoneError, '');
  assert.equal(state.phoneLoading, false);
});
