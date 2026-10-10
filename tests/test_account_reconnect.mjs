import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html = readFileSync('static/dashboard.html', 'utf8');
const source = html.slice(html.indexOf('async checkAccountConnection(id) {'), html.indexOf('async deleteAccount(id) {'));
test('connection check uses the existing non-sending health endpoint and releases button', async () => {
  for (const healthy of [true, false]) {
    const calls = [], toasts = [];
    const state = {checkingAccounts: {}, showToast: (...args)=>toasts.push(args), loadAccounts: async()=>{}, loadHealth: async()=>{}};
    state.check = vm.runInNewContext(`({${source}}).checkAccountConnection`, {_api: {post: async (path)=>{
      calls.push(path); return {data: {result: healthy ? {status:'healthy'} : {status:'unhealthy', error:'Session revoked'}}};
    }}});
    await state.check('saved');
    assert.deepEqual(calls, ['/api/health/check/saved']);
    assert.equal(state.checkingAccounts.saved, false);
    assert.equal(toasts[0][1], !healthy);
  }
});
test('timestamps are interpreted as UTC across browser timezones and invalid dates stay empty', () => {
  const method = html.slice(html.indexOf('formatTime(t) {'), html.indexOf('getScheduleDescription(sch) {'));
  const format = vm.runInNewContext(`({${method}}).formatTime`, {Date});
  const timestamp = new Date(Date.now()-3600000).toISOString();
  assert.equal(format(timestamp), format(timestamp.slice(0,-1)));
  assert.equal(format('not-a-date'), '-');
  assert.match(html, /<td>\{\{ acc.phone \|\| '-' \}\}<\/td>/);
});
