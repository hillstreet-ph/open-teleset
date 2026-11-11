// Node workflows: the dashboard half of the canvas builder.
// The API contract lives in tests/test_workflows.py; here we prove the panel
// state, the canvas wiring, and that every control calls the route the backend
// serves.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const appStart = html.indexOf('const app = createApp({');
const dataStart = html.indexOf('data() {', appStart);
const dataEnd = html.indexOf('computed: {', dataStart);
const data = vm.runInNewContext(`({${html.slice(dataStart, dataEnd).replace(/,\s*$/, '')}})`, {
  window: {}, localStorage: { getItem: () => null }, location: { search: '', hash: '' },
  APP_CONFIG: {}, Date, Math, JSON, URLSearchParams,
}).data;

const computedEnd = html.indexOf('methods: {', dataEnd);
const computed = vm.runInNewContext(`({${html.slice(dataEnd, computedEnd).replace(/,\s*$/, '')}})`, {
  window: {}, localStorage: { getItem: () => null }, location: { search: '', hash: '' },
  APP_CONFIG: {}, Date, Math, JSON, URLSearchParams,
}).computed;

const CATALOG = [
  { type: 'scrape', label: 'Scrape members', hint: 'Read a group roster.', fields: [] },
  { type: 'wait', label: 'Wait', hint: 'Pause between steps.', fields: [] },
  { type: 'join', label: 'Join groups', hint: 'Join consented links.', fields: [] },
  { type: 'notify', label: 'Note yourself', hint: 'Send yourself a note.', fields: [] },
  { type: 'table_append', label: 'Save to a table', hint: 'Append results to a data table.', fields: [] },
];

function methods(api = {}, extra = {}) {
  return vm.runInNewContext(
    `({${html.slice(html.indexOf('// Node workflows'), html.indexOf('// Data tables'))}})`,
    { _api: api, window: { confirm: () => true }, console, Math, JSON, setTimeout, clearTimeout, ...extra },
  );
}

const plain = (value) => JSON.parse(JSON.stringify(value));

function state() {
  const workflows = plain(data.call({}).workflows);
  workflows.catalog = CATALOG;
  return {
    workflows,
    accounts: [{ account_id: 'ui-one', name: 'UI One' }, { account_id: 'ui-two', name: 'UI Two' }],
    toasts: [],
    showToast(message, isError) { this.toasts.push({ message, isError }); },
    async loadWorkflows() { this.loaded = (this.loaded || 0) + 1; },
    pollWorkflowRun() { this.polled = (this.polled || 0) + 1; },
  };
}

function withComputed(app) {
  for (const [key, getter] of Object.entries(computed)) {
    Object.defineProperty(app, key, { get: getter, configurable: true });
  }
  return app;
}

const workflow = () => ({
  id: 'wf_1', name: 'Weekly sweep',
  nodes: [
    { id: 'n1', type: 'scrape', config: { target: '@news', limit: 50, filter_bots: true }, x: 40, y: 40 },
    { id: 'n2', type: 'wait', config: { seconds: 5 }, x: 290, y: 40 },
  ],
  edges: [{ from: 'n1', to: 'n2' }],
  created_at: '2026-11-07T00:00:00Z', updated_at: '2026-11-07T00:00:00Z',
  last_run: null, running: false,
});

test('data() initialises the workflow panel', () => {
  const config = data.call({});
  assert.ok(config.workflows, 'workflows state missing');
  assert.deepEqual(plain(config.workflows.list), []);
  assert.equal(config.workflows.capacity.limit, 50);
  assert.equal(config.workflows.capacity.nodes, 20);
  assert.equal(config.workflows.capacity.accounts, 5);
  assert.equal(config.workflows.active, null);
  assert.equal(config.workflows.activeRunning, false);
  assert.deepEqual(plain(config.workflows.accountIds), []);
  assert.deepEqual(plain(config.workflows.templates), []);
  assert.deepEqual(plain(config.workflows.schedules), []);
  assert.equal(config.workflows.scheduleForm.open, false);
});

