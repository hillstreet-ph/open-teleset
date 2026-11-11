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

const copilotMethods = methodSource('async loadCopilot() {', '// ---- Auto Reply ----');

function harness(draftResponse) {
  const calls = [];
  const toasts = [];
  const focused = [];
  const stored = [];
  const presets = [{ id: 'template-welcome', group: 'Templates', title: 'Welcome reply', description: 'd', instruction: 'Create a template' }];
  const tools = [
    { group: 'Workspace', tools: [{ id: 'accounts-overview', title: 'Accounts overview', summary: 'Counts and status.', write: false, example: 'Review my accounts.' }] },
    { group: 'Automation', tools: [{ id: 'schedule-create', title: 'Create an automation', summary: 'Adds a bound schedule.', write: true, example: 'Collect members from @group every day at 09:00, limit 200.' }] },
  ];
  const response = url => {
    if (url === '/api/copilot/history') return { turns: [] };
    if (url === '/api/copilot/presets') return { presets };
    if (url === '/api/copilot/tools') return { success: true, tools };
    return { success: true, draft: draftResponse, presets, turn_id: 'turn-1' };
  };
  const context = {
    _api: {
      get: async (url, config) => {
        calls.push({ method: 'get', url, config });
        return { data: response(url) };
      },
      post: async (url, body, config) => {
        calls.push({ method: 'post', url, body, config });
        return { data: response(url) };
      },
      put: async (url, body) => {
        calls.push({ method: 'put', url, body });
        return { data: response(url) };
      },
      delete: async (url) => {
        calls.push({ method: 'delete', url });
        return { data: response(url) };
      },
    },
    document: { getElementById: id => ({ focus: () => focused.push(id) }) },
    window: { confirm: () => true },
    localStorage: { setItem: (key, value) => stored.push([key, value]), getItem: () => null },
    console,
  };
  const state = {
    copilot: {
      instruction: '', loading: false, draft: null, presets: [], error: '', turnId: '', history: [],
      mode: 'ask', enhanced: [], attachments: [], suggestions: [], files: [], tools: [], toolCount: 0,
      editing: '', saving: false, formError: '',
      form: { group: '', title: '', description: '', instruction: '', command: '' },
    },
    newSchedule: {
      messageSource: 'input', message: '', templateId: '', month: 1, day: 1, hour: 9, minute: 0, second: 0,
      repeat: 'once', action: 'send_message', selectedAccount: '', selectedFriends: [],
      strangerUsernames: '', interval: 3000, approvalId: '', autoDedup: false, validateUsernames: false,
    },
    scraper: { limit: 500 },
    adder: { delay: 35, consentConfirmed: true },
    autojoin: { delay: 38, limit: 0 },
    bulksend: { messages: { stale_user: 'old' }, message: '', targets: '', consentConfirmed: true, delay: 45, perAccountLimit: 0 },
    autoReply: { config: { enabled: false, mode: 'rules', poll_seconds: 90, daily_cap: 20 }, pending: null },
    automation: { maxPrice: '', otpAttempts: '' },
    scheduleStep: 3,
    tab: 'copilot',
    sidebarOpen: true,
    showScheduleModal: false,
    $nextTick: fn => fn(),
    showToast: (message, error) => toasts.push({ message, error }),
    loadTemplates: async () => {},
    loadCopilotHistory: async () => {},
  };
  state.go = tab => { state.tab = tab; };
  const methods = vm.runInNewContext(`({${copilotMethods}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  state.loadCopilotHistory = async () => {};
  return { state, calls, toasts, focused, stored };
}

const FORBIDDEN = /send|bulk|outreach|autojoin|adder|\/api\/schedules/i;

test('copilot navigation and page metadata are wired', () => {
  assert.ok(html.includes("{ tab: 'copilot', label: 'Copilot', icon: 'copilot' }"));
  assert.ok(html.includes("copilot: ['Copilot', 'Draft templates, schedules and opt-in sends in plain language.']"));
  assert.match(html, /if \(tab === 'copilot'\) \{ this\.loadCopilot\(\); this\.loadCopilotHistory\(\); this\.loadCopilotSuggestions\(\); this\.loadCopilotAttachments\(\) \}/);
  assert.ok(html.includes('v-show="tab === \'copilot\'"'));
});

test('the Ask/Work switch sits on the left of the composer and marks writes', () => {
  assert.match(html, /class="copilot-auto"[\s\S]{0,200}role="switch"[\s\S]{0,160}:aria-checked/);
  assert.ok(!html.includes('class="copilot-mode-btn"'), 'the segmented header control is replaced by the composer switch');
  assert.ok(html.includes('@click="setCopilotMode(copilot.mode === \'work\' ? \'ask\' : \'work\')"'));
  assert.match(html, /Superpowers · \{\{ copilot\.toolCount \}\} internal actions/);
  assert.match(html, /<span v-if="tool\.write" class="copilot-tool-flag">write<\/span>/);
  assert.match(html, /PDF, Word, PowerPoint, spreadsheets and zips are read as text/);
});

test('a draft can be dismissed and a finished run leaves no dead apply button', () => {
  assert.match(html, /class="copilot-draft-x" @click="clearCopilot"/);
  assert.match(html, /v-if="copilot\.draft\.requires_confirmation && !\(copilot\.draft\.action === 'run' && copilot\.draft\.result\)"/);
  assert.match(html, /Ask keeps this write behind your click\./);
});

test('copilot apply paths never call a send, schedule or outreach endpoint', () => {
  assert.ok(!copilotMethods.includes('runBulkSend('), 'apply must not start BulkSend');
  assert.ok(!copilotMethods.includes('addSchedule('), 'apply must not create schedules');
  const endpoints = [...copilotMethods.matchAll(/_api\.(?:get|post|put|delete)\(\s*[`'"]([^`'"]+)/g)].map((m) => m[1]);
  assert.ok(endpoints.length >= 3, 'the copilot methods should call their own endpoints');
  for (const url of endpoints) assert.ok(!FORBIDDEN.test(url), `copilot must not reach ${url}`);
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
  assert.equal(calls[0].method, 'get');
  assert.equal(calls[0].url, '/api/copilot/presets');
  assert.equal(state.copilot.presets.length, 1);
});

