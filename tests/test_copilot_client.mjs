import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

function methodSource(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = html.indexOf(endMarker, start);
  assert.ok(end > start, `missing end marker after ${startMarker}`);
  return html.slice(start, end);
}

const copilotMethods = methodSource('async loadCopilot() {', '// Templates');

function harness(draftResponse) {
  const calls = [];
  const toasts = [];
  const context = {
    _api: {
      post: async (url, body, config) => {
        calls.push({ url, body, config });
        return { data: { success: true, draft: draftResponse, presets: [{ id: 'template-welcome', group: 'Templates', title: 'Welcome reply', description: 'd', instruction: 'Create a template' }] } };
      },
    },
    console,
  };
  const state = {
    copilot: { instruction: '', loading: false, draft: null, presets: [], error: '' },
    newSchedule: {
      messageSource: 'input', message: '', templateId: '', month: 1, day: 1, hour: 9, minute: 0, second: 0,
      repeat: 'once', action: 'send_message', selectedAccount: '', selectedFriends: [],
      strangerUsernames: '', interval: 3000, approvalId: '', autoDedup: false, validateUsernames: false,
    },
    bulksend: { messages: { stale_user: 'old' }, message: '', targets: '', consentConfirmed: true, delay: 5 },
    scheduleStep: 3,
    tab: 'copilot',
    sidebarOpen: true,
    showScheduleModal: false,
    showToast: (message, error) => toasts.push({ message, error }),
    loadTemplates: async () => {},
  };
  state.go = tab => { state.tab = tab; };
  const methods = vm.runInNewContext(`({${copilotMethods}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  return { state, calls, toasts };
}

const FORBIDDEN = /send|bulk|outreach|autojoin|adder|\/api\/schedules/i;

test('copilot navigation and page metadata are wired', () => {
  assert.ok(html.includes("{ tab: 'copilot', label: 'Copilot', icon: 'copilot' }"));
  assert.ok(html.includes("copilot: ['Copilot', 'Draft templates, schedules and opt-in sends in plain language.']"));
  assert.ok(html.includes("if (tab === 'copilot') this.loadCopilot()"));
  assert.ok(html.includes('v-show="tab === \'copilot\'"'));
});

test('copilot apply paths never call a send, schedule or outreach endpoint', () => {
  assert.ok(!copilotMethods.includes('runBulkSend('), 'apply must not start BulkSend');
  assert.ok(!copilotMethods.includes('addSchedule('), 'apply must not create schedules');
  assert.ok(!copilotMethods.includes('_api.get('), 'apply must not fetch execution endpoints');
});

test('draftCopilot posts the instruction and stores draft plus presets', async () => {
  const draft = { action: 'help', title: 'Help', summary: 's', payload: {}, warnings: [], requires_confirmation: false };
  const { state, calls } = harness(draft);
  state.copilot.instruction = 'hello';
  await state.draftCopilot();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/copilot/draft');
  assert.equal(calls[0].body.instruction, 'hello');
  assert.equal(state.copilot.draft.action, 'help');
  assert.equal(state.copilot.presets.length, 1);
  assert.equal(state.copilot.loading, false);
});

test('loadCopilot fetches presets once and caches them', async () => {
  const { state, calls } = harness(null);
  await state.loadCopilot();
  await state.loadCopilot();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.instruction, '');
  assert.equal(state.copilot.presets.length, 1);
});

test('template draft saves through /api/templates and nothing else', async () => {
  const { state, calls } = harness(null);
  state.copilot.draft = {
    action: 'template', requires_confirmation: true,
    payload: { name: 'Welcome', content: 'Hi {name}, thanks.', category: 'Copilot', variables: [] },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/templates');
  assert.ok(calls[0].body.template_id.startsWith('tpl_'));
  assert.equal(calls[0].body.name, 'Welcome');
  assert.equal(calls[0].body.content, 'Hi {name}, thanks.');
  assert.equal(calls[0].body.category, 'Copilot');
  assert.equal(calls[0].body.variables.length, 0);
  assert.equal(state.tab, 'templates');
  assert.ok(!FORBIDDEN.test(calls[0].url));
});

test('template draft without content is refused before any request', async () => {
  const { state, calls, toasts } = harness(null);
  state.copilot.draft = { action: 'template', requires_confirmation: true, payload: { name: 'Empty', content: '' } };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].error, true);
});

test('schedule draft prefills the approval-gated form without creating it', async () => {
  const { state, calls, toasts } = harness(null);
  state.copilot.draft = {
    action: 'schedule', requires_confirmation: true,
    payload: {
      message: 'Daily update', recipients: ['alice123', 'bob_1234'], repeat: 'daily',
      hour: 10, minute: 30, interval: 1000, approvalId: 'APR-9',
    },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0, 'no request may be made from the Copilot');
  assert.equal(state.showScheduleModal, true);
  assert.equal(state.scheduleStep, 1);
  assert.equal(state.tab, 'schedule');
  assert.equal(state.newSchedule.message, 'Daily update');
  assert.equal(state.newSchedule.strangerUsernames, 'alice123\nbob_1234');
  assert.equal(state.newSchedule.repeat, 'daily');
  assert.equal(state.newSchedule.hour, 10);
  assert.equal(state.newSchedule.minute, 30);
  assert.equal(state.newSchedule.second, 0);
  assert.equal(state.newSchedule.interval, 3000, 'interval below 3s is raised to the gate');
  assert.equal(state.newSchedule.approvalId, 'APR-9');
  assert.equal(state.newSchedule.autoDedup, true);
  assert.equal(state.newSchedule.validateUsernames, true);
  assert.equal(state.sidebarOpen, false);
  assert.equal(toasts.length, 1);
});

test('task draft prefills BulkSend with consent unchecked and sends nothing', async () => {
  const { state, calls, toasts } = harness(null);
  state.copilot.draft = {
    action: 'task', requires_confirmation: true,
    payload: { message: 'Hi there', targets: ['alice123'], delay: 0, consentConfirmed: true },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0, 'the Copilot must not start the send');
  assert.equal(state.tab, 'bulksend');
  assert.equal(state.bulksend.message, 'Hi there');
  assert.equal(state.bulksend.targets, 'alice123');
  assert.equal(Object.keys(state.bulksend.messages).length, 0, 'stale per-recipient messages are cleared');
  assert.equal(state.bulksend.consentConfirmed, false, 'consent must be re-confirmed by the operator');
  assert.equal(toasts.length, 1);
});

test('help and unsupported drafts apply nothing', async () => {
  const { state, calls, toasts } = harness(null);
  state.copilot.draft = { action: 'help', requires_confirmation: false, payload: {} };
  await state.applyCopilotDraft();
  state.copilot.draft = { action: 'unsupported', requires_confirmation: false, payload: {} };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0);
  assert.equal(state.tab, 'copilot');
  assert.equal(toasts.length, 0);
});