test('the sidebar, tab panel, canvas and controls are wired into the dashboard', () => {
  assert.ok(html.includes("{ tab: 'workflows', label: 'Workflows', icon: 'code' }"));
  assert.ok(html.includes("v-show=\"tab === 'workflows'\""));
  for (const id of ['wf-new', 'wf-save', 'wf-run', 'wf-name', 'wf-schedule-open',
    'wf-schedule-repeat', 'wf-schedule-hour', 'wf-schedule-minute', 'wf-schedule-weekday',
    'wf-schedule-date', 'wf-schedule-save']) {
    assert.ok(html.includes(`id="${id}"`), `missing #${id}`);
  }
  for (const testid of ['wf-editor', 'wf-canvas', 'wf-node', 'wf-config', 'wf-runlog',
    'wf-accounts', 'wf-schedule', 'wf-schedule-list']) {
    assert.ok(html.includes(`data-testid="${testid}"`), `missing data-testid ${testid}`);
  }
  assert.ok(html.includes(":data-testid=\"'wf-add-' + item.type\""));
  assert.ok(html.includes(":data-testid=\"'wf-template-' + t.id\""));
  assert.ok(html.includes("if (tab === 'workflows') this.loadWorkflows()"));
  assert.ok(html.includes("if (this.tab === 'workflows') this.loadWorkflows()"));
});

test('loading workflows stores the list, capacity, catalog, presets and schedules', async () => {
  const api = {
    get: async (url) => {
      if (url === '/api/tables') return { data: { tables: [] } };
      assert.equal(url, '/api/workflows');
      return { data: {
        workflows: [workflow()], capacity: { used: 1, limit: 50, nodes: 20, accounts: 5 },
        catalog: CATALOG, templates: [{ id: 'member_table', name: 'Scrape to table', nodes: [], edges: [] }],
        schedules: [{ schedule_id: 'wfsch_1', workflow_id: 'wf_1' }],
      } };
    },
  };
  const base = state();
  delete base.loadWorkflows;
  const app = { ...methods(api), ...base };
  await app.loadWorkflows();
  assert.equal(app.workflows.list.length, 1);
  assert.equal(app.workflows.capacity.used, 1);
  assert.equal(app.workflows.capacity.accounts, 5);
  assert.equal(app.workflows.catalog.length, 5);
  assert.equal(app.workflows.templates.length, 1);
  assert.equal(app.workflows.schedules.length, 1);
  assert.equal(app.workflows.loading, false);
});

test('adding nodes uses per-type defaults and auto-connects a single tail', () => {
  const app = { ...methods(), ...state() };
  app.newWorkflow();
  app.addNode('scrape');
  assert.equal(app.workflows.active.nodes.length, 1);
  const first = app.workflows.active.nodes[0];
  assert.deepEqual(plain(first.config), { target: '', limit: 200, filter_bots: true });
  assert.equal(first.x, 40);
  assert.equal(first.y, 40);
  assert.equal(app.workflows.selectedId, first.id);

  app.addNode('wait');
  const second = app.workflows.active.nodes[1];
  assert.deepEqual(plain(second.config), { seconds: 30 });
  assert.deepEqual(plain(app.workflows.active.edges), [{ from: first.id, to: second.id }]);

  app.addNode('notify');
  const third = app.workflows.active.nodes[2];
  assert.equal(app.workflows.active.edges.length, 2, 'the new node extends the single chain');
  assert.deepEqual(plain(app.workflows.active.edges.at(-1)), { from: second.id, to: third.id });

  app.addNode('table_append');
  assert.deepEqual(plain(app.workflows.active.nodes[3].config), { table_id: '', column_id: '' });
});

test('adding past the node cap is refused with a visible error', () => {
  const app = { ...methods(), ...state() };
  app.newWorkflow();
  app.workflows.capacity.nodes = 1;
  app.addNode('wait');
  app.addNode('wait');
  assert.equal(app.workflows.active.nodes.length, 1);
  assert.match(app.workflows.error, /1 nodes/);
});

test('connecting replaces the source edge and keeps one way in', () => {
  const app = { ...methods(), ...state() };
  app.workflows.active = workflow();
  app.workflows.active.nodes.push({ id: 'n3', type: 'wait', config: { seconds: 1 }, x: 540, y: 40 });

  app.connectInto({ id: 'n3' });
  assert.equal(app.workflows.active.edges.length, 1, 'no source armed means no edge');

  app.startLink({ id: 'n2' });
  app.connectInto({ id: 'n3' });
  assert.deepEqual(plain(app.workflows.active.edges), [
    { from: 'n1', to: 'n2' },
    { from: 'n2', to: 'n3' },
  ]);

  app.startLink({ id: 'n1' });
  app.connectInto({ id: 'n3' });
  assert.deepEqual(plain(app.workflows.active.edges), [{ from: 'n1', to: 'n3' }]);
  assert.equal(app.workflows.linkFrom, '');
});

