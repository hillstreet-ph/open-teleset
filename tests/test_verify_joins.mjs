import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const methods = vm.runInNewContext(
  `({${html.slice(html.indexOf('async verifyJoinMembership(job) {'), html.indexOf('// ---- Webhooks ----'))}})`
);
const labels = vm.runInNewContext(
  `({${html.slice(html.indexOf('jobCountLabel(job) {'), html.indexOf('outreachReason(reason) {'))}})`
);

function view(post, confirmResult = true) {
  const state = {
    outreachJobBusy: '',
    outreachJobError: '',
    accessReady: true,
    authRevision: 1,
    loadCalls: 0,
    loadOutreachJobs() { this.loadCalls += 1; },
  };
  state.verifyJoinMembership = vm.runInNewContext(
    `({${html.slice(html.indexOf('async verifyJoinMembership(job) {'), html.indexOf('// ---- Webhooks ----'))}}).verifyJoinMembership`,
    { _api: { post }, window: { confirm: () => confirmResult }, console }
  ).bind(state);
  state.jobCountLabel = labels.jobCountLabel.bind(state);
  state.jobResultState = labels.jobResultState.bind(state);
  return state;
}

test('checking memberships queues a probe for the run and reloads the list', async () => {
  const calls = [];
  const state = view(async (url) => { calls.push(url); return { data: { id: 'check' } }; });
  await state.verifyJoinMembership({ id: 'abc12345', kind: 'auto_join', completed: 3 });
  assert.deepEqual(calls, ['/api/outreach/jobs/abc12345/verify-joins']);
  assert.equal(state.loadCalls, 1);
  assert.equal(state.outreachJobBusy, '');
  assert.equal(state.outreachJobError, '');
});

test('cancelling the confirmation sends nothing', async () => {
  let posted = 0;
  const state = view(async () => { posted += 1; }, false);
  await state.verifyJoinMembership({ id: 'abc12345', kind: 'auto_join', completed: 3 });
  assert.equal(posted, 0);
  assert.equal(state.loadCalls, 0);
});

test('a refused check surfaces the server detail and clears the busy flag', async () => {
  const state = view(async () => {
    throw { response: { data: { detail: 'Membership check ran in the last 10 minutes' } } };
  });
  await state.verifyJoinMembership({ id: 'abc12345', kind: 'auto_join', completed: 3 });
  assert.equal(state.outreachJobError, 'Membership check ran in the last 10 minutes');
  assert.equal(state.outreachJobBusy, '');
});

test('a verify run counts confirmed membership instead of new joins', () => {
  const state = view(async () => ({ data: {} }));
  const check = { kind: 'auto_join', verify_only: true };
  assert.equal(state.jobCountLabel(check), 'Member');
  assert.equal(state.jobResultState(check, 'completed'), 'Member');
  assert.equal(state.jobCountLabel({ kind: 'auto_join', verify_only: false }), 'Joined');
  assert.equal(state.jobResultState({ kind: 'auto_join' }, 'completed'), 'Joined');
});

test('the job card offers the check only for runs that reported joins', () => {
  const button = html.slice(html.indexOf('Check memberships'), html.indexOf('Check memberships') + 260);
  assert.match(html, /job\.kind === 'auto_join' && !job\.verify_only && job\.completed > 0/);
  assert.match(html, /verifyJoinMembership\(job\)/);
  assert.ok(button.length > 0);
});
