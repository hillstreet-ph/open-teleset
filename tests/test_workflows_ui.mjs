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

const CATALOG = [
  { type: 'scrape', label: 'Scrape members', hint: 'Read a group roster.', fields: [] },
  { type: 'wait', label: 'Wait', hint: 'Pause between steps.', fields: [] },
  { type: 'join', label: 'Join groups', hint: 'Join consented links.', fields: [] },
  { type: 'notify', label: 'Note yourself', hint: 'Send yourself a note.', fields: [] },
];

function methods(api = {}, extra = {}) {
  return vm.runInNewContext(
    `({${html.slice(html.indexOf('// Node workflows'), html.indexOf('// Data tables'))}})`,
    { _api: api, window: { confirm: () => true }, console, Math, JSON, setTimeout, clearTimeout, ...extra },
  );
}

function state() {
  return {
    workflows: {
      loading: false, busy: '', error: '', list: [],
      capacity: { used: 0, limit: 50, nodes: 20 }, catalog: CATALOG,
      active: null, nameDraft: '', selectedId: '', linkFrom: '',
      accountId: '', run: null, activeRunning: false, poll: null,
    },
    accounts: [{ account_id: 'ui-one', name: 'UI One' }],
    toasts: [],
    showToast(message, isError) { this.toasts.push({ message, isError }); },
    async loadWorkflows() { this.loaded = (this.loaded || 0) + 1; },
    pollWorkflowRun() { this.polled = (this.polled || 0) + 1; },
  };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

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
  assert.equal(config.workflows.active, null);
  assert.equal(config.workflows.activeRunning, false);
});

test('the sidebar, tab panel, canvas and controls are wired into the dashboard', () => {
  assert.ok(html.includes("{ tab: 'workflows', label: 'Workflows', icon: 'code' }"));
  assert.ok(html.includes("v-show=\"tab === 'workflows'\""));
  for (const id of ['wf-new', 'wf-save', 'wf-run', 'wf-name', 'wf-account']) {
    assert.ok(html.includes(`id="${id}"`), `missing #${id}`);
  }
  for (const testid of ['wf-editor', 'wf-canvas', 'wf-node', 'wf-config', 'wf-runlog']) {
    assert.ok(html.includes(`data-testid="${testid}"`), `missing data-testid ${testid}`);
  }
  assert.ok(html.includes(":data-testid=\"'wf-add-' + item.type\""));
  assert.ok(html.includes("if (tab === 'workflows') this.loadWorkflows()"));
  assert.ok(html.includes("if (this.tab === 'workflows') this.loadWorkflows()"));
});

test('loading workflows stores the list, capacity and catalog', async () => {
  const api = { get: async (url) => { assert.equal(url, '/api/workflows'); return { data: { workflows: [workflow()], capacity: { used: 1, limit: 50, nodes: 20 }, catalog: CATALOG } }; } };
  const base = state();
  delete base.loadWorkflows;
  const app = { ...methods(api), ...base };
  await app.loadWorkflows();
  assert.equal(app.workflows.list.length, 1);
  assert.equal(app.workflows.capacity.used, 1);
  assert.equal(app.workflows.catalog.length, 4);
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

test('running needs a saved workflow and an account, then polls the run', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { run: { id: 'run_1', status: 'running', steps: [] } } }; } };
  const app = { ...methods(api), ...state() };

  await app.runWorkflow();
  assert.equal(app.workflows.error, 'Save the workflow before running it.');

  app.workflows.active = workflow();
  await app.runWorkflow();
  assert.equal(app.workflows.error, 'Choose the account that should run this workflow.');

  app.workflows.accountId = 'ui-one';
  await app.runWorkflow();
  assert.deepEqual(plain(calls), [{ url: '/api/workflows/wf_1/run', body: { account_id: 'ui-one' } }]);
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

test('deleting is confirmed, calls the route and resets the panel', async () => {
  const calls = [];
  const api = { delete: async (url) => { calls.push(url); return { data: { success: true } }; } };
  const app = { ...methods(api), ...state() };
  app.workflows.active = workflow();
  await app.deleteWorkflow();
  assert.deepEqual(calls, ['/api/workflows/wf_1']);
  assert.equal(app.workflows.active, null);
  assert.equal(app.workflows.run, null);
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
  assert.equal(app.stepSummary({ status: 'failed', detail: { error: 'step_failed' } }), 'failed');
  assert.equal(app.stepSummary({ status: 'stopped', detail: {} }), 'stopped');
  assert.match(app.stepSummary({ status: 'ok', detail: { stopped: 'telegram_cooldown', wait_seconds: 42 } }), /42/);
  assert.equal(app.stepSummary({ status: 'ok', detail: { scraped: 12 } }), '12 members read');
  assert.match(app.stepSummary({ status: 'ok', detail: { joined: 2, skipped: 1, failed: 0 } }), /2 joined/);
  assert.equal(app.stepSummary({ status: 'ok', detail: { waited_seconds: 30 } }), 'waited 30s');
  assert.match(app.stepSummary({ status: 'ok', detail: { characters: 18 } }), /18 chars/);
});

test('node summaries reflect the configured values', () => {
  const app = { ...methods(), ...state() };
  assert.match(app.nodeSummary({ type: 'scrape', config: { target: '@news', limit: 50 } }), /@news/);
  assert.equal(app.nodeSummary({ type: 'wait', config: { seconds: 12 } }), 'Pause 12s');
  assert.match(app.nodeSummary({ type: 'join', config: { urls: ['a'], delay: 35 } }), /1 link/);
  assert.equal(app.nodeSummary({ type: 'notify', config: { text: 'done' } }), 'done');
});