test('removing a node or a line prunes its edges', () => {
  const app = { ...methods(), ...state() };
  app.workflows.active = workflow();
  app.removeEdge({ from: 'n1', to: 'n2' });
  assert.deepEqual(plain(app.workflows.active.edges), []);

  app.workflows.active = workflow();
  app.removeNode({ id: 'n2' });
  assert.deepEqual(app.workflows.active.nodes.map(n => n.id), ['n1']);
  assert.deepEqual(plain(app.workflows.active.edges), []);
});

test('dragging a node moves it and clamps it to the canvas', () => {
  const listeners = {};
  const app = {
    ...methods({}, { window: { confirm: () => true, addEventListener: (name, fn) => { listeners[name] = fn; }, removeEventListener: (name) => { delete listeners[name]; } } }),
    ...state(),
  };
  const node = { id: 'n1', x: 100, y: 100, type: 'wait', config: {} };
  app.beginNodeDrag({ target: { closest: () => null }, preventDefault() {}, clientX: 200, clientY: 200 }, node);
  listeners.pointermove({ clientX: 140, clientY: 260 });
  assert.equal(node.x, 40);
  assert.equal(node.y, 160);
  listeners.pointermove({ clientX: 5000, clientY: -5000 });
  assert.equal(node.x, 1000);
  assert.equal(node.y, 0);
  listeners.pointerup();
  assert.equal(listeners.pointermove, undefined);
});

test('a drag starting on a node button is ignored', () => {
  const app = { ...methods(), ...state() };
  const node = { id: 'n1', x: 100, y: 100, type: 'wait', config: {} };
  app.beginNodeDrag({ target: { closest: () => ({}) }, preventDefault() {} }, node);
  assert.equal(node.x, 100);
});

test('saving posts a new workflow and puts the server copy in the editor', async () => {
  const calls = [];
  const api = {
    post: async (url, body) => { calls.push({ method: 'POST', url, body }); return { data: { workflow: { ...workflow(), ...body } } }; },
    put: async (url, body) => { calls.push({ method: 'PUT', url, body }); return { data: { workflow: { ...workflow(), ...body } } }; },
  };
  const app = { ...methods(api), ...state() };
  app.workflows.active = { ...workflow(), id: '' };
  app.workflows.nameDraft = 'Weekly sweep';
  await app.saveWorkflow();
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, '/api/workflows');
  assert.deepEqual(plain(calls[0].body.nodes.map(n => n.id)), ['n1', 'n2']);
  assert.deepEqual(plain(calls[0].body.edges), [{ from: 'n1', to: 'n2' }]);
  assert.equal(app.workflows.active.id, 'wf_1');
  assert.equal(app.loaded, 1);

  await app.saveWorkflow();
  assert.equal(calls[1].method, 'PUT');
  assert.equal(calls[1].url, '/api/workflows/wf_1');
});

test('a save refusal keeps the error visible and clears the busy flag', async () => {
  const api = {
    put: async () => { const error = new Error('bad'); error.response = { data: { detail: 'A workflow needs exactly one starting node.' } }; throw error; },
  };
  const app = { ...methods(api), ...state() };
  app.workflows.active = workflow();
  await app.saveWorkflow();
  assert.equal(app.workflows.error, 'A workflow needs exactly one starting node.');
  assert.equal(app.workflows.busy, '');
});

test('running needs a saved workflow and accounts, then polls the run', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { run: { id: 'run_1', status: 'running', steps: [] } } }; } };
  const app = { ...methods(api), ...state() };

  await app.runWorkflow();
  assert.equal(app.workflows.error, 'Save the workflow before running it.');

  app.workflows.active = workflow();
  await app.runWorkflow();
  assert.equal(app.workflows.error, 'Choose at least one account to run this workflow.');

  app.workflows.accountIds = ['ui-one', 'ui-two'];
  await app.runWorkflow();
  assert.deepEqual(plain(calls), [{ url: '/api/workflows/wf_1/run', body: { account_ids: ['ui-one', 'ui-two'] } }]);
  assert.equal(app.workflows.run.id, 'run_1');
  assert.equal(app.workflows.activeRunning, true);
  assert.equal(app.polled, 1);
});