test('runCopilotPreset drafts by id instead of re-parsing blocked prose', async () => {
  const draft = { action: 'ops', requires_confirmation: true, payload: { tab: 'adder', gate: 'gate-adder', fields: { delay: 38 } } };
  const { state, calls } = harness(draft);
  await state.runCopilotPreset({ id: 'ops-adder', instruction: 'Open the adder with a delay of 38.' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/copilot/preset-draft');
  assert.equal(calls[0].body.preset_id, 'ops-adder');
  assert.equal(calls[0].body.instruction, 'Open the adder with a delay of 38.');
  assert.equal(calls[0].body.mode, 'ask');
  assert.equal(state.copilot.draft, draft);
  assert.equal(state.copilot.turnId, 'turn-1');
  assert.equal(state.copilot.loading, false);
});

test('a custom internal preset is added and an existing preset is edited', async () => {
  const { state, calls } = harness(null);
  state.newCopilotPreset();
  Object.assign(state.copilot.form, { title: 'Find notes', instruction: 'Search notes for renewals', command: 'knowledge-search' });
  await state.saveCopilotPreset();
  assert.equal(calls[0].method, 'post');
  assert.equal(calls[0].url, '/api/copilot/presets');
  assert.equal(calls[0].body.kind, 'command');
  assert.equal(calls[0].body.command, 'knowledge-search');
  assert.equal(state.copilot.editing, '');

  state.editCopilotPreset({ id: 'template-welcome', group: 'Templates', title: 'Old', description: 'd', instruction: 'Old words' });
  state.copilot.form.title = 'New title';
  await state.saveCopilotPreset();
  assert.equal(calls[1].method, 'put');
  assert.equal(calls[1].url, '/api/copilot/presets/template-welcome');
  assert.equal(calls[1].body.title, 'New title');
  assert.equal(calls[1].body.kind, undefined);
});

test('preset validation happens before any request', async () => {
  const { state, calls } = harness(null);
  state.newCopilotPreset();
  await state.saveCopilotPreset();
  assert.equal(calls.length, 0);
  assert.match(state.copilot.formError, /title and an instruction/);
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

test('ops drafts only prefill declared fields and focus the operator gate', async () => {
  const { state, calls, toasts, focused } = harness(null);
  state.copilot.draft = {
    action: 'ops', requires_confirmation: true,
    payload: { surface: 'adder', tab: 'adder', gate: 'gate-adder', gate_label: 'Start Member Adder', fields: { delay: 38, unexpected: 'ignored' } },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0, 'opening an ops form must not call its endpoint');
  assert.equal(state.tab, 'adder');
  assert.equal(state.adder.delay, 38);
  assert.equal(state.adder.unexpected, undefined);
  assert.equal(state.adder.consentConfirmed, false);
  assert.deepEqual(focused, ['gate-adder']);
  assert.equal(toasts.length, 1);
});

test('BulkSend ops drafts reassert consent and pacing without sending', async () => {
  const { state, calls } = harness(null);
  state.bulksend.delay = 0;
  state.bulksend.perAccountLimit = 50;
  state.copilot.draft = {
    action: 'ops', requires_confirmation: true,
    payload: { surface: 'bulksend', tab: 'bulksend', gate: 'gate-bulksend', fields: { delay: 45, perAccountLimit: 0 } },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0);
  assert.equal(state.tab, 'bulksend');
  assert.equal(state.bulksend.consentConfirmed, false);
  assert.equal(state.bulksend.delay, 45);
  assert.equal(state.bulksend.perAccountLimit, 0);
});

test('Auto Reply ops drafts never enable the listener or save settings', async () => {
  const { state, calls } = harness(null);
  state.copilot.draft = {
    action: 'ops', requires_confirmation: true,
    payload: { surface: 'autoreply', tab: 'autoreply', gate: 'gate-autoreply', fields: { poll: 120, cap: 12, mode: 'rules' } },
  };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 0);
  assert.equal(state.tab, 'autoreply');
  assert.equal(state.autoReply.config.enabled, false);
  assert.equal(state.autoReply.config.poll_seconds, 120);
  assert.equal(state.autoReply.config.daily_cap, 12);
  assert.equal(state.autoReply.pending.poll_seconds, 120);
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

test('the superpower catalogue loads grouped tools and a write flag', async () => {
  const { state, calls } = harness(null);
  await state.loadCopilotTools();
  assert.equal(calls[0].url, '/api/copilot/tools');
  assert.equal(state.copilot.toolCount, 2);
  assert.deepEqual(state.copilot.tools.map(group => group.group), ['Workspace', 'Automation']);
  assert.equal(state.copilot.tools[1].tools[0].write, true);
});

test('clicking a superpower prefills its example and drafts it in the current mode', async () => {
  const draft = { action: 'run', requires_confirmation: false, payload: { command: 'accounts-overview' }, result: { summary: '2 accounts', items: [] } };
  const { state, calls } = harness(draft);
  state.copilot.mode = 'ask';
  await state.useCopilotTool({ example: 'Review my accounts.' });
  assert.equal(calls[0].url, '/api/copilot/draft');
  assert.equal(calls[0].body.instruction, 'Review my accounts.');
  assert.equal(calls[0].body.mode, 'ask');
  assert.equal(state.copilot.draft.result.summary, '2 accounts');
});

test('a pending run is executed by Run now in Work and the mode is restored', async () => {
  const executed = {
    action: 'run', requires_confirmation: false, mode: 'work', payload: { command: 'schedule-create' },
    result: { summary: 'Automation created.', items: [] },
  };
  const { state, calls, stored } = harness(executed);
  state.copilot.mode = 'ask';
  state.copilot.instruction = 'Collect members from @group every day at 09:00, limit 200.';
  state.copilot.draft = { action: 'run', requires_confirmation: true, pending: true, payload: { command: 'schedule-create' } };
  await state.applyCopilotDraft();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/copilot/draft');
  assert.equal(calls[0].body.mode, 'work');
  assert.equal(state.copilot.draft.result.summary, 'Automation created.');
  assert.equal(state.copilot.mode, 'ask', 'the one-off run does not flip the panel into Work');
  assert.deepEqual(stored.at(-1), ['ot_copilot_mode', 'ask']);
});

test('a pending run gets a Run now label instead of Apply draft', () => {
  const { state } = harness(null);
  state.copilot.draft = { action: 'run', requires_confirmation: true, payload: { command: 'schedule-create' } };
  assert.equal(state.copilotApplyLabel(), 'Run now');
});