test('stopping calls the stop route', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { success: true } }; } };
  const app = { ...methods(api), ...state() };
  app.workflows.active = workflow();
  await app.stopWorkflow();
  assert.deepEqual(plain(calls), [{ url: '/api/workflows/wf_1/stop', body: {} }]);
});

test('deleting is confirmed, removes its schedules and resets the panel', async () => {
  const calls = [];
  const api = { delete: async (url) => { calls.push(url); return { data: { success: true } }; } };
  const app = withComputed({ ...methods(api), ...state() });
  app.workflows.active = workflow();
  app.workflows.schedules = [{ schedule_id: 'wfsch_1', workflow_id: 'wf_1', repeat: 'daily', execute_time: { hour: 9, minute: 0 } }];
  await app.deleteWorkflow();
  assert.deepEqual(calls, ['/api/schedules/wfsch_1', '/api/workflows/wf_1']);
  assert.equal(app.workflows.active, null);
  assert.equal(app.workflows.run, null);
  assert.deepEqual(plain(app.workflows.accountIds), []);
  assert.equal(app.loaded, 1);
});

test('cancelling the delete confirmation leaves the workflow alone', async () => {
  let calls = 0;
  const app = { ...methods({ delete: async () => { calls++; } }, { window: { confirm: () => false } }), ...state() };
  app.workflows.active = workflow();
  await app.deleteWorkflow();
  assert.equal(calls, 0);
  assert.equal(app.workflows.active.id, 'wf_1');
});

test('opening a workflow clones it and restores its last run', () => {
  const app = { ...methods(), ...state() };
  const stored = { ...workflow(), last_run: { id: 'run_9', status: 'succeeded', steps: [] } };
  app.workflows.list = [stored];
  app.openWorkflow(stored.id);
  assert.notEqual(app.workflows.active, stored);
  assert.deepEqual(plain(app.workflows.active.nodes), plain(stored.nodes));
  assert.equal(app.workflows.run.id, 'run_9');
  assert.equal(app.polled, undefined);
});

test('step summaries cover run states and counters', () => {
  const app = { ...methods(), ...state() };
  assert.equal(app.stepSummary({ status: 'pending', detail: {} }), 'waiting');
  assert.equal(app.stepSummary({ status: 'running', detail: {} }), 'running');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'step_failed' } }), 'failed · the step did not finish');
  assert.equal(app.stepSummary({ status: 'stopped', detail: {} }), 'stopped');
  assert.match(app.stepSummary({ status: 'ok', detail: { stopped: 'telegram_cooldown', wait_seconds: 42 } }), /42/);
  assert.equal(app.stepSummary({ status: 'ok', detail: { scraped: 12 } }), '12 members read');
  assert.match(app.stepSummary({ status: 'ok', detail: { joined: 2, skipped: 1, failed: 0 } }), /2 joined/);
  assert.equal(app.stepSummary({ status: 'ok', detail: { waited_seconds: 30 } }), 'waited 30s');
  assert.match(app.stepSummary({ status: 'ok', detail: { characters: 18 } }), /18 chars/);
  assert.equal(app.stepSummary({ status: 'ok', detail: { appended: 3, skipped: 1 } }), '3 added · 1 already there');
});

test('failure summaries name the safe code without leaking an exception', () => {
  const app = { ...methods(), ...state() };
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'account_not_connected' } }),
    'failed · the account is not signed in');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'operator_required' } }),
    'failed · the operator session was missing');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'timed_out' } }),
    'failed · the step timed out');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'network' } }),
    'failed · the network dropped');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'invalid_link' } }),
    'failed · not a group link');
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'telegram_peer_flood' } }),
    'failed · Telegram said peer flood');
  assert.equal(app.failureLabel('telegram_username_not_occupied'), 'Telegram said username not occupied');
});

test('the completed catalog summarises inspect, table read and honest join counts', () => {
  const app = { ...methods(), ...state() };
  assert.equal(app.stepSummary({ status: 'ok', detail: { title: 'Harness group', members: 1500, kind: 'group' } }),
    'Harness group · 1500 members');
  assert.equal(app.stepSummary({ status: 'ok', detail: { title: 'Hidden', members: null, kind: 'channel' } }),
    'Hidden · member count hidden');
  assert.equal(app.stepSummary({ status: 'ok', detail: { rows: 12, values: 10 } }),
    '10 values read from 12 rows');
  assert.equal(app.stepSummary({ status: 'ok', detail: { joined: 1, skipped: 2, failed: 1, reasons: { invalid_link: 1 } } }),
    '1 joined · 2 already in · 1 failed (1 not a group link)');
  assert.equal(app.stepSummary({ status: 'ok', detail: { joined: 0, skipped: 0, failed: 0, stopped: 'table_empty' } }),
    'the table had no links');
  assert.equal(app.stepSummary({ status: 'ok', detail: { scraped: 2, limited: true } }),
    '2 recent posters read · partial results');
});

test('node summaries reflect the configured values', () => {
  const app = { ...methods(), ...state() };
  assert.match(app.nodeSummary({ type: 'scrape', config: { target: '@news', limit: 50 } }), /@news/);
  assert.equal(app.nodeSummary({ type: 'wait', config: { seconds: 12 } }), 'Pause 12s');
  assert.match(app.nodeSummary({ type: 'join', config: { urls: ['a'], delay: 35 } }), /1 link/);
  assert.equal(app.nodeSummary({ type: 'notify', config: { text: 'done' } }), 'done');
  assert.equal(app.nodeSummary({ type: 'table_append', config: { table_id: '', column_id: '' } }), 'Pick a table');
  app.workflows.tableList = [{ id: 't_1', name: 'Members' }];
  app.workflows.tableDetails = { t_1: { columns: [{ id: 'c_1', name: 'Username', type: 'text' }] } };
  assert.equal(app.nodeSummary({ type: 'table_append', config: { table_id: 't_1', column_id: 'c_1' } }), 'Members · Username');
});

test('the account picker enforces the run cap and frees slots', () => {
  const app = { ...methods(), ...state() };
  app.workflows.capacity.accounts = 2;
  app.toggleWorkflowAccount('a');
  app.toggleWorkflowAccount('b');
  app.toggleWorkflowAccount('c');
  assert.deepEqual(plain(app.workflows.accountIds), ['a', 'b']);
  assert.match(app.workflows.error, /at most 2 accounts/);
  app.toggleWorkflowAccount('a');
  assert.deepEqual(plain(app.workflows.accountIds), ['b']);
  app.toggleWorkflowAccount('c');
  assert.deepEqual(plain(app.workflows.accountIds), ['b', 'c']);
  assert.equal(app.workflows.error, '');
});

test('a preset opens as an unsaved draft with cloned nodes', () => {
  const app = withComputed({ ...methods(), ...state() });
  const template = {
    id: 'member_table', name: 'Scrape to table', description: 'Read a roster into a table.',
    nodes: [
      { id: 'n_scrape', type: 'scrape', config: { target: '', limit: 200, filter_bots: true }, x: 40, y: 40 },
      { id: 'n_table', type: 'table_append', config: { table_id: '', column_id: '' }, x: 290, y: 40 },
    ],
    edges: [{ from: 'n_scrape', to: 'n_table' }],
  };
  app.applyWorkflowTemplate(template);
  assert.equal(app.workflows.active.id, '');
  assert.equal(app.workflows.nameDraft, 'Scrape to table');
  assert.notEqual(app.workflows.active.nodes, template.nodes);
  assert.deepEqual(plain(app.workflows.active.edges), plain(template.edges));
  assert.equal(app.workflows.run, null);
  assert.equal(app.workflows.scheduleForm.open, false);
  template.nodes[0].config.target = '@mutated';
  assert.equal(app.workflows.active.nodes[0].config.target, '', 'the draft must not share the preset object');
  assert.match(app.toasts.at(-1).message, /draft/);
});

test('scheduling requires accounts and posts the repeat plan', async () => {
  const calls = [];
  const api = {
    post: async (url, body) => {
      calls.push({ url, body });
      return { data: { schedule: { schedule_id: 'wfsch_1', repeat: body.repeat, execute_time: { hour: body.hour, minute: body.minute } } } };
    },
  };
  const app = withComputed({ ...methods(api), ...state() });
  app.workflows.active = workflow();
  await app.saveWorkflowSchedule();
  assert.equal(app.workflows.error, 'Choose at least one account for the schedule.');
  assert.equal(calls.length, 0);

  app.workflows.accountIds = ['ui-one'];
  app.workflows.scheduleForm = { open: true, repeat: 'weekly', hour: 9, minute: 30, weekday: 1, date: '', busy: '' };
  await app.saveWorkflowSchedule();
  assert.deepEqual(plain(calls), [{
    url: '/api/workflows/wf_1/schedule',
    body: { account_ids: ['ui-one'], repeat: 'weekly', hour: 9, minute: 30, weekday: 1, date: '' },
  }]);
  assert.equal(app.workflows.scheduleForm.open, false);
  assert.equal(app.workflows.scheduleForm.busy, '');
  assert.equal(app.loaded, 1);
  assert.match(app.toasts.at(-1).message, /Every week at 09:30/);
});

test('deleting a schedule confirms and calls the schedule route', async () => {
  const calls = [];
  const api = { delete: async (url) => { calls.push(url); return { data: { success: true } }; } };
  const app = withComputed({ ...methods(api), ...state() });
  await app.deleteWorkflowSchedule({ schedule_id: 'wfsch_1', repeat: 'daily', execute_time: { hour: 9, minute: 0 } });
  assert.deepEqual(calls, ['/api/schedules/wfsch_1']);
  assert.equal(app.loaded, 1);
});

test('schedule labels describe each repeat mode', () => {
  const app = { ...methods(), ...state() };
  assert.equal(app.scheduleLabel({ repeat: 'daily', execute_time: { hour: 9, minute: 5 } }), 'Every day at 09:05');
  assert.equal(app.scheduleLabel({ repeat: 'workday', execute_time: { hour: 18, minute: 0 } }), 'Workdays at 18:00');
  assert.equal(app.scheduleLabel({ repeat: 'weekly', execute_time: { hour: 7, minute: 0 } }), 'Every week at 07:00');
  assert.equal(app.scheduleLabel({ repeat: 'once', execute_time: { day: 7, month: 11, hour: 8, minute: 0 } }), 'Once on 07/11 at 08:00');
});

test('the save-to-table node picks a table and offers only text columns', async () => {
  const detail = {
    id: 't_1', name: 'Members',
    columns: [{ id: 'c_1', name: 'Username', type: 'text' }, { id: 'c_2', name: 'Score', type: 'number' }],
    rows: [],
  };
  const api = { get: async (url) => { assert.equal(url, '/api/tables/t_1'); return { data: { table: detail } }; } };
  const app = withComputed({ ...methods(api), ...state() });
  app.workflows.active = { id: '', name: 'x', nodes: [{ id: 'n1', type: 'table_append', config: { table_id: '', column_id: '' }, x: 0, y: 0 }], edges: [] };
  app.workflows.selectedId = 'n1';
  app.workflows.tableList = [{ id: 't_1', name: 'Members' }];
  app.chooseWorkflowTable('t_1');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.workflows.active.nodes[0].config.table_id, 't_1');
  assert.equal(app.workflows.active.nodes[0].config.column_id, '');
  assert.deepEqual(plain(app.workflows.tableDetails.t_1.columns), plain(detail.columns));
  assert.deepEqual(app.workflowNodeColumns.map(column => column.id), ['c_1']);
});

test('run steps group by account for the log', () => {
  const app = withComputed({ ...methods(), ...state() });
  app.workflows.run = { steps: [
    { account_id: 'ui-one', status: 'ok' }, { account_id: 'ui-two', status: 'running' }, { account_id: 'ui-one', status: 'pending' },
  ] };
  const groups = app.runAccounts;
  assert.deepEqual(plain(groups.map(group => group.account_id)), ['ui-one', 'ui-two']);
  assert.equal(groups[0].steps.length, 2);
  assert.equal(app.accountLabel('ui-one'), 'UI One');
  app.workflows.run = { steps: [{ status: 'ok' }] };
  assert.deepEqual(plain(app.runAccounts), [], 'legacy single-account runs stay flat');
});